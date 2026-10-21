"""
リスク(デルタ)計算モジュール。カーブは market.CurveSet(基準カーブとバンプしたカーブの一式、
全行で共有)から受け取り、ここではブートストラップしない。

- parallel_delta: カーブに使っている全テナーを動かしたときのPV差分(1bpあたり)
- bucketed_deltas / bucketed_delta: テナーごとに1本ずつ動かしたときのPV差分(1bpあたり、カーブに
  使っているテナーだけ)。基準カーブのクォートを1本ずつ一時的に動かして、全ポジションをまとめて評価する
- バンプ幅は market.BUMP_BP(0.1bp)で、PV差分をバンプ幅で割って1bpあたりにする(per_bp)
- annuity_delta: Notional × アニュイティ × 0.0001 の解析的近似値(参考値)
- solve_notional_for_target_delta: D列(目標delta)からE列(adj_notional)を逆算する

■ 重要: デルタは fix_rate を具体的な数値に固定してから計算する(resolve_params)
  pricing sheetの「fix rate」欄が空欄(=市場パーレートで組む)の場合、price_swap()に
  fix_rate=Noneを渡すとバンプ後のカーブの新パーレートに追従してしまい、PVが常に0 →
  deltaが常に0という無意味な結果になる。基準カーブでのパーレートを一度だけ求めて固定する。
"""

from dataclasses import replace
from typing import Dict, List, Optional, Sequence

from engine.common.market import BUMP_BP
from engine.common.market_data import Market
from engine.common.swap_pricer import SwapPricingResult, price_swap
from engine.common.terms import PAY, SwapParams


def price(market: Market, params: SwapParams) -> SwapPricingResult:
    return price_swap(
        market, params.terms, notional=params.notional, pay_rec=params.pay_rec, fix_rate=params.fix_rate,
    )


def resolve_params(curves, params: SwapParams) -> SwapParams:
    """fix_rateが空欄なら基準カーブのパーレートに固定したパラメータを返す。"""
    if params.fix_rate is not None:
        return params
    base = price(curves.base, params)
    return replace(params, fix_rate=base.fix_rate_used)


def per_bp(pv_change: float) -> float:
    """バンプ(market.BUMP_BP)でのPV差分 → 1bpあたりのデルタ。"""
    return pv_change / BUMP_BP


def parallel_delta(curves, params: SwapParams, base_pv: Optional[float] = None) -> float:
    """円(1bpあたり)。params.fix_rate は resolve_params で固定済みであること。
    base_pv: 同じポジションの基準カーブでのPV。渡せば評価し直さない。"""
    if base_pv is None:
        base_pv = price(curves.base, params).pv
    return per_bp(price(curves.parallel(), params).pv - base_pv)


def bucketed_deltas(curves, params_list: Sequence[SwapParams]) -> List[Dict[str, float]]:
    """ポジションごとの {テナーのラベル: 円(1bpあたり)}。params.fix_rate は resolve_params で固定済みであること。
    テナーごとにクォートを1回だけ動かし(カーブの引き直しはテナーの数だけ)、その間に全ポジションを
    評価する(RiskGridの全行)。"""
    base = [price(curves.base, p).pv for p in params_list]
    deltas: List[Dict[str, float]] = [{} for _ in params_list]
    for label in curves.bump_labels:
        with curves.bumped(label) as bumped:
            for n, p in enumerate(params_list):
                deltas[n][label] = per_bp(price(bumped, p).pv - base[n])
    return deltas


def bucketed_delta(curves, params: SwapParams) -> Dict[str, float]:
    """{テナーのラベル: 円(1bpあたり)}。1ポジション版(bucketed_deltas)。"""
    return bucketed_deltas(curves, [params])[0]


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
