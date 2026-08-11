"""
スワップのキャッシュフロースケジュール生成モジュール。QuantLibの ql.Schedule を使う。

roll convention:
  - STD: ql.DateGeneration.Forward(start起点で正規期間を並べ、端数はショート・バックスタブ)
  - EOM: 同じくForwardだが endOfMonth=True (startが月末なら常に月末にロール)
  - IMM: ql.DateGeneration.ThirdWednesday(各周期の月の第3水曜日にロール)

休日調整は常にModified Following(Phase 1の方針通り)。
支払日・アニュイティ計算用の日付は、いずれも休日調整後の日付を使う。
"""

from collections import namedtuple
from datetime import date
from typing import Optional

import QuantLib as ql

from swap_pricing.calendars import CALENDAR
from swap_pricing.daycount import day_counter, to_ql_date

ROLL_STD = "STD"
ROLL_EOM = "EOM"
ROLL_IMM = "IMM"
SUPPORTED_ROLL = (ROLL_STD, ROLL_EOM, ROLL_IMM)

FREQ_TO_QL_PERIOD = {
    "PA": ql.Period(1, ql.Years),
    "SA": ql.Period(6, ql.Months),
    "QA": ql.Period(3, ql.Months),
    "1m": ql.Period(1, ql.Months),
}
FREQ_STEP_MONTHS = {"PA": 12, "SA": 6, "QA": 3, "1m": 1}

Period = namedtuple("Period", ["accrual_start", "accrual_end", "payment_date", "dcf"])


def parse_tenor_months(tenor: str) -> int:
    """'10y' -> 120, '15m' -> 15 のように、年/月表記のテナーを月数に変換する。"""
    s = tenor.strip().lower()
    if s.endswith("y"):
        return int(s[:-1]) * 12
    if s.endswith("m"):
        return int(s[:-1])
    raise ValueError(f"未対応のtenor表記です: {tenor!r} ('y'または'm'サフィックスのみ対応)")


def _from_ql_date(d: ql.Date) -> date:
    return date(d.year(), d.month(), d.dayOfMonth())


def generate_ql_schedule(
    start: date,
    freq: str,
    roll_conv: str,
    tenor: Optional[str] = None,
    end: Optional[date] = None,
) -> ql.Schedule:
    """生の ql.Schedule オブジェクトを返す(swap_pricer.pyがOvernightIndexedSwap構築に使う)。"""
    if roll_conv not in SUPPORTED_ROLL:
        raise ValueError(f"未対応のroll conventionです: {roll_conv!r} (対応: {SUPPORTED_ROLL})")
    if freq not in FREQ_TO_QL_PERIOD:
        raise ValueError(f"未対応のfreqです: {freq!r} (対応: {tuple(FREQ_TO_QL_PERIOD)})")
    if (tenor is None) == (end is None):
        raise ValueError("tenor と end のどちらか一方だけを指定してください")

    effective = to_ql_date(start)

    if end is not None:
        termination = to_ql_date(end)
    else:
        total_months = parse_tenor_months(tenor)
        end_of_month = roll_conv == ROLL_EOM and CALENDAR.isEndOfMonth(effective)
        termination = CALENDAR.advance(
            effective, ql.Period(total_months, ql.Months), ql.Unadjusted, end_of_month
        )

    if roll_conv == ROLL_IMM:
        rule = ql.DateGeneration.ThirdWednesday
        end_of_month = False
    elif roll_conv == ROLL_EOM:
        rule = ql.DateGeneration.Forward
        end_of_month = CALENDAR.isEndOfMonth(effective)
    else:
        rule = ql.DateGeneration.Forward
        end_of_month = False

    return ql.Schedule(
        effective,
        termination,
        FREQ_TO_QL_PERIOD[freq],
        CALENDAR,
        ql.ModifiedFollowing,
        ql.ModifiedFollowing,
        rule,
        end_of_month,
    )


def generate_schedule(
    start: date,
    freq: str,
    roll_conv: str,
    dcf_convention: str,
    tenor: Optional[str] = None,
    end: Optional[date] = None,
) -> list:
    """
    固定脚 or 変動脚どちらか一方の Period リストを生成する
    (freq/dcfが脚ごとに異なるため、脚ごとに1回ずつ呼び出す想定)。

    tenor か end のどちらか一方を指定する。
    """
    ql_schedule = generate_ql_schedule(start, freq, roll_conv, tenor=tenor, end=end)

    dc = day_counter(dcf_convention)
    dates = [_from_ql_date(d) for d in ql_schedule]

    periods = []
    for i in range(1, len(dates)):
        accrual_start, accrual_end = dates[i - 1], dates[i]
        dcf = dc.yearFraction(to_ql_date(accrual_start), to_ql_date(accrual_end))
        periods.append(
            Period(accrual_start=accrual_start, accrual_end=accrual_end, payment_date=accrual_end, dcf=dcf)
        )
    return periods
