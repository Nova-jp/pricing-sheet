"""
デイカウントフラクション(dcf)計算モジュール。

対応コンベンション:
  - "act/365fixed": Act/365 Fixed。実日数 / 365
  - "30/360": 30/360 US (Bond Basis, ISDA定義)。
      D1 = day(date1); D1==31 なら D1=30
      D2 = day(date2); D2==31 かつ (調整後)D1==30 なら D2=30
      dcf = (360*(Y2-Y1) + 30*(M2-M1) + (D2-D1)) / 360
      (2月末special caseなし。ISDA 2006 Definitionsの"30/360"に準拠)
"""

from datetime import date

ACT_365_FIXED = "act/365fixed"
THIRTY_360_US = "30/360"

SUPPORTED_DCF = (ACT_365_FIXED, THIRTY_360_US)


def year_fraction(start: date, end: date, convention: str) -> float:
    if convention == ACT_365_FIXED:
        return _act_365_fixed(start, end)
    if convention == THIRTY_360_US:
        return _thirty_360_us(start, end)
    raise ValueError(
        f"未対応のdcfコンベンションです: {convention!r} (対応: {SUPPORTED_DCF})"
    )


def _act_365_fixed(start: date, end: date) -> float:
    return (end - start).days / 365.0


def _thirty_360_us(start: date, end: date) -> float:
    d1 = start.day
    d2 = end.day

    if d1 == 31:
        d1 = 30
    if d2 == 31 and d1 == 30:
        d2 = 30

    days = 360 * (end.year - start.year) + 30 * (end.month - start.month) + (d2 - d1)
    return days / 360.0
