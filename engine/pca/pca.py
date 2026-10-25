"""
主成分分析(PCA)。スワップ(テナー)とボラ(満期x原資産)の共通ロジック。

■ 手順(標準的なPCA)
  1. 推定: 推定の最終日(Window end)からさかのぼって、選択した銘柄が全てそろった
     営業日のサンプルを N 個集める(1本でも欠けた日は除外して、さらに過去へ)。
       - LEVEL : サンプル = 各日の水準
       - CHANGE: サンプル = 隣り合う営業日どうしの日次変化(間の営業日が欠けている
                 ペアは2日分の変化になるので使わない)
     サンプルの平均 μ と、標準化するなら標準偏差 σ(しないなら σ=1)を求め、
     z = (サンプル − μ) / σ の分散共分散行列を固有値分解して主成分ベクトル V を作る。
     標準化なし=分散共分散行列(bp)のPCA、標準化あり=相関行列のPCA(z-score)。
  2. 復元: 復元日(Recon date)の実績 x(LEVELは水準、CHANGEは復元日の日次変化)について
       得点 s = Vᵀ(x − μ)/σ、復元 = μ + σ·V_k·s_k、差分 = x − 復元。
     この計算は Excel の数式で行う(ここでは μ・σ・V と x を返すだけ)。
     復元日は推定の最終日と独立に選べる(windowの中でもよい)。
  3. 差分のσ(residual_std): 推定した μ・σ・V を固定したまま、推定終了日からさかのぼったN日の
     サンプル(推定と同じ選び方。Nの既定はwindow)の各日を k 本で復元して差分を出し、
     銘柄ごとの標準偏差をとる(差分のグラフの±1σ。定義どおりに日ごとに計算する)。

■ 推定の最終日・復元日の既定(空欄)
  どちらも今日(ユーザー指示)。推定に使うのはDBの引け値だけで、今日のリアルタイムの気配は推定に
  混ぜない(実勢を表していない時間帯があるため。ユーザー指示)。DBに今日の引け値がなければ、
  推定は実際には直近の営業日までになる。推定に使った最後の日が推定の最終日の前営業日より前なら
  (DBの更新漏れ・銘柄の欠損)警告を出して計算は続ける。

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

from engine.pca.history import History, normalize_label
from engine.common.conventions import CALENDAR

LEVEL = "LEVEL"
CHANGE = "CHANGE"
_MODE_ALIASES = {"LEVEL": LEVEL, "レベル": LEVEL, "CHANGE": CHANGE, "日次変化": CHANGE}
_STANDARDIZE_ALIASES = {
    "あり": True, "なし": False, "YES": True, "NO": False, "TRUE": True, "FALSE": False, "1": True, "0": False,
}

SIGMA_FLOOR_BP = 0.01


def parse_mode(value) -> str:
    mode = _MODE_ALIASES.get(str(value or "").strip().upper())
    if mode is None:
        raise ValueError(f"モードは「Level」か「Change」(「レベル」/「日次変化」)です: {value!r}")
    return mode


def parse_standardize(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    flag = _STANDARDIZE_ALIASES.get(str(value or "").strip().upper())
    if flag is None:
        raise ValueError(f"標準化は「Yes」か「No」(「あり」/「なし」)です: {value!r}")
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
    scale: np.ndarray  # 標準化に使うσ(標準化なしは1)
    std: np.ndarray  # windowのサンプルの標準偏差(値の単位。標準化の有無によらない。グラフの±1σ用)
    eigenvalues: np.ndarray  # 標準化なし: bp²、あり: 単位なし
    vectors: np.ndarray


def _samples(
    history: History, keys: Tuple[str, ...], mode: str, n: int, until: date, what: str,
) -> Tuple[np.ndarray, set, date]:
    """until 以前で選択銘柄がそろった日から n 個のサンプル(古い順)。
    → (サンプル行列, 使った日の集合, 最後の日)。足りなければエラー(what は「window」等)。"""
    if n < 2:
        raise ValueError(f"{what}は2以上です: {n}")
    valid = _valid_dates_desc(history, keys, until)
    if not valid:
        raise ValueError(f"{until}以前に、選択銘柄がそろった日がDBにありません")
    rows: List[np.ndarray] = []
    used_days = set()
    if mode == LEVEL:
        for d, x in valid[:n]:
            rows.append(x)
            used_days.add(d)
    else:
        by_date = dict(valid)
        for d, x in valid:
            if len(rows) == n:
                break
            prev = _previous_business_day(d)
            if prev in by_date:
                rows.append(x - by_date[prev])
                used_days.update((d, prev))
    if len(rows) < n:
        unit = "日" if mode == LEVEL else "個の日次変化"
        raise ValueError(
            f"{what}の{n}{unit}に対し、DBで使えるのは{len(rows)}{unit}だけです"
            f"({until}以前、選択銘柄がそろった日)"
        )
    return np.array(rows[::-1]), used_days, valid[0][0]


def estimate(
    history: History, labels: Sequence[str], mode: str, standardize: bool, window: int,
    until: date, unit_bp: float,
) -> PcaModel:
    """until(推定の最終日)以前の window 個のサンプルで主成分ベクトルを作る。"""
    keys = _labels(labels)
    samples, used_days, end = _samples(history, keys, mode, window, until, "window")
    mean = samples.mean(axis=0)
    std = samples.std(axis=0, ddof=1)
    if standardize:
        scale = std
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
        n_samples=len(samples), excluded_days=_business_days(start, end) - len(used_days),
        mean=mean, scale=scale, std=std, eigenvalues=result.eigenvalues, vectors=result.vectors,
    )


def residual_std(
    history: History, model: PcaModel, k: int, n_days: Optional[int], unit_bp: float,
) -> np.ndarray:
    """
    差分(実績 − k本での復元)の標準偏差(bp、銘柄ごと)。主成分ベクトル・μ・σは model のまま固定し、
    推定終了日からさかのぼった n_days 個のサンプル(推定と同じ選び方。None=推定のwindowと同じ数)の
    各日について 復元 = μ + σ·V_k·V_kᵀ(x − μ)/σ、差分 = x − 復元 を出して標準偏差(ddof=1)をとる。
    """
    m = len(model.labels)
    if not 1 <= k <= m:
        raise ValueError(f"主成分数kは1〜{m}です: {k}")
    n = model.n_samples if n_days is None else n_days
    samples, _, _ = _samples(history, model.labels, model.mode, n, model.end, "σの日数")
    v = model.vectors[:, :k]
    z = (samples - model.mean) / model.scale
    recon = model.mean + model.scale * (z @ v @ v.T)
    return (samples - recon).std(axis=0, ddof=1) * unit_bp


def gap_warning(end: date, window_end: date) -> str:
    """推定に使った最後の日 end が推定の最終日 window_end(当日か前営業日)に届いていなければ
    警告文(DBの更新漏れ・銘柄の欠損。セルに出すので英語)。なければ空。"""
    gap = _business_days(end + timedelta(days=1), window_end)
    if gap <= 1:
        return ""
    return f"Window ends {end}, {gap} business days before {window_end} (DB not updated, or tenors missing)"


class Observation(NamedTuple):
    values: np.ndarray  # LEVEL: 復元日の水準、CHANGE: 基準日→復元日の変化
    base_date: Optional[date]  # CHANGEの変化の基準日(DBの値)
    warning: str


def observe(
    history: History, labels: Sequence[str], mode: str, recon_date: date, level, source: str,
) -> Observation:
    """復元日の実績。level は復元日の {銘柄(大文字): 値}(RealTimeかDBのその日)。

    CHANGEは「復元日の水準 − 復元日より前で選択銘柄がそろったDBの直近の日の水準」。
    その日が前営業日でなければ、何営業日分の変化かを警告に出して計算は続ける(ユーザー指示)。
    """
    keys = _labels(labels)
    missing = [k for k in keys if level is None or k not in level]
    if missing:
        raise ValueError(f"{source}に値がない銘柄: {', '.join(missing)}")
    x = np.array([level[k] for k in keys], dtype=float)
    if mode == LEVEL:
        return Observation(x, None, "")
    valid = _valid_dates_desc(history, keys, recon_date - timedelta(days=1))
    if not valid:
        raise ValueError(f"復元日{recon_date}より前に、選択銘柄がそろった日がDBにありません")
    base, prev = valid[0]
    gap = _business_days(base + timedelta(days=1), recon_date)
    warning = (
        f"Change over {gap} business days from {base} (no DB value on the previous business day)"
        if gap > 1 else ""
    )
    return Observation(x - prev, base, warning)
