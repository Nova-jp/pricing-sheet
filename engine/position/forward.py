"""
パーのピラーごとのデルタ → フォワードの区間ごとのデルタ の換算行列(ヤコビアン)。

■ 定義
  区間 i = 隣り合うピラーのスワップの満期日の間(最初の区間はスポット→最短のピラーの満期日)。
  区間 i のフォワード(瞬間フォワードレート)だけを1bp上げたカーブで、各ピラー j のパーレートが
  何bp動くかを J[j, i] とする。パーのデルタ d(ピラー j のパーレート1bpあたりのPV変化)のポジションは、
  区間 i のフォワード1bpで Σ_j d_j·J[j, i] だけ動く。つまり フォワードのデルタ = Jᵀ d
  (行列の積はExcelのMMULTで行う)。
  Σ_i J[j, i] はほぼ1(全区間を1bp上げる=パーレートがほぼ1bp上がる)。

■ 計算
  基準カーブ(market.CurveSet)の暦日ごとのDFを持つ曲線を作り、区間 i にかかる日のDFに
  exp(−1bp×区間内の経過年数) を掛けたものと比べる。パーレートは swap_pricer.par_rate(QuantLib)。
  基準のパーレートも同じ暦日ごとの曲線で出す(補間の差が換算に入らないように)。
  O/N(TONA)はスポットより前の1日だけなので、自分自身の区間(J=1)とし、他とは独立に扱う。
"""

import math
from datetime import date, timedelta
from typing import List, NamedTuple, Sequence

import QuantLib as ql

from engine.common.market import ON_LABEL, CurveSet
from engine.common.market_data import Market
from engine.common.swap_pricer import par_rate, swap_dates
from engine.common.terms import STANDARD, SwapTerms

BUMP = 1.0e-4  # 区間のフォワードを動かす幅(1bp)
_DC = ql.Actual365Fixed()
_MARGIN_DAYS = 10


class ForwardJacobian(NamedTuple):
    pillars: List[str]  # 入力の並び(行)
    segments: List[str]  # 区間のラベル(列、ピラーと同じ並び)。「10Y-11Y」等
    matrix: List[List[float]]  # J[ピラー][区間]


def _segment_labels(pillars: Sequence[str]) -> List[str]:
    labels, prev = [], "Spot"
    for p in pillars:
        if p.strip().lower() == ON_LABEL:
            labels.append(p)
            continue
        labels.append(f"{prev}-{p}")
        prev = p
    return labels


def _daily_curve(dates: List[ql.Date], dfs: List[float]) -> ql.YieldTermStructureHandle:
    curve = ql.DiscountCurve(dates, dfs, _DC)
    curve.enableExtrapolation()
    return ql.YieldTermStructureHandle(curve)


def forward_jacobian(curves: CurveSet, pillars: Sequence[str]) -> ForwardJacobian:
    """pillars: グリッドのピラーのラベル(短い順)。カーブに使っていないピラーの行・列は0。"""
    labels = [str(p).strip() for p in pillars]
    used = {p.lower() for p in curves.bump_labels}
    spot = curves.spot_date
    swaps = {}
    for p in labels:
        key = p.lower()
        if key == ON_LABEL or key not in used:
            continue
        terms = SwapTerms(spot, STANDARD, tenor=key)
        swaps[p] = (terms, swap_dates(terms)[1])
    ends = [swaps[p][1] for p in labels if p in swaps]
    if ends != sorted(ends):
        raise ValueError("ピラーは短い順に並べてください")

    val = curves.valuation_date
    last = max(ends) + timedelta(days=_MARGIN_DAYS)
    days = [val + timedelta(days=n) for n in range((last - val).days + 1)]
    qdates = [ql.Date.from_date(d) for d in days]
    base_curve = curves.base.curve
    base_dfs = [base_curve.discount(d) for d in qdates]

    def market(dfs: List[float]) -> Market:
        handle = _daily_curve(qdates, dfs)
        return Market(val, handle, ql.Tonar(handle), curves.base.fixings)

    def pars(m: Market) -> dict:
        return {p: par_rate(m, terms).target_fixrate for p, (terms, _) in swaps.items()}

    base = pars(market(base_dfs))
    n = len(labels)
    matrix = [[0.0] * n for _ in range(n)]
    prev_end = spot
    for i, p in enumerate(labels):
        if p.lower() == ON_LABEL and p.lower() in used:
            matrix[i][i] = 1.0
            continue
        if p not in swaps:
            continue
        a, b = prev_end, swaps[p][1]
        prev_end = b
        bumped_dfs = [
            df * math.exp(-BUMP * max(0, (min(d, b) - a).days) / 365.0) for d, df in zip(days, base_dfs)
        ]
        bumped = pars(market(bumped_dfs))
        for j, q in enumerate(labels):
            if q in swaps:
                matrix[j][i] = (bumped[q] - base[q]) * 100.0 / (BUMP * 1e4)
    return ForwardJacobian(labels, _segment_labels(labels), matrix)
