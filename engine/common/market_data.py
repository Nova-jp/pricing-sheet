"""
評価日時点の市場データ一式(カーブ+TONA実績)の型。計算(swap_pricer)は取引条件
(terms.SwapTerms)とこの Market を受け取る。

■ 取引条件と市場データを分ける
  カーブ・指標・TONA実績は取引ではなく市場の情報。取引条件の型には入れず、計算する側が
  取引条件から「どの日の実績が必要か」を判断して Market.fixings から取り出す
  (swap_pricer._check_fixings / _set_fixing_history)。呼び出し側は取引条件と市場を渡すだけ。

■ 先読みの排除はここだけで行う
  fixings は作る時点で「評価日より前の日」だけに切る。評価日当日のTONAはその日の引け値の
  時点ではまだ公表されておらず(公表は翌営業日)、QuantLibは当日の実績が登録されていると
  それを使ってしまうため。pricingシート(評価日=今日・過去日)もヒストリカル(各過去日)も
  同じ規則なので、呼び出し側では絞り込まない。

■ 実績の取得元はヒストリカルDBだけ(2026-10-12)
  朝のバッチ(python -m engine.common.morning_batch)がhistorical_data.xlsxのFixingシートから
  DBに取り込んだものを使う(カーブと同じ流れ)。DBを読むのは呼び出し側(udf・market)で、
  このモジュールはQuantLib以外に依存しない。
"""

from dataclasses import dataclass, field
from datetime import date
from types import MappingProxyType
from typing import Mapping

import QuantLib as ql


@dataclass(frozen=True)
class Market:
    """評価日・カーブ・TONA指標(カーブにひも付いたもの)・TONA実績 {日付: %}。"""

    valuation_date: date
    curve: ql.YieldTermStructureHandle
    index: ql.OvernightIndex
    fixings: Mapping[date, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        cut = {d: v for d, v in self.fixings.items() if d < self.valuation_date}
        object.__setattr__(self, "fixings", MappingProxyType(cut))
