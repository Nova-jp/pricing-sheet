"""
デイカウントフラクション(dcf)計算モジュール。QuantLibのDayCounterを使う。

対応コンベンション:
  - "act/365fixed": Act/365 Fixed
  - "30/360": 30/360 US(Bond Basis, ISDA定義)。QuantLibの Thirty360.BondBasis が
    厳密に一致することを確認済み(Feb28,2007->Aug31,2007 = 183/360)。
    ※ Thirty360.USA は2月末特例が入るため別物(不一致)。BondBasisを使うこと。
"""

from datetime import date

import QuantLib as ql

ACT_365_FIXED = "act/365fixed"
THIRTY_360_US = "30/360"

SUPPORTED_DCF = (ACT_365_FIXED, THIRTY_360_US)


def to_ql_date(d: date) -> ql.Date:
    return ql.Date(d.day, d.month, d.year)


def day_counter(convention: str) -> ql.DayCounter:
    if convention == ACT_365_FIXED:
        return ql.Actual365Fixed()
    if convention == THIRTY_360_US:
        return ql.Thirty360(ql.Thirty360.BondBasis)
    raise ValueError(
        f"未対応のdcfコンベンションです: {convention!r} (対応: {SUPPORTED_DCF})"
    )


def year_fraction(start: date, end: date, convention: str) -> float:
    dc = day_counter(convention)
    return dc.yearFraction(to_ql_date(start), to_ql_date(end))
