"""
Japanese business day calendar utilities.
Day count: Act/365 Fixed (TONA OIS standard)
Business day convention: Modified Following
Spot: T+2 business days for JPY OIS
"""
import jpholiday
from datetime import date, timedelta


def is_holiday_jp(d: date) -> bool:
    """土日または日本の祝日か判定。"""
    if d.weekday() >= 5:  # 土=5, 日=6
        return True
    return bool(jpholiday.is_holiday(d))


def is_business_day(d: date) -> bool:
    return not is_holiday_jp(d)


def next_business_day(d: date) -> date:
    """翌営業日（当日が営業日でも翌日から探す）。"""
    d = d + timedelta(days=1)
    while not is_business_day(d):
        d += timedelta(days=1)
    return d


def prev_business_day(d: date) -> date:
    """前営業日。"""
    d = d - timedelta(days=1)
    while not is_business_day(d):
        d -= timedelta(days=1)
    return d


def add_business_days(d: date, n: int) -> date:
    """n営業日後の日付（n>0）。"""
    count = 0
    while count < n:
        d += timedelta(days=1)
        if is_business_day(d):
            count += 1
    return d


def modified_following(d: date) -> date:
    """
    Modified Following調整。
    次の営業日が翌月になる場合は前営業日に戻す。
    """
    original_month = d.month
    candidate = d
    while not is_business_day(candidate):
        candidate += timedelta(days=1)
    if candidate.month != original_month:
        # 月をまたぐ → 前営業日
        candidate = d
        while not is_business_day(candidate):
            candidate -= timedelta(days=1)
    return candidate


def spot_date(valuation_date: date) -> date:
    """
    JPY OISのスポット日 = バリュエーション日の2営業日後。
    ただし2025年以降はT+1が標準との議論もあるが、
    LSEGのJPBOJxONI慣行に合わせてT+2を使用。
    """
    return add_business_days(valuation_date, 2)


def add_months(d: date, months: int) -> date:
    """日付にmonthsヶ月を加算（月末処理あり）。"""
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    last_day = (date(year, month % 12 + 1, 1) - timedelta(days=1)).day if month < 12 else 31
    day = min(d.day, last_day)
    return date(year, month, day)


def tenor_date(spot: date, months: int) -> date:
    """
    スポット日からmonthsヶ月後の決済日（Modified Following適用）。
    """
    raw = add_months(spot, months)
    return modified_following(raw)


def act365(start: date, end: date) -> float:
    """Act/365 Fixed day fraction。"""
    return (end - start).days / 365.0


def meeting_effective_date(meeting_date: date) -> date:
    """
    BOJ会合の翌営業日 = レート変更の実効日。
    BOJスワップのピリオド区切りに使用。
    """
    return next_business_day(meeting_date)
