"""
ポジション・リスク管理のブック(PositionSheet.xlsm、叩き台)のUDF。

■ 設計方針(他のブックと同じ)
  - QuantLibが要る計算(現物・日中の取引のバケットデルタ、フォワードの換算行列)と、固有値分解・DBの読み出し
    (PCA、前日の引け値)だけをUDFにする。グリッドの合計・ネット、PCAの射影(エクスポージャー・1σ・残差)、
    フォワードのデルタ(Jᵀd)、日中の損益の説明(デルタ・ガンマ・ベガ)、PCA中立のヘッジは、
    Excelの数式(SUM・MMULT・MINVERSE)で計算する。
  - 計算方法は「手動」+F9。UDFは非volatile。TODAY()は引数に使わない(評価日の空欄=今日)。
  - 配列はそのまま返し、Excelの動的配列のスピルに任せる(@xw.ret(expand=...)は使わない)。
  - 会社のリスク(デルタ・ガンマ・ベガ)は入力シートに貼り付けるだけで、UDFは読まない(四則演算と行列の積だけ)。

■ 単位・符号(全シート共通)
  デルタ: 百万円/bp(金利が1bp上がったときのPVの変化)。想定元本・額面: 百万円。

■ 引数(共通)
  pillars: グリッドのピラーの縦の並び(Setupシート。短い順)。結果の列・行はこの並び。
  real_time_range: フラグ/Ticker/Value の範囲(Setupシートのカーブ用の範囲)。評価日の空欄=今日はライブ、
    過去日はDBのその日の引け値(他のブックと同じ udf_common._market)。
"""

from functools import lru_cache
from typing import List, Optional, Tuple

import xlwings as xw

from engine.common import pca, udf_common
from engine.common.history import SWAP, UNIT_BP, load_history, normalize_label
from engine.common.historical_store import HistoricalStore
from engine.common.udf_common import _is_blank, _to_date, _today
from engine.position import bonds, forward, trades

_PCA_MODE = pca.CHANGE  # 日次変化の分散共分散(bp)。米金利のデスクで標準のやり方(docs/position.md)


def _labels(values) -> Tuple[str, ...]:
    return tuple(str(v).strip() for v in (values or []) if not _is_blank(v))


def _number(value, name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name}が数値ではありません: {value!r}") from None


def _optional_number(value, name: str) -> Optional[float]:
    return None if _is_blank(value) else _number(value, name)


def _rows(table) -> List[list]:
    return [list(r) if r is not None else [] for r in (table or [])]


def _deltas_row(deltas: dict, pillars: Tuple[str, ...]) -> list:
    """{カーブのラベル(小文字): デルタ} → ピラーの並び(カーブに使っていないピラーは0)。"""
    return [float(deltas.get(p.lower(), 0.0)) for p in pillars]


def _blank_row(n: int) -> list:
    return [""] * n


BOND_HEADER = ["Maturity", "Coupon", "Yield", "Close date", "Price", "Z-spread", "DV01", "Message"]


@xw.func
@xw.arg("positions", ndim=2)
@xw.arg("pillars", ndim=1)
@xw.arg("real_time_range", ndim=2)
def PosBondRisk(positions, pillars, real_time_range, valuation_date=None) -> List[list]:
    """
    現物のポジション(行ごと)のピラーごとのデルタ(Zスプレッド固定。engine/position/bonds.py)。
    positions: [Code, Face(百万円), Yield override(単利%、空欄=DBの引け値)] の行(見出しなし)。Codeが空の行は空欄。
    返り値: 見出し行 [Maturity, Coupon, Yield, Close date, Price, Z-spread(bp), DV01, Message, ピラー…] + 入力と同じ行数。
    DV01 = ピラーのデルタの合計(百万円/bp)。エラーの行はMessageに理由を出し、他の行は続ける。
    """
    labels = _labels(pillars)
    rows = _rows(positions)
    parsed: List[Optional[bonds.BondPosition]] = []
    errors = {}
    for n, row in enumerate(rows):
        row = row + [None] * (3 - len(row))
        if _is_blank(row[0]):
            parsed.append(None)
            continue
        try:
            parsed.append(bonds.BondPosition(
                str(row[0]), _number(row[1], "Face"), _optional_number(row[2], "Yield override"),
            ))
        except ValueError as e:
            parsed.append(None)
            errors[n] = e
    curves = udf_common._market(real_time_range, valuation_date)
    with udf_common._historical_store() as store:
        results = bonds.bond_risks(curves, store, parsed)
    width = len(BOND_HEADER) + len(labels)
    out = [BOND_HEADER + list(labels)]
    for n, r in enumerate(results):
        if n in errors or isinstance(r, Exception):
            line = _blank_row(width)
            line[BOND_HEADER.index("Message")] = str(errors.get(n, r))
            out.append(line)
        elif r is None:
            out.append(_blank_row(width))
        else:
            deltas = _deltas_row(r.deltas, labels)
            out.append([
                r.maturity, r.coupon, r.simple_yield, r.close_date or "", r.clean, r.z_spread_bp, sum(deltas), "",
            ] + deltas)
    return out


