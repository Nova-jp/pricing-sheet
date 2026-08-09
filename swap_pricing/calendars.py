"""
JPY営業日カレンダーと休日調整コンベンション。

営業日判定: 土日 + 日本の祝日(jpholidayライブラリ)を非営業日として扱う。
休日調整は Modified Following (MF) のみ対応(roll conventionに関わらず、
今回のPhase 1では常にMFを使う、というユーザー指定に基づく)。
"""

from datetime import date, timedelta

import jpholiday


def is_business_day(d: date) -> bool:
    if d.weekday() >= 5:  # 5=土, 6=日
        return False
    if jpholiday.is_holiday(d):
        return False
    return True


def modified_following(d: date) -> date:
    """
    Modified Following: 非営業日なら翌営業日へ。
    ただし月をまたぐ場合は、代わりに前営業日へ戻す。
    """
    adjusted = d
    while not is_business_day(adjusted):
        adjusted += timedelta(days=1)

    if adjusted.month != d.month:
        adjusted = d
        while not is_business_day(adjusted):
            adjusted -= timedelta(days=1)

    return adjusted


def add_business_days(d: date, n: int) -> date:
    """d から n 営業日後の日付を返す(スポット日=T+2の算出等に使用)。"""
    current = d
    remaining = n
    step = 1 if n >= 0 else -1
    while remaining != 0:
        current += timedelta(days=step)
        if is_business_day(current):
            remaining -= step
    return current
