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
from typing import Dict, List, NamedTuple, Optional

import QuantLib as ql

from swap_pricing.curve import bootstrap_curve
from swap_pricing.local_cache import cached_dates, load_rates
from swap_pricing.swap_pricer import price_swap

# カーブ構築(curve.py)自体が使っている標準コンベンション。
# ヒストリカルのアウトライトがこれと完全一致する場合は、自前スケジュールでは
# なくql.MakeOISで直接組む(カーブの内部スケジュールと厳密に一致させるため。
# 自前スケジュール(Forward生成)は月末を跨ぐケースでMakeOIS側とごく僅かに
# 異なることがある(0.1bp未満)。任意コンベンション対応の自前ロジックは
# 端株が必要なケース向けに残す)。
_STANDARD_CONVENTION = ("PA", "act/365fixed", "PA", "act/365fixed", "STD")


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
        rates = load_rates(as_of_date)
        if rates is None:
            continue
        boot = bootstrap_curve(as_of_date, rates)

        if (fix_freq, fix_dcf, float_freq, float_dcf, roll_conv) == _STANDARD_CONVENTION:
            # カーブ自体と同じ標準コンベンション -> ql.MakeOISで直接組み、
            # カーブ構築時の内部スケジュールと厳密に一致させる
            engine = ql.DiscountingSwapEngine(boot.curve)
            n = int(tenor[:-1]) if tenor.endswith("y") else None
            if n is None:
                raise ValueError(f"標準コンベンションのtenorは 'Ny' 形式のみ対応: {tenor!r}")
            ois = ql.MakeOIS(ql.Period(n, ql.Years), boot.index, 0.0)
            ois.setPricingEngine(engine)
            fixrate = ois.fairRate() * 100.0
        else:
            result = price_swap(
                boot.curve, as_of_date, boot.spot_date, fix_freq, fix_dcf, float_freq, float_dcf,
                roll_conv, notional=1.0, pay_rec="PAY", tenor=tenor, index=boot.index,
            )
            fixrate = result.target_fixrate

        results.append(HistoricalPoint(as_of_date=as_of_date, par_rate=fixrate))

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
