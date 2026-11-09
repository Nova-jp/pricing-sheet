"""
国債店頭オプション(ヨーロピアン、現物受渡)の計算。先渡価格・先渡複利・先渡DD/MD・期間・Black式・
ITMバイライトのストライク。

式は docs/bond_option.md の F6〜F11。ユーザーのプライサー(正)の4サンプルとプレミアム4桁まで一致。
利回り・レポ・価格ボラは%、単価は額面100円あたりの円。
BondOption.xlsmでは、先渡価格(F6)と期間はExcelの式で同じ計算をする(手で値を上書きしても動くように)。
forward_price(F6、実体は engine/common/bond.py。国債先物のベーシスと共用)・option_term は、
サンプルとの照合(テスト)とシートの式の確認に使う。

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

from engine.common.bond import NO_LEAP, ForwardPrice, Jgb, dollar_duration, forward_price, ql_bond, truncate3  # noqa: F401
from engine.common.bond import compound_yield as bond_compound_yield
from engine.common.conventions import CALENDAR

TRADING_DAYS = 252
CALL = "CALL"
PUT = "PUT"


class ForwardDuration(NamedTuple):
    compound_yield: float  # 先渡複利(%、小数3桁で切り捨て)
    dollar_duration: float  # DD = −dP/dy(額面100円、利回り1%あたり)
    modified_duration: float  # MD = DD / 先渡dirty × 100


def forward_duration(bond: Jgb, forward_dirty: float, forward_settle: date) -> ForwardDuration:
    """F7・F8: 先渡dirtyに一致する半年複利(うるう日除き/365、受渡日起点)を小数3桁で切り捨て、
    その利回りでの −dP/dy を DD、DD/先渡dirty を MD とする(分母はレポで計算した先渡dirty)。"""
    compound_yield = bond_compound_yield(bond, forward_dirty, forward_settle)
    dd = dollar_duration(bond, compound_yield, forward_settle)
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


class BlackGreeks(NamedTuple):
    premium: float  # 額面100円あたりの円(F10)
    delta: float  # ∂プレミアム/∂先渡clean(コールは0〜1、プットは−1〜0)
    gamma: float  # ∂²プレミアム/∂先渡clean²(1円あたり)
    vega: float  # 価格ボラ1%(%/年)あたりのプレミアムの変化(円)
    theta: float  # 1営業日(T − 1/252、先渡・ボラは同じ)でのプレミアムの変化(円、ふつうマイナス)


def black_greeks(option_type, forward: float, strike: float, price_vol: float, term: float) -> BlackGreeks:
    """F10のBlack式(割引なし)のグリークス(ql.BlackCalculator)。引数は black_premium と同じ。
    セータは期間だけを1営業日縮めた値との差(満期まで1営業日以内なら本源的価値との差)。"""
    if price_vol < 0 or term <= 0:
        raise ValueError(f"価格ボラは0以上、期間は正の値です: vol={price_vol}, T={term}")
    kind = parse_option_type(option_type)
    calc = ql.BlackCalculator(ql.PlainVanillaPayoff(kind, strike), forward, price_vol / 100 * term ** 0.5, 1.0)
    premium = calc.value()
    later = max(term - 1 / TRADING_DAYS, 0.0)
    after = ql.blackFormula(kind, strike, forward, price_vol / 100 * later ** 0.5) if later > 0 else (
        max(forward - strike, 0.0) if kind == ql.Option.Call else max(strike - forward, 0.0))
    return BlackGreeks(premium, calc.deltaForward(), calc.gammaForward(), calc.vega(term) / 100, after - premium)


def itm_buy_write_strike(forward: float, target: float, price_vol: float, term: float) -> float:
    """F11: ITMバイライト(債券を target=購入単価で買い、同時にコールを売る)のストライク。
    ストライク + コールのプレミアム = target になるKを解き、小数3桁で切り捨てる。
    f(K) = K + C(K) はKについて単調増加(傾き 1 − N(d2) > 0)で、K→0 で先渡価格F、K = target で
    target + C ≥ target なので、解があるのは target > F(キャリーが正)のときだけ。解は通常 K < F(ITM)。"""
    if target <= forward:
        raise ValueError(
            f"購入単価 {target} が先渡価格 {forward} 以下のため、ストライク+プレミアム=購入単価になる"
            "ストライクがありません(キャリーが負)"
        )
    black_premium(CALL, forward, target, price_vol, term)  # 入力の確認(ボラ・期間)

    def excess(k: float) -> float:
        return k + black_premium(CALL, forward, k, price_vol, term) - target

    return truncate3(ql.Brent().solve(excess, 1.0e-10, (forward + target) / 2, 0.0, target))
