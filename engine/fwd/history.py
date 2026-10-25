"""
分析ブック(SwapAnalysis.xlsm)のFwdMonitorシートの材料: Fwdスワップレート(NyMy、例 5y5y)の
ヒストリカルを3つの方式で計算する。QuantLibが要る部分(各日のFwdの水準と、同じ契約の日次変化)だけを
ここで計算し、カーブ・フライ・変化・Z値はExcelの数式で行う。

■ 3つの方式(mode)。基準日B(空欄=今日)の取引を、各過去日Dにどう移すか
  - CMT    : 定義どおり。Dのスポット日+N年を起算日、そこからM年(マーケットの「5y5y」の推移)。
  - FIXED  : Bの取引(Bのスポット日+N年からM年)の起算日・満期日をそのまま固定して、各Dで評価する
             (HistChartのFIXEDと同じ。過去にさかのぼるほど起算日が先になる=ロールダウンが見える)。
  - ROLLING: Bの取引の「スポット日→起算日」「起算日→満期日」の営業日数(ql.Japan)を保って、
             各Dのスポット日に平行移動する(HistChartのROLLINGと同じ。祝日の入り方で暦日数が変わる)。
  BがDBの最終日より後(通常は今日)なら、3方式とも最終日の取引はほぼ同じで、ライブ(FwdLive)とも同じ取引。

■ 日次変化(bp)
  Dの変化 = Dの取引を、Dのカーブと前の日付のカーブで評価した差(同じ契約どうし。ロールダウンを含まない。
  engine.vol.hv と同じ)。前の日付はDBでカーブのある直前の日。Z値の分母(σ)に使う。

■ 速くするための工夫
  カーブは日付ごとに1回だけ読み、3方式・全銘柄で共有する。スワップは ForwardPricer で使い回す。
  ROLLINGの営業日の移動は、営業日の一覧の添字の足し算にする(ql.Calendar.advance は1日ずつ
  数えるので、35年先では1回ごとに数千日を数えることになり遅い。結果は同じ。tests/test_fwd.py)。
"""

import re
from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional, Protocol, Sequence, Tuple

import QuantLib as ql

from engine.common.conventions import CALENDAR, forward_start_date
from engine.common.curve import spot_date_for
from engine.common.forward import ForwardPricer, last_date
from engine.common.swap_pricer import swap_dates
from engine.common.terms import STANDARD, SwapTerms

CMT = "CMT"
FIXED = "FIXED"
ROLLING = "ROLLING"
MODES = (CMT, FIXED, ROLLING)

_LABEL = re.compile(r"^(\d+[MY])X?(\d+[MY])$")
_HORIZON_MARGIN = timedelta(days=40)  # カーブを読む範囲の余裕(満期日の休日調整分)


class CurveSource(Protocol):
    def dates(self) -> List[date]: ...

    def load(self, as_of_date: date, until: Optional[date] = None): ...


def parse_label(label) -> Tuple[str, str]:
    """「N先M年」(例 5y5y・1y1y。5Yx5Yも可)→ ("5Y", "5Y")。"""
    m = _LABEL.match(str(label).strip().upper().replace(" ", ""))
    if m is None:
        raise ValueError(f"銘柄は「何年先+何年」で指定してください(例 5y5y・1y1y): {label!r}")
    return m.group(1), m.group(2)


def label_name(label) -> str:
    """表示用の名前(例 5y5y)。"""
    start, tail = parse_label(label)
    return f"{start}{tail}".lower()


def parse_mode(value) -> str:
    mode = str(value).strip().upper()
    if mode not in MODES:
        raise ValueError(f"modeは {'/'.join(MODES)} のどれかです: {value!r}")
    return mode


class _BusinessDays:
    """営業日(ql.Japan)の一覧。営業日から営業日への移動・数え方を添字の計算にする。"""

    def __init__(self, first: date, last: date) -> None:
        self.days: List[date] = []
        d = CALENDAR.adjust(ql.Date.from_date(first))
        end = ql.Date.from_date(last)
        while d <= end:
            if CALENDAR.isBusinessDay(d):
                self.days.append(d.to_date())
            d += 1
        self._index: Dict[date, int] = {d: i for i, d in enumerate(self.days)}

    def between(self, start: date, end: date) -> int:
        """CALENDAR.businessDaysBetween(start, end)(start以上end未満の営業日数)。startは営業日。"""
        return bisect_left(self.days, end) - self._index[start]

    def advance(self, start: date, n: int) -> date:
        """CALENDAR.advance(start, n, Days)(startは営業日)。"""
        return self.days[self._index[start] + n]


