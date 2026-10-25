"""
保存済みの日次カーブで、多数のFwdスワップレートを日付ごとに評価する部品(分析ブックのvol・fwdが共有)。

■ 速くするための工夫(結果は swap_pricer.par_rate と同じ。tests/test_vol.py・tests/test_fwd.py)
  カーブを差し替え可能なハンドルに載せ、同じ取引条件のスワップは1回だけ組んで、日付ごとに
  ハンドルを当日のカーブにつなぎ替えて fairRate を読む。スワップの組み方は swap_pricer._build_swap
  をそのまま使う。
"""

from datetime import date
from typing import Dict, Iterable

import QuantLib as ql

from engine.common.swap_pricer import PAY, _build_swap
from engine.common.terms import SwapTerms


class ForwardPricer:
    """同じ取引条件(SwapTerms)のスワップを使い回して、日付ごとのFwdスワップレートを読む。"""

    def __init__(self) -> None:
        self.handle = ql.RelinkableYieldTermStructureHandle()
        self.index = ql.Tonar(self.handle)
        self.engine = ql.DiscountingSwapEngine(self.handle)
        self.swaps: Dict[SwapTerms, ql.OvernightIndexedSwap] = {}

    def set_curve(self, as_of_date: date, curve: ql.YieldTermStructureHandle) -> None:
        ql.Settings.instance().evaluationDate = ql.Date.from_date(as_of_date)
        self.handle.linkTo(curve.currentLink())

    def rate(self, terms: SwapTerms) -> float:
        """Fwdスワップレート(%)。"""
        swap = self.swaps.get(terms)
        if swap is None:
            swap = _build_swap(self.index, terms, 1.0, PAY, 0.0)
            swap.setPricingEngine(self.engine)
            self.swaps[terms] = swap
        return swap.fairRate() * 100.0

    def keep_only(self, terms: Iterable[SwapTerms]) -> None:
        keep = set(terms)
        for key in [k for k in self.swaps if k not in keep]:
            del self.swaps[key]


def last_date(terms: SwapTerms) -> date:
    """カーブを読み出す範囲の目安(満期日。tenor指定なら起算日+tenor、休日調整前)。"""
    if terms.end is not None:
        return terms.end
    return (ql.Date.from_date(terms.start) + ql.PeriodParser.parse(terms.tenor.upper())).to_date()
