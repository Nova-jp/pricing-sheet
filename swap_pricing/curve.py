"""
JPY OIS(TONA)ディスカウントカーブのブートストラップモジュール。QuantLibを使用。

■ 使用データ
  RealTimeシートのスポットテナー(O/N〜40Y)のみ。M1〜M8(BOJ会合デート物)
  は現時点では使用しない(ユーザー指示により将来フェーズで対応)。

■ O/Nの扱い
  O/Nのみ settlementDays=0, tenor=1日 のOISヘルパーとして評価日(T)起点で
  構築し、それ以外(1W〜40Y)は settlementDays=2 でスポット日(T+2)起点。
  評価日→スポット日のDFは、QuantLibのブートストラップが全ヘルパーを
  同時に整合させる形で解くため、以前の自前実装にあった「O/Nレートを
  T→スポットの期間にそのまま延長する近似」は不要になった。

■ 補間法
  ql.PiecewiseConvexMonotoneForward (Hagan-West Convex Monotone)。
  QuantLibのブートストラップは全pillar確定後の整合性も含めて解くため、
  以前の自前実装で必要だった精緻化パス(_refine_gap_tenors)は不要。

■ 15M/18M/21Mの扱い(include_odd_tenors引数で選択可能)
  デフォルトは不使用(skipped_tenorsに計上)。含める場合、他のテナーと
  全く同じOISRateHelperとして扱う(端株の特別扱いはQuantLib内部に一任)。

■ 未対応(スコープ外)
  - M1〜M8: BOJ会合デート物、今回は使用しない
"""

from datetime import date
from typing import Dict, List, NamedTuple

import QuantLib as ql

from swap_pricing.calendars import add_business_days, CALENDAR
from swap_pricing.daycount import to_ql_date
from swap_pricing.schedule import parse_tenor_months

BULLET_SUB_1Y = [f"{i}m" for i in range(1, 12)]  # 1m〜11m
ODD_TENORS = ["15m", "18m", "21m"]  # include_odd_tenors=Trueの場合のみ使用
ANNUAL_TENORS = [f"{i}y" for i in range(1, 13)] + ["15y", "20y", "25y", "30y", "35y", "40y"]
WEEK_TENORS = ["1w", "2w", "3w"]
EXCLUDED_MEETING_TENORS = {f"m{i}" for i in range(1, 9)}
ON_KEYS = ("o/n", "on", "1d")


class BootstrapResult(NamedTuple):
    curve: ql.YieldTermStructureHandle
    index: "ql.OvernightIndex"
    valuation_date: date
    spot_date: date
    used_tenors: List[str]
    skipped_tenors: List[str]


def bootstrap_curve(
    valuation_date: date, spot_rates: Dict[str, float], include_odd_tenors: bool = False
) -> BootstrapResult:
    """
    spot_rates: {tenor_label: rate(パーセント表記)} 例 {'1Y': 1.135, '5Y': 1.62, ...}
    キーの大小文字・空白は問わない。

    include_odd_tenors: Trueの場合、15M/18M/21Mをブートストラップに含める。
    """
    rates = {k.strip().lower(): v for k, v in spot_rates.items() if v is not None}

    valuation_ql = to_ql_date(valuation_date)
    ql.Settings.instance().evaluationDate = valuation_ql
    spot_date = add_business_days(valuation_date, 2)

    bootstrap_index = ql.Tonar()  # ヘルパー構築専用(カーブに未リンク)
    used_tenors: List[str] = []
    skipped_tenors: List[str] = [t for t in rates if t in EXCLUDED_MEETING_TENORS]
    if not include_odd_tenors:
        skipped_tenors += [t for t in rates if t in ODD_TENORS]

    helpers = []

    on_key = next((k for k in ON_KEYS if k in rates), None)
    if on_key is not None:
        # 1日物のOISRateHelperはQuantLib内部のスケジュール生成が特定の日付で
        # 退化する(degenerate single date)ことがあるため、単純な預金型の
        # DepositRateHelperを使う(O/Nは複利計算不要な1日物のため実質等価)。
        quote = ql.QuoteHandle(ql.SimpleQuote(rates[on_key] / 100.0))
        helpers.append(
            ql.DepositRateHelper(
                quote, ql.Period(1, ql.Days), 0, CALENDAR, ql.ModifiedFollowing, False, ql.Actual365Fixed()
            )
        )
        used_tenors.append(on_key)

    for label in WEEK_TENORS:
        if label not in rates:
            continue
        n = int(label[:-1])
        quote = ql.QuoteHandle(ql.SimpleQuote(rates[label] / 100.0))
        helpers.append(ql.OISRateHelper(2, ql.Period(n, ql.Weeks), quote, bootstrap_index))
        used_tenors.append(label)

    candidate_tenors = list(BULLET_SUB_1Y) + list(ANNUAL_TENORS)
    if include_odd_tenors:
        candidate_tenors += ODD_TENORS
    ordered_tenors = sorted(
        (t for t in candidate_tenors if t in rates), key=parse_tenor_months
    )
    for label in ordered_tenors:
        months = parse_tenor_months(label)
        quote = ql.QuoteHandle(ql.SimpleQuote(rates[label] / 100.0))
        helpers.append(ql.OISRateHelper(2, ql.Period(months, ql.Months), quote, bootstrap_index))
        used_tenors.append(label)

    if not helpers:
        raise ValueError("ブートストラップに使えるレートがありません")

    term_structure = ql.PiecewiseConvexMonotoneForward(valuation_ql, helpers, ql.Actual365Fixed())
    term_structure.enableExtrapolation()
    curve_handle = ql.YieldTermStructureHandle(term_structure)
    index = ql.Tonar(curve_handle)

    return BootstrapResult(
        curve=curve_handle,
        index=index,
        valuation_date=valuation_date,
        spot_date=spot_date,
        used_tenors=used_tenors,
        skipped_tenors=skipped_tenors,
    )
