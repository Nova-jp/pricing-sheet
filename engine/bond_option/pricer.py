"""
国債店頭オプション(ヨーロピアン、現物受渡)の計算。先渡価格・先渡複利・先渡DD/MD・期間・Black式。

式は docs/bond_option.md の F6〜F10。ユーザーのプライサー(正)の4サンプルとプレミアム4桁まで一致。
利回り・レポ・価格ボラは%、単価は額面100円あたりの円。

■ QuantLibとの分担
  - 先渡複利・デュレーション(BondFunctions.bondYield/duration)、営業日数(ql.Japan())、
    Black式(ql.blackFormula)は QuantLib。
  - 先渡価格だけは自前の式。ql.BondForward は期中の利払を曲線のDFで割り引く
    (c(1+rT)/(1+r·tc))が、JGBのレポの慣行は c(1+r(T−tc))で、レポ20%・6Mで約0.0011円ずれる。
    スポット価格も曲線から計算する作りで、市場の単価を入れられない。

■ 期間と年率
  T = 営業日数(取引日→満期)/252、価格ボラ = bp vol(bp/日) × √252 × MD。
  標準偏差 = 価格ボラ × √T = bp vol × MD × √営業日数 なので、252はプレミアムには効かない。
"""

from datetime import date
from typing import NamedTuple

import QuantLib as ql

from engine.common.bond import NO_LEAP, Jgb, accrued_interest, coupon_dates, ql_bond, truncate3
from engine.common.conventions import CALENDAR

TRADING_DAYS = 252
CALL = "CALL"
PUT = "PUT"


class ForwardPrice(NamedTuple):
    clean: float
    dirty: float


class ForwardDuration(NamedTuple):
    compound_yield: float  # 先渡複利(%、小数3桁で切り捨て)
    dollar_duration: float  # DD = −dP/dy(額面100円、利回り1%あたり)
    modified_duration: float  # MD = DD / 先渡dirty × 100


def forward_price(
    bond: Jgb, spot_clean: float, spot_settle: date, forward_settle: date, repo: float,
) -> ForwardPrice:
    """F6: スポット単価をレポ(%、単利・暦日/365)で行使後の受渡日まで延ばす。
    dirty = (P0 + AI(s0)) × (1 + r × 暦日(s0→se)/365) − Σ c/2 × (1 + r × 暦日(利払日→se)/365)
    clean = dirty − AI(se)。丸めない。"""
    if forward_settle < spot_settle:
        raise ValueError(f"行使後の受渡日 {forward_settle} がスポット受渡日 {spot_settle} より前です")
    r = repo / 100
    dirty = (spot_clean + accrued_interest(bond, spot_settle)) * (
        1 + r * (forward_settle - spot_settle).days / 365
    )
    for d in coupon_dates(bond, spot_settle, forward_settle):
        dirty -= bond.coupon / 2 * (1 + r * (forward_settle - d).days / 365)
    return ForwardPrice(clean=dirty - accrued_interest(bond, forward_settle), dirty=dirty)


def _rate(compound_yield: float) -> ql.InterestRate:
    return ql.InterestRate(compound_yield / 100, NO_LEAP, ql.Compounded, ql.Semiannual)


def forward_duration(bond: Jgb, forward_dirty: float, forward_settle: date) -> ForwardDuration:
    """F7・F8: 先渡dirtyに一致する半年複利(うるう日除き/365、受渡日起点)を小数3桁で切り捨て、
    その利回りでの −dP/dy を DD、DD/先渡dirty を MD とする(分母はレポで計算した先渡dirty)。"""
    b = ql_bond(bond, "no_leap")
    settle = ql.Date.from_date(forward_settle)
    y = ql.BondFunctions.bondYield(
        b, ql.BondPrice(forward_dirty, ql.BondPrice.Dirty), NO_LEAP, ql.Compounded, ql.Semiannual,
        settle, 1.0e-12, 100, 0.02,
    )
    compound_yield = truncate3(y * 100)
    rate = _rate(compound_yield)
    dirty_at_yield = ql.BondFunctions.cleanPrice(b, rate, settle) + ql.BondFunctions.accruedAmount(b, settle)
    dd = ql.BondFunctions.duration(b, rate, ql.Duration.Modified, settle) * dirty_at_yield / 100
    return ForwardDuration(compound_yield, dd, dd / forward_dirty * 100)


def option_term(trade_date: date, expiry: date) -> float:
    """T = 営業日数(取引日→満期、ql.Japan())/252。"""
    if expiry <= trade_date:
        raise ValueError(f"満期 {expiry} が取引日 {trade_date} 以前です")
    days = CALENDAR.businessDaysBetween(ql.Date.from_date(trade_date), ql.Date.from_date(expiry))
    return days / TRADING_DAYS


def parse_option_type(value) -> int:
    text = str(value).strip().upper()
    if text in (CALL, "C"):
        return ql.Option.Call
    if text in (PUT, "P"):
        return ql.Option.Put
    raise ValueError(f"Call/Putのいずれかを指定してください: {value!r}")


def black_premium(option_type, forward: float, strike: float, price_vol: float, term: float) -> float:
    """F10: Black式(割引なし)。forward=先渡clean、strike=ストライク単価、price_vol=価格ボラ(%/年)、
    term=T。額面100円あたりの円を返す(銭はその×100)。"""
    if price_vol < 0 or term <= 0:
        raise ValueError(f"価格ボラは0以上、期間は正の値です: vol={price_vol}, T={term}")
    std_dev = price_vol / 100 * term ** 0.5
    return ql.blackFormula(parse_option_type(option_type), strike, forward, std_dev)