TRADE_HEADER = ["Fix rate", "Par rate", "PV", "DV01", "Message"]


def _date_or_period(value):
    """日付(datetime・シリアル値)は日付に、それ以外(「5Y」等)は文字列のまま。"""
    if hasattr(value, "year") or (isinstance(value, (int, float)) and not isinstance(value, bool)):
        return _to_date(value)
    return str(value).strip()


def _trade_input(row) -> Optional[trades.Trade]:
    """[Type, Start, Tenor/End, Notional, Pay/Rec, Fix rate] → Trade(Typeが空の行はNone)。"""
    row = row + [None] * (6 - len(row))
    kind, start, tenor, notional, pay_rec, fix_rate = row[:6]
    if _is_blank(kind):
        return None
    if _is_blank(tenor):
        raise ValueError("Tenor/Endが空欄です")
    return trades.Trade(
        None if _is_blank(start) else _date_or_period(start), _date_or_period(tenor),
        _number(notional, "Notional"), pay_rec, _optional_number(fix_rate, "Fix rate"),
    )


@xw.func
@xw.arg("trade_table", ndim=2)
@xw.arg("pillars", ndim=1)
@xw.arg("real_time_range", ndim=2)
def PosTradeRisk(trade_table, pillars, real_time_range, valuation_date=None) -> List[list]:
    """
    日中の取引(標準のOIS)のピラーごとのデルタ(engine/position/trades.py)。
    trade_table: [Type, Start, Tenor/End, Notional(百万円), Pay/Rec, Fix rate(%)] の行(見出しなし)。
      Type: Done(約定済み)/ What-if(検討中)。グリッドでの集計はExcelがTypeで分ける。空欄の行は計算しない。
      Start: 空欄=スポット、日付、または期間(「5Y」=スポットから5年後に始まる先日付)。
      Tenor/End: 期間(「10Y」)か満期日。Fix rate: 空欄=パーレート。
    返り値: 見出し行 [Fix rate, Par rate, PV(百万円), DV01, Message, ピラー…] + 入力と同じ行数。
    """
    labels = _labels(pillars)
    parsed: List[Optional[trades.Trade]] = []
    errors = {}
    for n, row in enumerate(_rows(trade_table)):
        try:
            parsed.append(_trade_input(row))
        except ValueError as e:
            parsed.append(None)
            errors[n] = e
    curves = udf_common._market(real_time_range, valuation_date)
    results = trades.trade_risks(curves, parsed)
    width = len(TRADE_HEADER) + len(labels)
    out = [TRADE_HEADER + list(labels)]
    for n, r in enumerate(results):
        if n in errors or isinstance(r, Exception):
            line = _blank_row(width)
            line[TRADE_HEADER.index("Message")] = str(errors.get(n, r))
            out.append(line)
        elif r is None:
            out.append(_blank_row(width))
        else:
            deltas = _deltas_row(r.deltas, labels)
            out.append([r.fix_rate, r.par_rate, r.pv, sum(deltas), ""] + deltas)
    return out


@xw.func
@xw.arg("pillars", ndim=1)
@xw.arg("real_time_range", ndim=2)
def PosForwardJacobian(pillars, real_time_range, valuation_date=None) -> List[list]:
    """
    パーのピラー → フォワードの区間 の換算行列 J(engine/position/forward.py)。
    返り値: 見出し行 [Pillar, 区間のラベル…] + ピラーごとの行 [ピラー, J[ピラー][区間]…]。
    フォワードのデルタ = MMULT(TRANSPOSE(J), パーのデルタ) はExcelで計算する。
    """
    curves = udf_common._market(real_time_range, valuation_date)
    jac = forward.forward_jacobian(curves, _labels(pillars))
    return [["Pillar"] + jac.segments] + [[p] + row for p, row in zip(jac.pillars, jac.matrix)]


def _db_key() -> Optional[int]:
    store = HistoricalStore()
    return store.db_path.stat().st_mtime_ns if store.exists() else None


@lru_cache(maxsize=8)
def _pca_cached(labels: Tuple[str, ...], window: int, until, db_key) -> pca.PcaModel:
    return pca.estimate(load_history(SWAP), labels, _PCA_MODE, False, window, until, UNIT_BP[SWAP])


def _pca_pillars(pillars, use) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """(グリッドの全ピラー, PCAに使うピラー)。use: pillars と同じ長さのフラグ(0=使わない、空欄・1=使う)。"""
    rows = [(str(p).strip(), u) for p, u in zip(pillars or [], list(use or []) + [None] * len(pillars or []))
            if not _is_blank(p)]
    labels = tuple(p for p, _ in rows)
    used = tuple(p for p, u in rows if not udf_common._flag_excludes(u))
    return labels, used


