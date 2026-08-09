"""
リスク(デルタ)計算モジュール。

- parallel_bump_delta: 全テナーのレートを+1bpして再ブートストラップ・再評価した際のPV差分
- bucketed_delta: テナーごとに1本ずつ+1bpして再評価(合計はparallel_bump_deltaに近似するはず)
- annuity_delta: Notional × アニュイティ × 0.0001 の解析的近似値(検算用の参考値)
- solve_notional_for_target_delta: D列(目標delta)からE列(adj_notional)を逆算する

■ 重要: params.fix_rate は必ず具体的な数値に解決してから渡すこと
  pricing sheetの「fix rate」欄が空欄(=市場パーレートで組む)の場合、
  price_swap()にfix_rate=Noneを渡すとその都度の再ブートストラップ後の
  新パーレートに自動的に追従してしまい、PVが常に0 → deltaが常に0という
  無意味な結果になる。正しくは、基準(バンプ前)カーブでのtarget_fixrateを
  一度だけ計算し、その数値をfix_rateとして固定した上でbump系の関数に渡す。
"""

from datetime import date
from typing import Dict, NamedTuple, Optional

from swap_pricing.curve import bootstrap_curve
from swap_pricing.swap_pricer import PAY, price_swap


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


def _pv(spot_rates: Dict[str, float], valuation_date: date, params: SwapParams) -> float:
    curve = bootstrap_curve(valuation_date, spot_rates).curve
    result = price_swap(
        curve,
        valuation_date,
        params.start,
        params.fix_freq,
        params.fix_dcf,
        params.float_freq,
        params.float_dcf,
        params.roll_conv,
        params.notional,
        params.pay_rec,
        tenor=params.tenor,
        end=params.end,
        fix_rate=params.fix_rate,
    )
    return result.pv


def parallel_bump_delta(
    spot_rates: Dict[str, float], valuation_date: date, params: SwapParams, bump_bp: float = 1.0
) -> float:
    pv_base = _pv(spot_rates, valuation_date, params)
    bumped = {k: (v + bump_bp / 100.0 if v is not None else v) for k, v in spot_rates.items()}
    pv_bumped = _pv(bumped, valuation_date, params)
    return pv_bumped - pv_base


def bucketed_delta(
    spot_rates: Dict[str, float], valuation_date: date, params: SwapParams, bump_bp: float = 1.0
) -> Dict[str, float]:
    pv_base = _pv(spot_rates, valuation_date, params)
    deltas = {}
    for tenor, rate in spot_rates.items():
        if rate is None:
            continue
        bumped = dict(spot_rates)
        bumped[tenor] = rate + bump_bp / 100.0
        pv_bumped = _pv(bumped, valuation_date, params)
        deltas[tenor] = pv_bumped - pv_base
    return deltas


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
