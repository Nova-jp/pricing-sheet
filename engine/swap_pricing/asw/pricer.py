"""
アセットスワップ(ASWPricerシート)の計算。現物の複利・MMS(マッチド・マチュリティ・スワップ)・par-par ASW。

■ 定義(Neonの ASW_data と同じ。書き込み元は別プロジェクト market-analysis の QuantLibHelper.calculate_mms)
  ASW = 現物の複利 − MMS。引き算はExcelの式で行い、ここでは複利とMMSを返す。
  - 現物の複利: 引け値(大和 =DWAT)の単利+スライドの単利 → 単価(スポット受渡 T+1、小数3桁で切り捨て)→
    利含み単価に一致する半年複利(うるう日除き/365、小数3桁で切り捨て。bond.compound_yield。
    Neon側はJSDAの平均値の複利で、同じく小数3桁)。
  - MMS: 起算日=スポット(T+2)、満期日=償還日(Modified Following)、償還日から遡る日程(roll conv=BWD)、
    固定側 Act/365・PA/SA、変動側も同じ頻度のOISのパーレート(swap_pricerの経路。PricingSheetの
    パーレートと同じカーブ)。PA/SAの差がNeonと0.01bpの範囲で一致することを確認済み(2026-09-25のDB)。
■ par-par ASW(参考値)
  額面で現物を買い(利含み単価を払う)、現物のクーポン・償還を払ってTONA+スプレッドを受けるスワップの
  スプレッド(bp)。QuantLibの ql.AssetSwap(parAssetSwap=True)の fairSpread。変動側の日程はMMSと同じ
  起算日・満期日・頻度・roll conv。
"""

from datetime import date
from typing import NamedTuple

import QuantLib as ql

from engine.common import bond
from engine.common.conventions import CALENDAR, FREQ_TO_QL_FREQUENCY, rule_and_eom
from engine.common.market_data import Market
from engine.common.terms import SwapTerms

SPOT_SETTLEMENT_DAYS = 1  # 現物のスポット受渡(T+1)


class SpotBond(NamedTuple):
    settle: date
    simple_yield: float  # %
    price: float  # 単価(額面100円あたり)
    accrued: float
    compound_yield: float  # 半年複利(%、小数3桁)


def spot_bond(jgb: bond.Jgb, simple_yield: float, valuation_date: date) -> SpotBond:
    """評価日のスポット受渡(T+1)での単価・複利。"""
    settle = bond.settlement_date(valuation_date, SPOT_SETTLEMENT_DAYS)
    if settle >= jgb.maturity:
        raise ValueError(f"受渡日 {settle} が償還日 {jgb.maturity} 以降です")
    price = bond.price_from_simple_yield(jgb, simple_yield, settle)
    accrued = bond.accrued_interest(jgb, settle)
    return SpotBond(settle, simple_yield, price, accrued, bond.compound_yield(jgb, price + accrued, settle))


def _asset_swap_bond(jgb: bond.Jgb) -> ql.FixedRateBond:
    """ql.AssetSwap用の債券。利払日は bond.schedule と同じ名目の日付(休日調整なし)だが、受渡日を
    日本の営業日で数えるためカレンダーは ql.Japan() にする(bond.schedule は NullCalendar で、T+1 が
    土曜になる)。支払日は翌営業日(Following)。"""
    maturity = ql.Date.from_date(jgb.maturity)
    schedule = ql.Schedule(
        maturity - ql.Period(12 * bond._SCHEDULE_YEARS, ql.Months), maturity, ql.Period(ql.Semiannual),
        CALENDAR, ql.Unadjusted, ql.Unadjusted, ql.DateGeneration.Backward, False,
    )
    return ql.FixedRateBond(SPOT_SETTLEMENT_DAYS, 100.0, schedule, [jgb.coupon / 100], bond.ACCRUAL)


def par_par_spread_bp(market: Market, jgb: bond.Jgb, spot: SpotBond, terms: SwapTerms) -> float:
    """par-par ASWのスプレッド(bp)。terms: MMSと同じ取引条件(変動側の日程に使う)。"""
    if terms.start < market.valuation_date:
        raise ValueError(f"par-par ASWは起算日 {terms.start} が評価日より前だと計算できません")
    ql.Settings.instance().evaluationDate = ql.Date.from_date(market.valuation_date)
    conv = terms.convention
    rule, end_of_month = rule_and_eom(conv.roll_conv)
    float_schedule = ql.Schedule(
        ql.Date.from_date(terms.start), ql.Date.from_date(terms.end), ql.Period(FREQ_TO_QL_FREQUENCY[conv.float_freq]),
        CALENDAR, ql.ModifiedFollowing, ql.ModifiedFollowing, rule, end_of_month,
    )
    qb = _asset_swap_bond(jgb)
    if qb.settlementDate().to_date() != spot.settle:
        raise ValueError(f"QuantLibの受渡日 {qb.settlementDate().to_date()} が {spot.settle} と違います")
    swap = ql.AssetSwap(
        True, qb, spot.price, market.index, 0.0, float_schedule, market.index.dayCounter(), True,
    )
    swap.setPricingEngine(ql.DiscountingSwapEngine(market.curve))
    return swap.fairSpread() * 1e4
