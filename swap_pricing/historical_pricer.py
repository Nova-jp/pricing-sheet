"""
ローカルキャッシュ済みの日次DFカーブから、任意のコンベンションの
ヒストリカル・パーレート推移を計算するモジュール。

「そのコンベンションでのヒストリカル」= 各過去日付Dのスポット日を
起点として、同じコンベンション(tenor/freq/dcf/roll conv)の
スワップを新規に組んだ場合のパーレートの時系列
(特定の1トレードのMTM推移ではない)。

Neonには一切アクセスしない(morning_batch.pyが事前にキャッシュした
ローカルSQLiteのみを参照)。
"""

from datetime import date
from typing import Dict, List, NamedTuple, Optional, Tuple

from swap_pricing.calendars import add_business_days
from swap_pricing.daycount import year_fraction
from swap_pricing.local_cache import cached_dates, load_curve
from swap_pricing.monotone_convex import MonotoneConvexCurve
from swap_pricing.swap_pricer import price_swap


class HistoricalPoint(NamedTuple):
    as_of_date: date
    par_rate: float  # %(カーブ/フライの場合はスプレッド、%表記のまま)


class Convention(NamedTuple):
    fix_freq: str
    fix_dcf: str
    float_freq: str
    float_dcf: str
    roll_conv: str
    tenor: str


def historical_par_rate_series(
    fix_freq: str,
    fix_dcf: str,
    float_freq: str,
    float_dcf: str,
    roll_conv: str,
    tenor: str,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> List[HistoricalPoint]:
    """
    キャッシュ済みの各as_of_dateについて、そのスポット日を起点とする
    tenor年限のコンベンションのパーレートを計算し、時系列で返す。
    """
    dates = cached_dates()
    if start_date is not None:
        dates = [d for d in dates if d >= start_date]
    if end_date is not None:
        dates = [d for d in dates if d <= end_date]

    results: List[HistoricalPoint] = []
    for as_of_date in dates:
        cached = load_curve(as_of_date)
        if cached is None:
            continue
        pillar_times, pillar_dfs = cached
        curve = MonotoneConvexCurve(pillar_times, pillar_dfs)
        spot_date = add_business_days(as_of_date, 2)

        result = price_swap(
            curve, as_of_date, spot_date, fix_freq, fix_dcf, float_freq, float_dcf,
            roll_conv, notional=1.0, pay_rec="PAY", tenor=tenor,
        )
        results.append(HistoricalPoint(as_of_date=as_of_date, par_rate=result.target_fixrate))

    return results


def _series_dict(convention: Convention) -> Dict[date, float]:
    points = historical_par_rate_series(
        convention.fix_freq, convention.fix_dcf, convention.float_freq,
        convention.float_dcf, convention.roll_conv, tenor=convention.tenor,
    )
    return {p.as_of_date: p.par_rate for p in points}


def historical_curve_series(
    convention_short: Convention, convention_long: Convention
) -> List[HistoricalPoint]:
    """カーブ(スプレッド) = convention_longのパーレート - convention_shortのパーレート の時系列。"""
    s_short = _series_dict(convention_short)
    s_long = _series_dict(convention_long)
    common_dates = sorted(set(s_short) & set(s_long))
    return [HistoricalPoint(d, s_long[d] - s_short[d]) for d in common_dates]


def historical_fly_series(
    convention_short: Convention, convention_belly: Convention, convention_long: Convention
) -> List[HistoricalPoint]:
    """
    フライ(等ウェイト) = 2*bellyのパーレート - shortのパーレート - longのパーレート の時系列。
    DV01加重等の高度な重み付けは未対応(スコープ外)。
    """
    s_short = _series_dict(convention_short)
    s_belly = _series_dict(convention_belly)
    s_long = _series_dict(convention_long)
    common_dates = sorted(set(s_short) & set(s_belly) & set(s_long))
    return [
        HistoricalPoint(d, 2 * s_belly[d] - s_short[d] - s_long[d]) for d in common_dates
    ]