def _pca(used: Tuple[str, ...], window, window_end) -> pca.PcaModel:
    n = _number(window, "window(営業日数)")
    if n != int(n):
        raise ValueError(f"window(営業日数)は整数です: {window!r}")
    return _pca_cached(used, int(n), _to_date(window_end) or _today(), _db_key())


@xw.func
@xw.arg("pillars", ndim=1)
@xw.arg("use", ndim=1)
def PosPcaLoadings(pillars, use, window, window_end=None) -> List[list]:
    """
    ピラーの日次変化(bp)の分散共分散行列の主成分ベクトル(単位ベクトル)。DBの引け値(スワップのミッド、
    O/NはTONA実績)のwindow営業日分。符号はPCA(SwapAnalysis)と同じ向き(PC1=全体が正、PC2=長い側が正)。
    use: ピラーごとのフラグ(0=PCAに使わない。DBの値が欠けがちなピラー(O/N=翌日公表、1W)を外す)。
      使わないピラーの行は0(そのピラーのデルタは主成分に乗らず、残差に入る)。
    返り値: 見出し行 [Pillar, PC1, …, PCm] + グリッドの全ピラーの行(m=使うピラーの数。Excel側でk本使う)。
    window_end: 推定の最終日(空欄=今日。DBに今日の値がなければ直近の営業日まで)。
    """
    labels, used = _pca_pillars(pillars, use)
    model = _pca(used, window, window_end)
    m = len(used)
    index = {p: i for i, p in enumerate(used)}
    return [["Pillar"] + [f"PC{j + 1}" for j in range(m)]] + [
        [p] + ([float(v) for v in model.vectors[index[p]]] if p in index else [0.0] * m) for p in labels
    ]


@xw.func
@xw.arg("pillars", ndim=1)
@xw.arg("use", ndim=1)
def PosPcaEigen(pillars, use, window, window_end=None) -> List[list]:
    """主成分ごとの日次の標準偏差(bp/日、=√固有値)。見出し行 [PC, Std] + 主成分ごとの行。
    寄与率・1σの損益はExcelで計算する。引数はPosPcaLoadingsと同じ。"""
    model = _pca(_pca_pillars(pillars, use)[1], window, window_end)
    return [["PC", "Std"]] + [[f"PC{j + 1}", float(v) ** 0.5] for j, v in enumerate(model.eigenvalues)]


@xw.func
@xw.arg("pillars", ndim=1)
@xw.arg("use", ndim=1)
def PosPcaWindow(pillars, use, window, window_end=None) -> List[list]:
    """推定に使ったwindowの情報(Start/End/Samples/Warning の2列)。引数はPosPcaLoadingsと同じ。"""
    model = _pca(_pca_pillars(pillars, use)[1], window, window_end)
    return [
        ["Start", model.start],
        ["End", model.end],
        ["Samples", model.n_samples],
        ["Warning", pca.gap_warning(model.end, _to_date(window_end) or _today())],
    ]


@xw.func
@xw.arg("pillars", ndim=1)
def PosCloseRates(pillars, as_of_date) -> List[list]:
    """DBのその日の引け値(%、ピラーの並びの縦1列。O/NはTONA実績、値が無いピラーは空欄)。
    日中の損益の説明で、会社のリスクの基準(前日引け)からのレートの変化を出すのに使う。"""
    d = _to_date(as_of_date)
    if d is None:
        raise ValueError("日付を入れてください(会社のリスクの基準日)")
    with udf_common._historical_store() as store:
        quotes = {normalize_label(k): v for k, v in store.quotes_on(d).items()}
        fixing = store.fixings().get(d)
    if not quotes:
        raise ValueError(f"{d}の引け値がDBにありません")
    if fixing is not None:
        quotes["O/N"] = fixing
    return [[quotes.get(normalize_label(p), "")] for p in _labels(pillars)]


@xw.func
@xw.arg("expiries", ndim=1)
@xw.arg("tails", ndim=1)
def PosCloseVols(expiries, tails, as_of_date) -> List[list]:
    """DBのその日のスワップションATMノーマルvol(bp/年)。行=満期、列=原資産(値が無ければ空欄)。"""
    d = _to_date(as_of_date)
    if d is None:
        raise ValueError("日付を入れてください(会社のリスクの基準日)")
    with udf_common._historical_store() as store:
        vols = {normalize_label(k): v for k, v in store.vol_quotes().get(d, {}).items()}
    if not vols:
        raise ValueError(f"{d}のvolがDBにありません")
    return [[vols.get(normalize_label(f"{e}x{t}"), "") for t in _labels(tails)] for e in _labels(expiries)]
