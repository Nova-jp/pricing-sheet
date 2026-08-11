"""
JPY営業日カレンダーと休日調整コンベンション。QuantLibの ql.Japan() を使う。

jpholiday(祝日法ベース)と異なり、ql.Japan()は1/2・1/3等の金融機関特有の
休業日も正しく非営業日として扱う(確認済み)。
"""

from datetime import date

import QuantLib as ql

from swap_pricing.daycount import to_ql_date

CALENDAR = ql.Japan()
_CALENDAR = CALENDAR  # 後方互換用エイリアス


def _from_ql_date(d: ql.Date) -> date:
    return date(d.year(), d.month(), d.dayOfMonth())


def is_business_day(d: date) -> bool:
    return _CALENDAR.isBusinessDay(to_ql_date(d))


def modified_following(d: date) -> date:
    return _from_ql_date(_CALENDAR.adjust(to_ql_date(d), ql.ModifiedFollowing))


def add_business_days(d: date, n: int) -> date:
    """d から n 営業日後の日付を返す(スポット日=T+2の算出等に使用)。"""
    return _from_ql_date(_CALENDAR.advance(to_ql_date(d), n, ql.Days))
