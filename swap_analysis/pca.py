"""
主成分分析(PCA)。スワップ(テナー)とボラ(満期x原資産)の共通ロジック。

■ 手順(標準的なPCA)
  1. 推定: 推定基準日(windowの最終日)からさかのぼって、選択した銘柄が全てそろった
     営業日のサンプルを N 個集める(1本でも欠けた日は除外して、さらに過去へ)。
       - LEVEL : サンプル = 各日の水準
       - CHANGE: サンプル = 隣り合う営業日どうしの日次変化(間の営業日が欠けている
                 ペアは2日分の変化になるので使わない)
     サンプルの平均 μ と、標準化するなら標準偏差 σ(しないなら σ=1)を求め、
     z = (サンプル − μ) / σ の分散共分散行列を固有値分解して主成分ベクトル V を作る。
     標準化なし=分散共分散行列(bp)のPCA、標準化あり=相関行列のPCA(z-score)。
  2. 比較: 比較日の実績 x(LEVELは水準、CHANGEは比較日の日次変化)について
       得点 s = Vᵀ(x − μ)/σ、復元 = μ + σ·V_k·s_k、差分 = x − 復元。
     この計算は Excel の数式で行う(ここでは μ・σ・V と x を返すだけ)。
     比較日は推定基準日と独立に選べる(windowの中でもよい)。

■ 推定基準日の既定(空欄)
  比較日より前の、選択銘柄がそろった直近の営業日(out-of-sample。当日のリアルタイムの気配は
  実勢を表していない時間帯があるため推定に混ぜない。ユーザー指示)。それが比較日の前営業日で
  なければ(DBの更新漏れ等)警告を出して計算は続ける。

■ 標準化ありの注意
  windowの中でほとんど動かない銘柄(利上げの間のO/N等)は σ≈0 になり z-score が極端になる。
  σ が SIGMA_FLOOR_BP 未満の銘柄があればエラーにする(黙って外したり補ったりしない)。

■ 主成分の符号
  固有ベクトルの符号は任意なので、j番目の主成分は銘柄の並び(等間隔とみなす)上の
  ルジャンドル多項式 P_j との内積が正になる向きに揃える(PC1=全体が正、PC2=後ろ側が正、
  PC3=両端が正)。windowや日付が変わってもグラフが反転しないようにするため。
"""

from datetime import date, timedelta
from typing import List, NamedTuple, Optional, Sequence, Tuple

import numpy as np
import QuantLib as ql

from swap_analysis.history import History, normalize_label
from swap_pricing.conventions import CALENDAR

LEVEL = "LEVEL"
CHANGE = "CHANGE"
_MODE_ALIASES = {"LEVEL": LEVEL, "レベル": LEVEL, "CHANGE": CHANGE, "日次変化": CHANGE}
_STANDARDIZE_ALIASES = {"あり": True, "なし": False, "TRUE": True, "FALSE": False, "1": True, "0": False}

SIGMA_FLOOR_BP = 0.01


def parse_mode(value) -> str:
    mode = _MODE_ALIASES.get(str(value or "").strip().upper())
    if mode is None:
        raise ValueError(f"モードは「レベル」か「日次変化」です: {value!r}")
    return mode


