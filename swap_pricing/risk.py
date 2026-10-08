"""
リスク(デルタ)計算モジュール。カーブは market.CurveSet(基準カーブと+1bpしたカーブの一式、
全行で共有)から受け取り、ここではブートストラップしない。

- parallel_delta: カーブに使っている全テナーを+1bpしたときのPV差分
- bucketed_delta: テナーごとに1本ずつ+1bpしたときのPV差分(カーブに使っているテナーだけ)
- annuity_delta: Notional × アニュイティ × 0.0001 の解析的近似値(参考値)
- solve_notional_for_target_delta: D列(目標delta)からE列(adj_notional)を逆算する

■ 重要: デルタは fix_rate を具体的な数値に固定してから計算する(resolve_params)
  pricing sheetの「fix rate」欄が空欄(=市場パーレートで組む)の場合、price_swap()に
  fix_rate=Noneを渡すとバンプ後のカーブの新パーレートに追従してしまい、PVが常に0 →
  deltaが常に0という無意味な結果になる。基準カーブでのパーレートを一度だけ求めて固定する。
"""

from datetime import date
from typing import Dict, NamedTuple, Optional

from swap_pricing.curve import BootstrapResult
from swap_pricing.swap_pricer import PAY, SwapPricingResult, price_swap


class SwapParams(NamedTuple):
    start: date
    fix_freq: str
    fix_dcf: str
    float_freq: str
    float_dcf: str
    roll_conv: str
    notional: float
    pay_rec: str
    tenor: Optional[str] = None
    end: Optional[date] = None
    fix_rate: Optional[float] = None
    fixings: Optional[Dict[date, float]] = None  # 過去起算時のTONA実績 {日付: %}


def price(boot: BootstrapResult, valuation_date: date, params: SwapParams) -> SwapPricingResult:
    return price_swap(
        boot.curve, valuation_date, params.start, params.fix_freq, params.fix_dcf,
        params.float_freq, params.float_dcf, params.roll_conv, params.notional, params.pay_rec,
        tenor=params.tenor, end=params.end, fix_rate=params.fix_rate, index=boot.index,
        fixings=params.fixings,
    )


def resolve_params(curves, params: SwapParams) -> SwapParams:
    """fix_rateが空欄なら基準カーブのパーレートに固定したパラメータを返す。"""
    if params.fix_rate is not None:
        return params
    base = price(curves.base, curves.valuation_date, params)
    return params._replace(fix_rate=base.fix_rate_used)


def parallel_delta(curves, params: SwapParams) -> float:
    """円。params.fix_rate は resolve_params で固定済みであること。"""
    base = price(curves.base, curves.valuation_date, params).pv
    return price(curves.parallel(), curves.valuation_date, params).pv - base


def bucketed_delta(curves, params: SwapParams) -> Dict[str, float]:
    """{テナーのラベル: 円}。params.fix_rate は resolve_params で固定済みであること。"""
    base = price(curves.base, curves.valuation_date, params).pv
    return {
        label: price(curves.bucket(label), curves.valuation_date, params).pv - base
        for label in curves.bump_labels
    }


def annuity_delta(annuity: float, notional: float, pay_rec: str, bump_bp: float = 1.0) -> float:
    sign = 1.0 if pay_rec == PAY else -1.0
    return sign * notional * annuity * (bump_bp / 10000.0)


def solve_notional_for_target_delta(
    target_delta: float, annuity: float, pay_rec: str, bump_bp: float = 1.0
) -> float:
    """D列(目標delta)からE列(adj_notional)を逆算する(アニュイティデルタ基準)。"""
    per_unit_delta = annuity_delta(annuity, notional=1.0, pay_rec=pay_rec, bump_bp=bump_bp)
    if per_unit_delta == 0:
        raise ValueError("annuityが0のため、目標deltaからnotionalを逆算できません")
    return target_delta / per_unit_delta
