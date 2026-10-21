"""
JGB(固定利付国債)の市場の規約。単利⇔単価・経過利子・利払日・受渡日。

現物オプション(engine/bond_option/)が使う。規則はユーザーのプライサー(正)と照合して特定した
(docs/bond_option.md)。利回り・利率は%、単価は額面100円あたりの円で扱う。
BondOption.xlsmでは、単利⇔単価・経過利子・受渡日はExcelの式(名前付きLAMBDAとWORKDAY等)で同じ計算をする
(手で値を上書きしても動くように)。ここの関数は、サンプルとの照合(テスト)とシートの式の確認に使う。

■ QuantLibとの分担
  - 日数(うるう日除き)・経過利子・利払日・営業日は QuantLib(ql.Actual365Fixed(NoLeap)、
    FixedRateBond(Actual365Fixed).accruedAmount、ql.Schedule、ql.Japan())。
  - 単利⇔単価だけは自前の式。QuantLibの ql.Simple の利回りは各キャッシュフローを 1/(1+y·t) で
    割り引く別の定義で、JGBの単利 (c + (100−P)/n)/P に当たるものが無いため(確認済み。
    10年債で約0.12%違う)。

■ 利払日
  償還日から半年ごとに遡った名目の日付(休日調整なし)。初回の利払期間が変則な銘柄
  (例 JN487: 発行日8/3、初回利払日2027/2/1)も名目の利払日(8/1)から経過利子を数える
  (LSEGの経過利子と一致)。発行日より前の利払期間もスケジュールに含むが、受渡日より前の
  キャッシュフローは計算に使われない。

■ 端数処理
  単価・単利は小数3桁で切り捨て(市場の規則)。負の値は0に近い側に切る(ExcelのROUNDDOWNと同じ。
  負の利回りの市場の慣行は未確認で、暫定。docs/roadmap.mdの14 #4)。日数は「うるう日(2/29)を除く日数/365」。
"""

import math
from datetime import date
from functools import lru_cache
from typing import List, NamedTuple

import QuantLib as ql

from engine.common.conventions import CALENDAR

NO_LEAP = ql.Actual365Fixed(ql.Actual365Fixed.NoLeap)
ACCRUAL = ql.Actual365Fixed()  # 経過利子は暦日/365(うるう日を含む)
_SCHEDULE_YEARS = 41  # 償還日から遡ってスケジュールを作る年数(40年債+余裕)


class Jgb(NamedTuple):
    coupon: float  # 表面利率(%)
    maturity: date


def truncate3(x: float) -> float:
    """小数3桁で切り捨て(0に近い側、ExcelのROUNDDOWN)。x*1000 の浮動小数点の誤差(91.30099999…)で
    1つ下がらないように丸めてから切る。"""
    return math.trunc(round(x * 1000, 6)) / 1000


def _qd(d: date) -> ql.Date:
    return ql.Date.from_date(d)


def years_to_maturity(bond: Jgb, start: date) -> float:
    """n = うるう日を除く日数(start→償還日)/365。"""
    if start >= bond.maturity:
        raise ValueError(f"起点 {start} が償還日 {bond.maturity} 以降です")
    return NO_LEAP.dayCount(_qd(start), _qd(bond.maturity)) / 365


def price_from_simple_yield(bond: Jgb, simple_yield: float, start: date) -> float:
    """単利(%) → 単価。P = (c + 100/n) / (y + 100/n) × 100 を小数3桁で切り捨て。"""
    n = years_to_maturity(bond, start)
    return truncate3(100 * (bond.coupon + 100 / n) / (simple_yield + 100 / n))


def simple_yield_from_price(bond: Jgb, price: float, start: date) -> float:
    """単価 → 単利(%)。y = (c + (100 − P)/n) / P × 100 を小数3桁で切り捨て。"""
    n = years_to_maturity(bond, start)
    return truncate3((bond.coupon + (100 - price) / n) / price * 100)


@lru_cache(maxsize=1024)
def schedule(bond: Jgb) -> ql.Schedule:
    """償還日から半年ごとに遡る利払日(休日調整なし)。"""
    maturity = _qd(bond.maturity)
    start = maturity - ql.Period(12 * _SCHEDULE_YEARS, ql.Months)
    return ql.Schedule(
        start, maturity, ql.Period(ql.Semiannual), ql.NullCalendar(), ql.Unadjusted, ql.Unadjusted,
        ql.DateGeneration.Backward, False,
    )


@lru_cache(maxsize=1024)
def ql_bond(bond: Jgb, day_counter_name: str = "accrual") -> ql.FixedRateBond:
    """QuantLibの債券(額面100、受渡日は引数で渡すので settlementDays=0)。
    day_counter_name: "accrual"=経過利子用(暦日/365)、"no_leap"=複利・デュレーション用(うるう日除き/365)。"""
    dc = ACCRUAL if day_counter_name == "accrual" else NO_LEAP
    return ql.FixedRateBond(0, 100.0, schedule(bond), [bond.coupon / 100], dc)


def accrued_interest(bond: Jgb, settle: date) -> float:
    """経過利子 = 利率 × 暦日(前回利払日→受渡日)/365。"""
    return ql.BondFunctions.accruedAmount(ql_bond(bond), _qd(settle))


def coupon_dates(bond: Jgb, after: date, until: date) -> List[date]:
    """after < 利払日 ≤ until の利払日(名目の日付)。"""
    return [d.to_date() for d in schedule(bond) if _qd(after) < d <= _qd(until)]


def compound_yield(bond: Jgb, dirty: float, settle: date) -> float:
    """利含み単価 dirty に一致する半年複利(%、うるう日除き/365、受渡日起点)を小数3桁で切り捨てる。
    現物オプションの先渡複利(先渡dirty・行使後の受渡日)とアセットスワップの複利(スポット)が使う。"""
    y = ql.BondFunctions.bondYield(
        ql_bond(bond, "no_leap"), ql.BondPrice(dirty, ql.BondPrice.Dirty), NO_LEAP, ql.Compounded,
        ql.Semiannual, _qd(settle), 1.0e-12, 100, 0.02,
    )
    return truncate3(y * 100)


def settlement_date(trade_date: date, days: int) -> date:
    """取引日・行使日の T+days(日本の営業日)。スポット受渡は T+1。"""
    return CALENDAR.advance(_qd(trade_date), int(days), ql.Days).to_date()