def parse_standardize(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    flag = _STANDARDIZE_ALIASES.get(str(value or "").strip().upper())
    if flag is None:
        raise ValueError(f"標準化は「あり」か「なし」です: {value!r}")
    return flag


class PcaResult(NamedTuple):
    eigenvalues: np.ndarray  # 降順(サンプルの単位の2乗)
    vectors: np.ndarray  # 列 j が第 j+1 主成分(単位ベクトル)


def principal_components(samples: np.ndarray) -> PcaResult:
    """サンプル行列(行=観測、列=変数)→ 分散共分散行列の固有値・固有ベクトル。"""
    samples = np.asarray(samples, dtype=float)
    n, m = samples.shape
    if n < 2:
        raise ValueError(f"サンプルが足りません: {n}")
    cov = np.atleast_2d(np.cov(samples, rowvar=False, ddof=1))
    values, vectors = np.linalg.eigh(cov)
    order = np.argsort(values)[::-1]
    values = np.clip(values[order], 0.0, None)  # 丸め誤差の負値を0に
    vectors = vectors[:, order]
    x = np.linspace(-1.0, 1.0, m) if m > 1 else np.zeros(1)
    for j in range(m):
        ref = np.polynomial.legendre.Legendre.basis(j)(x)
        dot = float(ref @ vectors[:, j])
        if abs(dot) < 1e-12:  # 参照と直交する場合は絶対値最大の要素を正にする
            dot = float(vectors[np.argmax(np.abs(vectors[:, j])), j])
        if dot < 0:
            vectors[:, j] = -vectors[:, j]
    return PcaResult(values, vectors)


def _business_days(start: date, end: date) -> int:
    """start〜end(両端含む)の営業日数。start > end なら0。"""
    if start > end:
        return 0
    return CALENDAR.businessDaysBetween(
        ql.Date.from_date(start), ql.Date.from_date(end), True, True,
    )


def _previous_business_day(d: date) -> date:
    return CALENDAR.advance(ql.Date.from_date(d), -1, ql.Days).to_date()


def _labels(labels: Sequence[str]) -> Tuple[str, ...]:
    keys = tuple(normalize_label(t) for t in labels)
    if not keys:
        raise ValueError("銘柄(テナー)が選択されていません")
    duplicated = sorted({t for t in keys if keys.count(t) > 1})
    if duplicated:
        raise ValueError(f"銘柄が重複しています: {', '.join(duplicated)}")
    return keys


def _row(row, keys: Sequence[str]):
    if row is None or any(k not in row for k in keys):
        return None
    return np.array([row[k] for k in keys], dtype=float)


def _valid_dates_desc(history: History, keys, until: date) -> List[Tuple[date, np.ndarray]]:
    """until 以前で選択銘柄がそろった日(新しい順)。"""
    valid = []
    for d in sorted((d for d in history if d <= until), reverse=True):
        x = _row(history[d], keys)
        if x is not None:
            valid.append((d, x))
    return valid


class PcaModel(NamedTuple):
    labels: Tuple[str, ...]  # 突き合わせ用(大文字)
    mode: str
    standardize: bool
    start: date  # サンプルに使った最初の日(CHANGEは最初の変化の前日)
    end: date  # 推定終了日(windowの最終日)
    n_samples: int
    excluded_days: int  # start〜endの営業日のうち、サンプルに使わなかった日数
    mean: np.ndarray  # μ(値の単位。CHANGEは日次変化の平均)
    scale: np.ndarray  # σ(標準化なしは1)
    eigenvalues: np.ndarray  # 標準化なし: bp²、あり: 単位なし
    vectors: np.ndarray


def estimate(
    history: History, labels: Sequence[str], mode: str, standardize: bool, window: int,
    until: date, unit_bp: float,
) -> PcaModel:
    """until(推定基準日)以前の window 個のサンプルで主成分ベクトルを作る。"""
    keys = _labels(labels)
    if window < 2:
        raise ValueError(f"windowは2以上です: {window}")
    valid = _valid_dates_desc(history, keys, until)
    if not valid:
        raise ValueError(f"{until}以前に、選択銘柄がそろった日がDBにありません")
    end = valid[0][0]

    rows: List[np.ndarray] = []
    used_days = set()
    if mode == LEVEL:
        for d, x in valid[:window]:
            rows.append(x)
            used_days.add(d)
    else:
        by_date = dict(valid)
        for d, x in valid:
            if len(rows) == window:
                break
            prev = _previous_business_day(d)
            if prev in by_date:
                rows.append(x - by_date[prev])
                used_days.update((d, prev))
    if len(rows) < window:
        unit = "日" if mode == LEVEL else "個の日次変化"
        raise ValueError(
            f"windowの{window}{unit}に対し、DBで使えるのは{len(rows)}{unit}だけです"
            f"({until}以前、選択銘柄がそろった日)"
        )
    samples = np.array(rows[::-1])
    mean = samples.mean(axis=0)
    if standardize:
        scale = samples.std(axis=0, ddof=1)
        flat = [keys[i] for i in np.flatnonzero(scale * unit_bp < SIGMA_FLOOR_BP)]
        if flat:
            raise ValueError(
                f"windowの中でほとんど動かない銘柄があり、標準化できません: {', '.join(flat)}"
                f"(標準偏差{SIGMA_FLOOR_BP}bp未満。外すか、標準化なしにする)"
            )
        result = principal_components((samples - mean) / scale)
    else:
        scale = np.ones(len(keys))
        result = principal_components(samples * unit_bp)
    start = min(used_days)
    return PcaModel(
        labels=keys, mode=mode, standardize=standardize, start=start, end=end,
        n_samples=len(rows), excluded_days=_business_days(start, end) - len(used_days),
        mean=mean, scale=scale, eigenvalues=result.eigenvalues, vectors=result.vectors,
    )


def default_until(target_date: date) -> date:
    """推定基準日が空欄のとき: 比較日より前の日まで(out-of-sample)。"""
    return target_date - timedelta(days=1)


def gap_warning(end: date, target_date: date, what: str) -> str:
    """end が target_date の前営業日でなければ警告文(DBの更新漏れ等)。なければ空。"""
    gap = _business_days(end + timedelta(days=1), target_date)
    if gap <= 1:
        return ""
    return f"{what}{end}は比較日{target_date}の{gap}営業日前です(DB未更新か、銘柄の欠損)"


class Observation(NamedTuple):
    values: np.ndarray  # LEVEL: 比較日の水準、CHANGE: 基準日→比較日の変化
    base_date: Optional[date]  # CHANGEの変化の基準日(DBの値)
    warning: str


def observe(
    history: History, labels: Sequence[str], mode: str, target_date: date, level, source: str,
) -> Observation:
    """比較日の実績。level は比較日の {銘柄(大文字): 値}(RealTimeかDBのその日)。

    CHANGEは「比較日の水準 − 比較日より前で選択銘柄がそろったDBの直近の日の水準」。
    その日が前営業日でなければ、何営業日分の変化かを警告に出して計算は続ける(ユーザー指示)。
    """
    keys = _labels(labels)
    missing = [k for k in keys if level is None or k not in level]
    if missing:
        raise ValueError(f"{source}に値がない銘柄: {', '.join(missing)}")
    x = np.array([level[k] for k in keys], dtype=float)
    if mode == LEVEL:
        return Observation(x, None, "")
    valid = _valid_dates_desc(history, keys, target_date - timedelta(days=1))
    if not valid:
        raise ValueError(f"比較日{target_date}より前に、選択銘柄がそろった日がDBにありません")
    base, prev = valid[0]
    gap = _business_days(base + timedelta(days=1), target_date)
    warning = (
        f"比較日の変化は{base}からの{gap}営業日分です(前営業日のDBの値がありません)"
        if gap > 1 else ""
    )
    return Observation(x - prev, base, warning)
