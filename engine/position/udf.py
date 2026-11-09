"""
ポジション・リスク管理のブック(PositionSheet.xlsm)のUDF。

■ 設計方針(他のブックと同じ)
  - UDFにするのは、QuantLibが要る日中の取引のデルタ・ガンマと、DBの引け値の読み出しだけ。
    会社のリスクの抽出・今のデルタ(SOD + ガンマ × Move + 今日の取引)・合計はExcelの数式(四則演算)で計算する。
  - 計算方法は「手動」+ Shift+F9。UDFは非volatile。TODAY()は引数に使わない(評価日の空欄=今日)。
  - 配列はそのまま返し、Excelの動的配列のスピルに任せる(@xw.ret(expand=...)は使わない)。

■ 単位・符号
  想定元本・額面: 百万円。デルタ: 百万円/bp(金利が1bp上がったときのPVの変化)。ガンマ: 百万円/bp²。

■ 引数(共通)
  pillars: グリッドのピラーの縦の並び(短い順)。結果の行はこの並び。
  real_time_range: フラグ/Ticker/Value の範囲(Positionシートのピラーの行)。評価日の空欄=今日はライブ
    (他のブックと同じ udf_common._market)。
"""

from typing import List, Optional, Tuple

import xlwings as xw

from engine.common import udf_common
from engine.common.history import normalize_label
from engine.common.historical_store import HistoricalStore
from engine.common.udf_common import _is_blank, _to_date
from engine.position import bonds, intraday, trades


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


def _date_or_period(value):
    """日付(datetime・シリアル値)は日付に、それ以外(「5Y」等)は文字列のまま。"""
    if hasattr(value, "year") or (isinstance(value, (int, float)) and not isinstance(value, bool)):
        return _to_date(value)
    return str(value).strip()


def _db_key() -> Optional[int]:
    store = HistoricalStore()
    return store.db_path.stat().st_mtime_ns if store.exists() else None


@xw.func
@xw.arg("pillars", ndim=1)
def PosCloseRates(pillars, as_of_date) -> List[list]:
    """DBのその日の引け値(%、ピラーの並びの縦1列。O/NはTONA実績、値が無いピラーは空欄)。
    PositionシートのMove(今のレート − 会社のリスクの基準日の引け)の基準に使う。"""
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
    """DBのその日のスワップションATMノーマルvol(bp/年、営業日ベース)。行=満期、列=原資産(値が無ければ空欄)。
    Positionシートのボラの動き(今のvol − 会社のリスクの基準日の引け)の基準に使う。"""
    d = _to_date(as_of_date)
    if d is None:
        raise ValueError("日付を入れてください(会社のリスクの基準日)")
    with udf_common._historical_store() as store:
        vols = {normalize_label(k): v for k, v in store.vol_quotes().get(d, {}).items()}
    if not vols:
        raise ValueError(f"{d}のvolがDBにありません")
    return [[vols.get(normalize_label(f"{e}x{t}"), "") for t in _labels(tails)] for e in _labels(expiries)]

# ---------------------------------------------------------------------------
# 日中の取引(Positionシートの下段。OIS・現物)のデルタ・ガンマ(engine/position/intraday.py)
# ---------------------------------------------------------------------------

INTRADAY_HEADER = ["Rate", "PV", "DV01", "Gamma", "Message"]
GRID_HEADER = ["Delta Swap", "Delta Bond", "Gamma Swap", "Gamma Bond"]
_STATUS_FILTERS = {"DONE": (intraday.DONE,), "WHATIF": (intraday.WHAT_IF,), "ALL": intraday.STATUSES}

# 直近の計算結果(PosIntradayRiskとPosIntradayGridが同じ入力で1回だけ計算するため)。キーは取引の表・
# カーブのスナップショット・DBの更新時刻
_intraday_cache: dict = {}


def _intraday_input(row) -> Optional[intraday.IntradayTrade]:
    """[Status, Product, Start, Tenor/End, Code, Notional, Side, Rate] → IntradayTrade(Productが空の行はNone)。
    OIS: Start(空欄=スポット)・Tenor/End・Notional・Side(Pay/Rec)・Rate(固定金利%、空欄=パー)。
    Bond: Code・Notional(額面)・Side(Buy/Sell)・Rate(単利%、空欄=DBの引け値)。"""
    row = row + [None] * (8 - len(row))
    status, product, start, tenor, code, notional, side, rate = row[:8]
    if _is_blank(product):
        return None
    product = intraday.parse_product(product)
    status = intraday.parse_status(status)
    if product == intraday.OIS:
        if _is_blank(tenor):
            raise ValueError("Tenor/Endが空欄です")
        swap = trades.Trade(
            None if _is_blank(start) else _date_or_period(start), _date_or_period(tenor),
            _number(notional, "Notional"), side, _optional_number(rate, "Rate"),
        )
        trades.parse_pay_rec(side)
        return intraday.IntradayTrade(status, product, swap=swap)
    if _is_blank(code):
        raise ValueError("現物のCodeが空欄です")
    face = abs(_number(notional, "Notional")) * intraday.bond_sign(side)
    return intraday.IntradayTrade(
        status, product, bond=bonds.BondPosition(str(code), face, _optional_number(rate, "Rate")),
    )


