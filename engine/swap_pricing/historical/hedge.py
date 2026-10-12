"""
ヘッジのヒストリカル(HistChartで系列1に重ねる)用に、スポットのテナーごとのパーレートを
指定した日付の並びで返すモジュール。

ヘッジの線は仮想金利 V(t) = Σ_i d_i × r_i(t)(d_i=テナーiのバンプデルタ、r_i=テナーiの
スポットのパーレート)で、Σ・縮尺・水準合わせはExcelの数式で行う(docs/decisions.mdの
「ヘッジのヒストリカル」)。ここでは r_i(t) だけを計算する。

- r_i(t) は標準の規約(terms.STANDARD)のスポットのスワップ(start空欄+テナー)を、
  historical.pricer と同じ経路で各日のカーブで評価したパーレート。ROLLING/FIXEDの意味も同じ
  (FIXED=基準日に取引したスポットのスワップを日付固定で評価)。
- その日のカーブの構築にテナーiを使っていない日(クォートが無かった日)は、カーブ上は
  補間で値が出てしまうが、空欄(None)にする(穴埋めしない。ユーザー判断)。
- ヘッジはスポットで取引されているものだけなので、O/Nと会合スワップ(RD・M1〜)は受け付けない。
"""

from datetime import date
from typing import Dict, List, Optional, Protocol, Sequence, Set

import QuantLib as ql

from engine.common.curve import EXCLUDED_MEETING_TENORS, ON_KEYS, normalize_tenor
from engine.common.terms import STANDARD
from engine.swap_pricing.historical.pricer import CurveSource, SwapSpec, historical_multi_rate_series


class HedgeCurveSource(CurveSource, Protocol):
    """日次カーブの供給元に、日付ごとの構築に使ったテナーを加えたもの。"""

    def used_tenors(self) -> Dict[date, Set[str]]:
        """{日付: カーブに使ったテナー(curve.normalize_tenor の表記)}。"""


def hedge_tenor(label) -> str:
    """ヘッジのテナーの入力(RealTimeのTicker、例 '5Y')を確かめて、前後の空白を除いて返す。"""
    text = str(label).strip()
    key = text.lower()
    if key in ON_KEYS:
        raise ValueError(f"O/Nはヘッジに使えません(スポットのテナーだけ): {text}")
    if key in EXCLUDED_MEETING_TENORS:
        raise ValueError(f"会合スワップはヘッジに使えません(スポットのテナーだけ): {text}")
    try:
        ql.PeriodParser.parse(text.upper())
    except RuntimeError:
        raise ValueError(f"テナーとして解釈できません: {text}") from None
    return text


class _OnDates:
    """カーブの供給元を、指定した日付(カーブのある日だけ)に絞る。"""

    def __init__(self, curves: CurveSource, wanted: Set[date]):
        self._curves = curves
        self._wanted = wanted

    def dates(self) -> List[date]:
        return [d for d in self._curves.dates() if d in self._wanted]

    def load(self, as_of_date: date, until: Optional[date] = None):
        return self._curves.load(as_of_date, until=until)


def hedge_rate_rows(
    tenors: Sequence[str],
    mode: str,
    dates: Sequence[date],
    base_date: Optional[date] = None,
    fixings: Optional[Dict[date, float]] = None,
    *,
    curves: HedgeCurveSource,
) -> List[List[Optional[float]]]:
    """
    dates の並び(重複・降順も可)どおりに、テナーごとのパーレート(%)の行を返す。
    カーブの無い日・そのテナーをカーブに使っていない日・計算できない日(FIXEDで満期後)は None。

    tenors: ヘッジのテナー(hedge_tenor で確かめる)。mode/base_date/fixings は
        historical.pricer.historical_multi_rate_series と同じ。
    """
    labels = []
    for n, label in enumerate(tenors, start=1):
        try:
            labels.append(hedge_tenor(label))
        except ValueError as e:
            raise ValueError(f"{n}本目: {e}") from e
    if not labels:
        return [[] for _ in dates]

    specs = [SwapSpec(STANDARD, tenor=label) for label in labels]
    series = historical_multi_rate_series(
        specs, mode, base_date=base_date, fixings=fixings, curves=_OnDates(curves, set(dates)),
    )
    used = curves.used_tenors()
    keys = [normalize_tenor(label) for label in labels]
    by_date: Dict[date, List[Optional[float]]] = {}
    for as_of_date, legs in series:
        used_on = used.get(as_of_date, set())
        by_date[as_of_date] = [
            leg.par_rate if leg is not None and key in used_on else None
            for key, leg in zip(keys, legs)
        ]
    empty = [None] * len(labels)
    return [list(by_date.get(d, empty)) for d in dates]
