"""
インプライド(スワップションATMノーマルvol)と比べるヒストリカル・ボラの材料(分析ブックのVolHVシート)。
QuantLibが要る部分(Fwdスワップレートの日次変化)だけをここで計算し、窓の集計・年率換算・
ボラの差(カーブ・フライ)はExcelの数式で行う(窓を変えてもここは再計算しない)。

■ 何と比べるか
  Tm x U(例 1Yx10Y)のIVは「T先スタート・U年」のFwdスワップレートのボラなので、HVも同じFwdの
  変化で測る(スポットのU年で代用すると、短い原資産・長い満期ほどずれる)。

■ 日次変化(bp)
  日付Dの変化 = 「Dの起算日(Dのスポット日+T)・満期日のFwd」を、Dのカーブと前の日付のカーブで
  評価した差。満期一定のFwd(1Yx10Yなど)を日ごとに比べると、日がたつだけで動く分(ロールダウン)が
  混ざるため、同じ起算日・満期日の契約どうしで比べる。前の日付はDBでカーブのある直前の日
  (休日・欠損をまたぐ日は2日以上の変化になる)。

■ 速くするための工夫
  同じ起算日・満期日のスワップを使い回す engine.common.forward.ForwardPricer で評価する
  (結果は swap_pricer.par_rate と同じ。tests/test_vol.py)。カーブは日付ごとに1回だけ読み、全銘柄で共有する。
"""

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import List, Mapping, Optional, Protocol, Sequence, Tuple

from engine.common.conventions import forward_start_date
from engine.common.curve import spot_date_for
from engine.common.forward import ForwardPricer, last_date
from engine.common.terms import STANDARD, SwapTerms

_LABEL = re.compile(r"^(\d+[MY])X(\d+[MY])$")
_HORIZON_MARGIN = timedelta(days=40)  # カーブを読む範囲の余裕(満期日の休日調整分)


class CurveSource(Protocol):
    def dates(self) -> List[date]: ...

    def load(self, as_of_date: date, until: Optional[date] = None): ...


def parse_label(label) -> Tuple[str, str]:
    """「満期x原資産」(例 1Yx10Y) → ("1Y", "10Y")。DBのvol_quotesの銘柄名と同じ書き方。"""
    m = _LABEL.match(str(label).strip().upper())
    if m is None:
        raise ValueError(f"銘柄は「満期x原資産」で指定してください(例 1Yx10Y): {label!r}")
    return m.group(1), m.group(2)


def live_terms(labels: Sequence[str], as_of_date: date) -> List[SwapTerms]:
    """ライブ(今日のカーブ)で評価する、今日の契約(起算日=今日のスポット日+満期、原資産)。compute の各日の契約と同じ決め方。"""
    spot = spot_date_for(as_of_date)
    return [SwapTerms(forward_start_date(spot, e), STANDARD, tenor=t) for e, t in map(parse_label, labels)]


@dataclass
class VolSeries:
    """行=カーブのある日付(最初の日付は変化が無いので含めない)。列=銘柄。"""

    labels: List[str]
    dates: List[date] = field(default_factory=list)
    iv: List[List[Optional[float]]] = field(default_factory=list)  # bp/年。DBに無い日は None
    fwd: List[List[float]] = field(default_factory=list)  # その日のFwd(%)
    dfwd: List[List[float]] = field(default_factory=list)  # 日次変化(bp)


def compute(
    curves: CurveSource,
    vol_quotes: Mapping[date, Mapping[str, float]],
    labels: Sequence[str],
) -> VolSeries:
    """curves: 日次カーブの供給元(historical_store.HistoricalStore)。
    vol_quotes: {日付: {銘柄: IV}}(DBのvol_quotes。銘柄名の大文字小文字は問わない)。"""
    pairs = [parse_label(name) for name in labels]
    series = VolSeries(labels=[f"{e}x{t}" for e, t in pairs])
    dates = curves.dates()
    if len(dates) < 2 or not pairs:
        return series

    # 日付ごとの、その日の契約(起算日=その日のスポット日+満期、原資産)
    contracts = [live_terms(series.labels, d) for d in dates]
    ivs = [{k.upper(): v for k, v in vol_quotes.get(d, {}).items()} for d in dates]

    pricer = ForwardPricer()
    prev: List[float] = []  # 前の日付のカーブで評価した、当日の契約のFwd
    for i, d in enumerate(dates):
        keys = contracts[i]
        nxt = contracts[i + 1] if i + 1 < len(dates) else []
        needed = set(keys) | set(nxt)
        pricer.keep_only(needed)
        stored = curves.load(d, until=max(last_date(k) for k in needed) + _HORIZON_MARGIN)
        pricer.set_curve(d, stored.curve)
        today = [pricer.rate(k) for k in keys]
        if i > 0:
            series.dates.append(d)
            series.iv.append([ivs[i].get(name.upper()) for name in series.labels])
            series.fwd.append(today)
            series.dfwd.append([(a - b) * 100.0 for a, b in zip(today, prev)])
        prev = [pricer.rate(k) for k in nxt]
    return series
