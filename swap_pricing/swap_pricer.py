"""
金利スワップのプライシングエンジン。

前提: ディスカウントカーブとフォーキャストカーブが同一(単一カーブ、
JSCC TONA OISのみ対応)。変動脚のフォワードレートは同じカーブから
単純フォワード(DF(start)/DF(end)-1)/dcfで算出する。

■ 主要な出力
  - annuity: 固定脚のアニュイティ(Σ DF×dcf)
  - target_fixrate: パーレート(%) = 変動脚PV / アニュイティ
  - PV: 入力された固定金利(またはパーレート)でのスワップ時価
"""

from datetime import date
from typing import NamedTuple, Optional

from swap_pricing.daycount import year_fraction
from swap_pricing.monotone_convex import MonotoneConvexCurve
from swap_pricing.schedule import generate_schedule

PAY = "PAY"
REC = "REC"


class SwapPricingResult(NamedTuple):
    annuity: float
    float_leg_pv: float
    target_fixrate: float  # % 表記
    fix_rate_used: float  # % 表記(入力がNoneならtarget_fixrateと同じ)
    pv: float
    telescoping_check_diff: float  # 検算: 個別積算とテレスコーピング公式の差(float_leg_pv単位)
    maturity_date: date  # tenorから計算した場合の満期日(endを直接指定した場合はend)


def _df(curve: MonotoneConvexCurve, valuation_date: date, d: date) -> float:
    t = year_fraction(valuation_date, d, "act/365fixed")
    return curve.discount_factor(t)


def price_swap(
    curve: MonotoneConvexCurve,
    valuation_date: date,
    start: date,
    fix_freq: str,
    fix_dcf: str,
    float_freq: str,
    float_dcf: str,
    roll_conv: str,
    notional: float,
    pay_rec: str,
    tenor: Optional[str] = None,
    end: Optional[date] = None,
    fix_rate: Optional[float] = None,
) -> SwapPricingResult:
    if pay_rec not in (PAY, REC):
        raise ValueError(f"pay_recは'{PAY}'または'{REC}'である必要があります: {pay_rec!r}")

    fixed_periods = generate_schedule(start, fix_freq, roll_conv, fix_dcf, tenor=tenor, end=end)
    float_periods = generate_schedule(start, float_freq, roll_conv, float_dcf, tenor=tenor, end=end)

    annuity = sum(
        _df(curve, valuation_date, p.payment_date) * p.dcf for p in fixed_periods
    )

    # 変動脚PV(単位ノーショナルあたり): 個別期間の積算(一般形、検算対象)
    float_leg_pv_sum = 0.0
    for p in float_periods:
        df_start = _df(curve, valuation_date, p.accrual_start)
        df_end = _df(curve, valuation_date, p.accrual_end)
        fwd = (df_start / df_end - 1.0) / p.dcf
        float_leg_pv_sum += df_end * p.dcf * fwd

    # テレスコーピング公式(単一カーブなので理論上完全一致するはず)
    df_leg_start = _df(curve, valuation_date, float_periods[0].accrual_start)
    df_leg_end = _df(curve, valuation_date, float_periods[-1].accrual_end)
    float_leg_pv_telescoping = df_leg_start - df_leg_end

    float_leg_pv = float_leg_pv_telescoping
    telescoping_check_diff = float_leg_pv_sum - float_leg_pv_telescoping

    target_fixrate = float_leg_pv / annuity * 100.0

    fix_rate_used = target_fixrate if fix_rate is None else fix_rate

    sign = 1.0 if pay_rec == PAY else -1.0
    pv = sign * notional * annuity * (target_fixrate / 100.0 - fix_rate_used / 100.0)

    return SwapPricingResult(
        annuity=annuity,
        float_leg_pv=float_leg_pv,
        target_fixrate=target_fixrate,
        fix_rate_used=fix_rate_used,
        pv=pv,
        telescoping_check_diff=telescoping_check_diff,
        maturity_date=fixed_periods[-1].payment_date,
    )