@dataclass
class FwdHistory:
    """行=カーブのある日付(最初の日付は変化が無いので含めない)。列=銘柄。"""

    labels: List[str]
    base_date: date
    dates: List[date] = field(default_factory=list)
    level: Dict[str, List[List[float]]] = field(default_factory=lambda: {m: [] for m in MODES})  # %
    change: Dict[str, List[List[float]]] = field(default_factory=lambda: {m: [] for m in MODES})  # bp


class _Contracts:
    """3方式それぞれの、各日の取引の決め方(基準日の取引から1回だけ作る)。"""

    def __init__(self, pairs: Sequence[Tuple[str, str]], base_date: date, first_date: date) -> None:
        self.pairs = pairs
        base_spot = spot_date_for(base_date)
        self.fixed = [SwapTerms(forward_start_date(base_spot, e), STANDARD, tenor=t) for e, t in pairs]
        spans = [swap_dates(terms) for terms in self.fixed]
        self.bdays = _BusinessDays(
            min(spot_date_for(first_date), base_spot), max(end for _, end in spans) + _HORIZON_MARGIN,
        )
        self.offsets = [
            (self.bdays.between(base_spot, start), self.bdays.between(start, end)) for start, end in spans
        ]

    def on(self, as_of_date: date) -> Dict[str, List[SwapTerms]]:
        spot = spot_date_for(as_of_date)
        rolling = []
        for offset, length in self.offsets:
            start = self.bdays.advance(spot, offset)
            rolling.append(SwapTerms(start, STANDARD, end=self.bdays.advance(start, length)))
        return {
            CMT: [SwapTerms(forward_start_date(spot, e), STANDARD, tenor=t) for e, t in self.pairs],
            FIXED: self.fixed,
            ROLLING: rolling,
        }


def compute(curves: CurveSource, labels: Sequence[str], base_date: date) -> FwdHistory:
    """curves: 日次カーブの供給元(historical_store.HistoricalStore)。base_date より後の日付は使わない。"""
    pairs = [parse_label(name) for name in labels]
    series = FwdHistory(labels=[f"{e}{t}".lower() for e, t in pairs], base_date=base_date)
    dates = [d for d in curves.dates() if d <= base_date]
    if len(dates) < 2 or not pairs:
        return series

    contracts = _Contracts(pairs, base_date, dates[0])
    pricer = ForwardPricer()
    prev: Dict[str, List[float]] = {}  # 前の日付のカーブで評価した、当日の取引のFwd
    today_terms = contracts.on(dates[0])
    for i, d in enumerate(dates):
        next_terms = contracts.on(dates[i + 1]) if i + 1 < len(dates) else {m: [] for m in MODES}
        needed = [t for terms in (today_terms, next_terms) for m in MODES for t in terms[m]]
        pricer.keep_only(needed)
        stored = curves.load(d, until=max(last_date(t) for t in needed) + _HORIZON_MARGIN)
        pricer.set_curve(d, stored.curve)
        if i > 0:
            series.dates.append(d)
        for m in MODES:
            today = [pricer.rate(t) for t in today_terms[m]]
            if i > 0:
                series.level[m].append(today)
                series.change[m].append([(a - b) * 100.0 for a, b in zip(today, prev[m])])
            prev[m] = [pricer.rate(t) for t in next_terms[m]]
        today_terms = next_terms
    return series


def live_terms(labels: Sequence[str], as_of_date: date) -> List[SwapTerms]:
    """ライブ(今日のカーブ)で評価する取引(今日のスポット日+N年からM年。基準日=今日の3方式と同じ取引)。"""
    spot = spot_date_for(as_of_date)
    return [SwapTerms(forward_start_date(spot, e), STANDARD, tenor=t) for e, t in map(parse_label, labels)]
