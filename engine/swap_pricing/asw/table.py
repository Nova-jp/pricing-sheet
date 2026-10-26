"""
ASWPricer表(見出し行+銘柄の行)を1回でまとめて計算する(UDF AswPricingTable の本体)。

pricing表(pricing/table.py)と同じ作り: 列は見出し名で探し、flag=1の行だけ計算し、行ごとのエラーは
message列に閉じ込める。計算の定義は asw/pricer.py。

■ 1行の流れ
  銘柄(JB380)→ DBの銘柄情報(利率・償還日)と評価日以前で直近の引け値(JCL、単利)
  → 単利 = 引け値+スライド(bp)/100(yield overrideがあればその値)→ 単価・複利(スポット T+1)
  → MMS(起算日=start、空欄ならスポット T+2。満期日=償還日。規約は行の fix freq 等)
  → par-par ASW、目標delta(百万円)があればMMSのnotional(アニュイティデルタ基準、pricing表と同じ)
  ASW(bp) = (複利 − MMS)×100 はシートの式で計算する。
"""

from typing import List, Sequence

from engine.common import bond, udf_common
from engine.common.swap_pricer import swap_dates
from engine.common.terms import SwapConvention, SwapTerms
from engine.swap_pricing.asw import pricer
from engine.swap_pricing.pricing import measures
from engine.swap_pricing.pricing.row import start_date
from engine.swap_pricing.pricing.table import columns, is_flagged

INPUT_COLUMNS = (
    "flag", "bond", "slide", "yield override", "delta", "start", "fix freq", "fix dcf", "float freq",
    "roll conv", "pay/rec",
)
OUTPUT_HEADER = (
    "coupon", "maturity", "close", "close date", "simple yield", "settle", "price", "compound yield",
    "start date", "end date", "swap rate", "par-par asw", "adj_notional", "message",
)
_OUT = {name: i for i, name in enumerate(OUTPUT_HEADER)}


def _number(value, name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name}が数値ではありません: {value!r}") from None


def _simple_yield(get, close) -> float:
    if not udf_common._is_blank(get("yield override")):
        return _number(get("yield override"), "yield override")
    if close is None:
        raise ValueError("引け値がDBにありません(yield overrideに単利を入れるか、朝のバッチを実行してください)")
    slide = 0.0 if udf_common._is_blank(get("slide")) else _number(get("slide"), "slide")
    return close.simple_yield + slide / 100


def price_row(row: Sequence, col, curves, store) -> list:
    get = lambda name: row[col[name]]  # noqa: E731
    result = [""] * len(OUTPUT_HEADER)
    put = lambda name, value: result.__setitem__(_OUT[name], value)  # noqa: E731
    if not is_flagged(get("flag")):
        return result
    try:
        code = str(get("bond")).strip().upper()
        info = store.jgb_bond(code)
        if info is None:
            raise ValueError(f"銘柄 {code} がDBにありません")
        jgb = bond.Jgb(info.coupon, info.maturity)
        put("coupon", info.coupon)
        put("maturity", info.maturity)
        close = store.jgb_close(code, curves.valuation_date)
        if close is not None:
            put("close", close.simple_yield)
            put("close date", close.as_of_date)
        spot = pricer.spot_bond(jgb, _simple_yield(get, close), curves.valuation_date)
        for name, value in (
            ("simple yield", spot.simple_yield), ("settle", spot.settle), ("price", spot.price),
            ("compound yield", spot.compound_yield),
        ):
            put(name, value)

        start = curves.spot_date if udf_common._is_blank(get("start")) else start_date(get("start"), curves)
        terms = SwapTerms(
            start, SwapConvention(*(get(c) for c in ("fix freq", "fix dcf", "float freq", "roll conv"))),
            end=info.maturity,
        )
        par = measures.par(curves, terms)
        swap_start, swap_end = swap_dates(terms)
        put("start date", swap_start)
        put("end date", swap_end)
        put("swap rate", measures.fair_rate(curves, terms, par))
    except Exception as e:
        put("message", str(e))
        return result

    errors = []
    try:
        put("par-par asw", pricer.par_par_spread_bp(curves.base, jgb, spot, terms))
    except Exception as e:
        errors.append(f"par-par asw: {e}")
    if not udf_common._is_blank(get("delta")):
        try:
            put("adj_notional", measures.notional_for_delta(curves, terms, get("delta"), get("pay/rec"), par))
        except Exception as e:
            errors.append(f"adj_notional: {e}")
    put("message", " / ".join(errors))
    return result


def price_table(table: Sequence[Sequence], curves, store) -> List[list]:
    """table: 見出し行+銘柄の行。見出し行+行ごとの結果(行の並びは入力と同じ)を返す。
    store: 銘柄情報・引け値を読むヒストリカルDB(開いたもの)。"""
    rows = [list(r) if r is not None else [] for r in (table or [])]
    if not rows:
        raise ValueError("ASWPricer表が空です")
    col = columns(rows[0], INPUT_COLUMNS)
    width = len(rows[0])
    return [list(OUTPUT_HEADER)] + [price_row(r + [None] * (width - len(r)), col, curves, store) for r in rows[1:]]