def _freeze(rows) -> tuple:
    return tuple(tuple("" if v is None else v for v in r) for r in rows)


def _intraday(trade_table, real_time_range, valuation_date) -> List[object]:
    """取引の表の行ごとの IntradayRisk / 例外 / None(入力の行と同じ並び)。同じ入力なら前の結果を返す。"""
    rows = _rows(trade_table)
    curves = udf_common._market(real_time_range, valuation_date)
    key = (_freeze(rows), curves.snapshot, _db_key())
    if key in _intraday_cache:
        return _intraday_cache[key]
    parsed: List[Optional[intraday.IntradayTrade]] = []
    errors = {}
    for n, row in enumerate(rows):
        try:
            parsed.append(_intraday_input(row))
        except ValueError as e:
            parsed.append(None)
            errors[n] = e
    if any(t is not None and t.product == intraday.BOND for t in parsed):
        with udf_common._historical_store() as store:
            results = intraday.intraday_risks(curves, store, parsed)
    else:
        results = intraday.intraday_risks(curves, None, parsed)
    results = [errors.get(n, r) for n, r in enumerate(results)]
    _intraday_cache.clear()  # 直近1件だけ保持
    _intraday_cache[key] = results
    return results


@xw.func
@xw.arg("trade_table", ndim=2)
@xw.arg("pillars", ndim=1)
@xw.arg("real_time_range", ndim=2)
def PosIntradayRisk(trade_table, pillars, real_time_range, valuation_date=None) -> List[list]:
    """
    日中の取引の行ごとの要約(engine/position/intraday.py)。
    trade_table: [Status, Product, Start, Tenor/End, Code, Notional(百万円), Side, Rate(%)] の行(見出しなし)。
      Status: Done(約定済み)/ What-if(検討中)。Product: OIS / Bond。Productが空欄の行は計算しない。
    返り値: 見出し行 [Rate, PV, DV01, Gamma, Message] + 入力と同じ行数。
      Rate: 計算に使った金利(OISは固定金利、現物は単利)。PV: OISのPV(百万円、現物は空欄)。
      DV01・Gamma: pillarsのピラーのデルタ・ガンマの合計(百万円/bp、百万円/bp²)。
    """
    labels = _labels(pillars)
    out = [list(INTRADAY_HEADER)]
    for r in _intraday(trade_table, real_time_range, valuation_date):
        if isinstance(r, Exception):
            out.append(["", "", "", "", str(r)])
        elif r is None:
            out.append(_blank_row(len(INTRADAY_HEADER)))
        else:
            out.append([
                r.rate, "" if r.pv is None else r.pv,
                sum(_deltas_row(r.deltas, labels)), sum(_deltas_row(r.gammas, labels)), "",
            ])
    return out


@xw.func
@xw.arg("trade_table", ndim=2)
@xw.arg("pillars", ndim=1)
@xw.arg("real_time_range", ndim=2)
def PosIntradayGrid(trade_table, pillars, real_time_range, valuation_date=None, status="Done") -> List[list]:
    """
    日中の取引のピラーごとの合計(PositionシートのToday の列)。引数はPosIntradayRiskと同じ。
    status: Done(既定)/ What-if / All のどれの取引を足すか。エラーの行は足さない(PosIntradayRiskのMessageで確かめる)。
    返り値: 見出し行 [Delta Swap, Delta Bond, Gamma Swap, Gamma Bond] + pillarsの並びの行。
      Swap = OIS、Bond = 現物。カーブに使っていないピラーは0。
    """
    key = str(status or "Done").strip().upper().replace("-", "").replace(" ", "")
    if key not in _STATUS_FILTERS:
        raise ValueError(f"statusは Done / What-if / All のどれかです: {status!r}")
    wanted = _STATUS_FILTERS[key]
    labels = _labels(pillars)
    sums = {p: [0.0] * len(labels) for p in ("delta_" + intraday.OIS, "delta_" + intraday.BOND,
                                              "gamma_" + intraday.OIS, "gamma_" + intraday.BOND)}
    for r in _intraday(trade_table, real_time_range, valuation_date):
        if not isinstance(r, intraday.IntradayRisk) or r.status not in wanted:
            continue
        for measure, values in (("delta_", r.deltas), ("gamma_", r.gammas)):
            acc = sums[measure + r.product]
            for i, v in enumerate(_deltas_row(values, labels)):
                acc[i] += v
    columns = [sums["delta_" + intraday.OIS], sums["delta_" + intraday.BOND],
               sums["gamma_" + intraday.OIS], sums["gamma_" + intraday.BOND]]
    return [list(GRID_HEADER)] + [list(row) for row in zip(*columns)]
