"""
日次カーブDBから、VolLab.xlsxのデータシートに書く値を作る(QuantLibが必要な部分だけ)。

■ 何を作るか(行=カーブのある日付。全シートで同じ日付の並び)
  - fwd     : 満期一定型(B)のFwdスワップレート(%)
  - dfwd    : 満期一定型(B)の日次変化(bp)。前日と当日のカーブで、当日の起算日・満期日の
              Fwdを評価した差(ロールダウンを混ぜない)。最初の日は無し
  - cum_sq  : dfwdの二乗の累積(bp²)。最初の日を0とする。Excelが任意の窓のRVを
              「累積の差」で計算するため(窓の長さをExcel側で自由に変えられる)
  - days_bd / days_cal : その日から各満期のオプションの満期日までの営業日数・暦日数
  - track_sumsq / track_n : オプション追跡型(A)。その日に取引した満期e x 原資産nのオプションの、
              満期日までの日次変化の二乗和と日数。満期日までDBのカーブがそろう取引日だけ
  - iv      : DBのvol_quotes(ATMノーマルvol、bp/年)。無い日は None

■ 速くするための工夫(結果は swap_pricer.par_rate と完全に一致する。tests/test_vol_lab.py)
  日付ごとにスワップを組み直すと遅いので、カーブを差し替え可能なハンドル
  (RelinkableYieldTermStructureHandle)に載せ、同じ起算日・満期日のスワップは1回だけ組んで
  使い回す(日付ごとにハンドルを当日のカーブにつなぎ替えて fairRate を読むだけ)。
  もう使わないスワップは捨てる。スワップの組み方は swap_pricer._build_swap をそのまま使う。
"""

from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

import QuantLib as ql

from engine.common.conventions import CALENDAR, forward_start_date
from engine.common.curve import spot_date_for
from engine.common.swap_pricer import PAY, _build_swap
from engine.common.terms import STANDARD, SwapTerms
from tools.vol_lab.grid import EXPIRIES, TAILS, TRACK_EXPIRIES, label

OPTION_EXPIRY_LAG = 2  # オプションの満期日 = 原資産の起算日の2営業日前(スポットラグ)

Key = Tuple[date, str]  # (休日調整前の起算日, 原資産のテナー)


@dataclass
class VolLabData:
    dates: List[date]
    labels: List[str]  # 満期一定型(B)の銘柄(列の並び)
    track_labels: List[str]  # オプション追跡型(A)の銘柄
    expiries: List[str]
    iv: List[List[Optional[float]]] = field(default_factory=list)
    fwd: List[List[float]] = field(default_factory=list)
    dfwd: List[List[Optional[float]]] = field(default_factory=list)
    cum_sq: List[List[float]] = field(default_factory=list)
    days_bd: List[List[int]] = field(default_factory=list)
    days_cal: List[List[int]] = field(default_factory=list)
    track_sumsq: List[List[Optional[float]]] = field(default_factory=list)
    track_n: List[List[Optional[int]]] = field(default_factory=list)


def option_expiry(start: date) -> date:
    """原資産の起算日(休日調整前) → オプションの満期日。"""
    adjusted = CALENDAR.adjust(ql.Date.from_date(start), ql.ModifiedFollowing)
    return CALENDAR.advance(adjusted, -OPTION_EXPIRY_LAG, ql.Days).to_date()


class _ForwardPricer:
    """同じ起算日・満期日のスワップを使い回して、日付ごとのFwdスワップレートを読む。"""

    def __init__(self) -> None:
        self.handle = ql.RelinkableYieldTermStructureHandle()
        self.index = ql.Tonar(self.handle)
        self.engine = ql.DiscountingSwapEngine(self.handle)
        self.swaps: Dict[Key, ql.OvernightIndexedSwap] = {}

    def set_curve(self, as_of_date: date, curve: ql.YieldTermStructureHandle) -> None:
        ql.Settings.instance().evaluationDate = ql.Date.from_date(as_of_date)
        self.handle.linkTo(curve.currentLink())

    def rate(self, key: Key) -> float:
        """Fwdスワップレート(%)。"""
        swap = self.swaps.get(key)
        if swap is None:
            start, tail = key
            swap = _build_swap(self.index, SwapTerms(start=start, convention=STANDARD, tenor=tail), 1.0, PAY, 0.0)
            swap.setPricingEngine(self.engine)
            self.swaps[key] = swap
        return swap.fairRate() * 100.0

    def keep_only(self, keys: Set[Key]) -> None:
        for key in [k for k in self.swaps if k not in keys]:
            del self.swaps[key]


