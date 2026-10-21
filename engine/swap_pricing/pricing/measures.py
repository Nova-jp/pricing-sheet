"""
pricingシートの1行の計算結果(列)ごとの関数。1行用のUDF(FairRate等)と表全体のUDF(PricingTable)は
どちらもこれらを組み合わせるだけにする。値はすべてQuantLib(risk.price)の結果をそのまま使う。

■ 評価の使い回し
  パーレートとアニュイティは規約と日付だけで決まる(想定元本1・固定金利なしで評価する。par)。
  par の結果を渡すと、fair_rate・annuity_delta_mm・notional_for_delta・固定金利が空欄の行のPV/デルタが
  評価をやり直さない。PricingTable はさらにPVを parallel_delta_mm の base_pv に渡し、1行あたりの
  評価を3回(par・PV・パラレル)にしている。渡さなければ各関数が自分で評価する(1行用のUDF)。

■ 固定金利が空欄の行
  PVとデルタはパーレートに固定して計算する(fixed)。バンプ後のカーブのパーレートに追従させると
  PVが常に0、デルタも常に0になるため(risk.pyのdocstring参照)。
"""

from dataclasses import replace
from typing import Optional

from engine.common.risk import annuity_delta, parallel_delta, price, solve_notional_for_target_delta
from engine.common.swap_pricer import SwapPricingResult
from engine.common.terms import PAY, SwapParams, SwapTerms

_MM = 1_000_000.0  # デルタの表示単位(百万円)


def par(curves, terms: SwapTerms) -> SwapPricingResult:
    """想定元本1・固定金利なしの評価(target_fixrateとannuity=想定元本1あたりを使う)。"""
    return price(curves.base, SwapParams(terms, 1.0, PAY))


def fair_rate(curves, terms: SwapTerms, par_result: Optional[SwapPricingResult] = None) -> float:
    """パーレート(%)。"""
    return (par_result or par(curves, terms)).target_fixrate


def fixed(curves, params: SwapParams, par_result: Optional[SwapPricingResult] = None) -> SwapParams:
    """固定金利が空欄ならパーレートに固定したポジション。"""
    if params.fix_rate is not None:
        return params
    return replace(params, fix_rate=fair_rate(curves, params.terms, par_result))


def pv(curves, params: SwapParams, par_result: Optional[SwapPricingResult] = None) -> float:
    """PV(円)。固定金利が空欄ならパーレートで組む(常に0近辺)。"""
    return price(curves.base, fixed(curves, params, par_result)).pv


def parallel_delta_mm(
    curves, params: SwapParams, par_result: Optional[SwapPricingResult] = None,
    base_pv: Optional[float] = None,
) -> float:
    """カーブに使っている全テナーを動かしたときのデルタ(1bpあたり、百万円)。
    base_pv: 同じポジション(par_resultで固定した固定金利)の基準カーブでのPV。渡せば評価し直さない。"""
    return parallel_delta(curves, fixed(curves, params, par_result), base_pv) / _MM


def annuity_delta_mm(curves, params: SwapParams, par_result: Optional[SwapPricingResult] = None) -> float:
    """アニュイティデルタ(解析的近似、参考値、百万円)。"""
    annuity = (par_result or par(curves, params.terms)).annuity
    return annuity_delta(annuity, params.notional, params.pay_rec) / _MM


def notional_for_delta(
    curves, terms: SwapTerms, target_delta_mm, pay_rec,
    par_result: Optional[SwapPricingResult] = None,
) -> float:
    """目標delta(百万円)から必要なnotional(円)を逆算する(アニュイティデルタ基準)。"""
    pay_rec = SwapParams(terms, 1.0, pay_rec).pay_rec  # PAY/RECの値の確認はSwapParamsに任せる
    annuity = (par_result or par(curves, terms)).annuity
    return solve_notional_for_target_delta(float(target_delta_mm) * _MM, annuity, pay_rec)