def compute(
    curves,
    vol_quotes: Mapping[date, Mapping[str, float]],
    *,
    expiries: Sequence[str] = EXPIRIES,
    tails: Sequence[str] = TAILS,
    track_expiries: Sequence[str] = TRACK_EXPIRIES,
    progress: Optional[Callable[[int, int], None]] = None,
) -> VolLabData:
    """curves: dates()とload(日付)を持つ日次カーブの供給元(historical_store.HistoricalStore)。"""
    dates = curves.dates()
    n_days = len(dates)
    tails_ql = [t.lower() for t in tails]
    labels = [label(e, t) for e in expiries for t in tails]
    track_labels = [label(e, t) for e in track_expiries for t in tails]
    data = VolLabData(dates, labels, track_labels, list(expiries))
    if n_days == 0:
        return data

    # 日付ごとの起算日(休日調整前)とオプションの満期日
    starts = []
    for d in dates:
        spot = spot_date_for(d)
        starts.append({e: forward_start_date(spot, e) for e in expiries})
    expiry_dates = [{e: option_expiry(s) for e, s in row.items()} for row in starts]
    last_date = dates[-1]

    # オプション追跡型(A): 取引日rの満期eのオプションを、満期日までの各日で追う
    # track_last[r][e] = 満期日以前で最後のカーブの行。満期日がDBの最終日より後なら追わない
    track_last: List[Dict[str, Optional[int]]] = []
    for r in range(n_days):
        row = {}
        for e in track_expiries:
            x = expiry_dates[r][e]
            row[e] = bisect_right(dates, x) - 1 if x <= last_date else None
        track_last.append(row)
    active: List[List[Tuple[int, str]]] = [[] for _ in range(n_days)]  # 行iで評価が要る(取引日, 満期)
    for r in range(n_days):
        for e in track_expiries:
            k = track_last[r][e]
            if k is not None:
                for i in range(r, k + 1):
                    active[i].append((r, e))

    def keys_at(i: int) -> Set[Key]:
        """行iのカーブで評価が要るFwd。当日分と、翌日の変化の「前日の値」になる翌日分(B)、追跡中(A)。"""
        keys = {(starts[i][e], t) for e in expiries for t in tails_ql}
        if i + 1 < n_days:
            keys |= {(starts[i + 1][e], t) for e in expiries for t in tails_ql}
        keys |= {(starts[r][e], t) for r, e in active[i] for t in tails_ql}
        return keys

    pricer = _ForwardPricer()
    n_track = len(track_labels)
    track_sumsq: List[List[float]] = [[0.0] * n_track for _ in range(n_days)]
    track_count: List[List[int]] = [[0] * n_track for _ in range(n_days)]
    track_col = {(e, t): n for n, (e, t) in enumerate((e, t) for e in track_expiries for t in tails_ql)}
    prev: Dict[Key, float] = {}
    cum = [0.0] * len(labels)

    for i, d in enumerate(dates):
        keys = keys_at(i)
        pricer.keep_only(keys)
        pricer.set_curve(d, curves.load(d).curve)
        values = {key: pricer.rate(key) for key in keys}

        fwd_row, dfwd_row = [], []
        for e in expiries:
            for t in tails_ql:
                key = (starts[i][e], t)
                fwd_row.append(values[key])
                dfwd_row.append((values[key] - prev[key]) * 100.0 if i > 0 else None)
        cum = [c + (x * x if x is not None else 0.0) for c, x in zip(cum, dfwd_row)]
        data.fwd.append(fwd_row)
        data.dfwd.append(dfwd_row)
        data.cum_sq.append(cum)

        for r, e in active[i]:
            if r == i:
                continue
            for t in tails_ql:
                key = (starts[r][e], t)
                x = (values[key] - prev[key]) * 100.0
                col = track_col[(e, t)]
                track_sumsq[r][col] += x * x
                track_count[r][col] += 1

        day = ql.Date.from_date(d)
        data.days_bd.append([
            CALENDAR.businessDaysBetween(day, ql.Date.from_date(expiry_dates[i][e]), False, True) for e in expiries
        ])
        data.days_cal.append([(expiry_dates[i][e] - d).days for e in expiries])
        quotes = vol_quotes.get(d, {})
        data.iv.append([quotes.get(name) for name in labels])
        prev = values
        if progress is not None:
            progress(i + 1, n_days)

    for r in range(n_days):
        done = {e: track_last[r][e] is not None for e in track_expiries}
        data.track_sumsq.append([
            track_sumsq[r][track_col[(e, t)]] if done[e] else None for e in track_expiries for t in tails_ql
        ])
        data.track_n.append([
            track_count[r][track_col[(e, t)]] if done[e] else None for e in track_expiries for t in tails_ql
        ])
    return data
