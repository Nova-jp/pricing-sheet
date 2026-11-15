"""
オプションの満期までの時間(スワップション・国債店頭オプションで共通)。

満期までの時間はここだけで数える。プレミアム→volの換算(swaption/convert.py)、ボラの格子の補間
(bond/vol.py)、現物オプションの期間T・セータ(bond/pricer.py)、BondOptionシートのT(表UDF
OptionTermTable)は全てここを使う。volを割った日数と、プライサーに渡すTの日数がずれると、同じ
プレミアムを再現できなくなるため。

■ 数え方
  - 満期までの営業日数 = 取引日から満期日の前日までの営業日数(ql.Japan()、当日を1日と数える)。
  - 年率の日数は1年252営業日。期間 T = 営業日数 / 252。
  - セータの1営業日 = T から 1/252 を引く。
  当日の扱いは、満期日の15時に0になるように日中の残り時間で減らしていく予定(roadmapの16)。
"""

from datetime import date

import QuantLib as ql

from engine.common.conventions import CALENDAR

TRADING_DAYS = 252  # 年率の日数(プロジェクトの規約。BondOption・VolHVと同じ)
ONE_DAY = 1 / TRADING_DAYS  # セータの1営業日(年)


def business_days(trade_date: date, expiry: date) -> int:
    """満期までの営業日数。取引日から満期日の前日まで(当日を1日と数える)。"""
    return CALENDAR.businessDaysBetween(ql.Date.from_date(trade_date), ql.Date.from_date(expiry), True, False)


def option_term(trade_date: date, expiry: date) -> float:
    """期間 T = 満期までの営業日数 / 252(年)。満期が取引日以前ならエラー。"""
    if expiry <= trade_date:
        raise ValueError(f"満期 {expiry} が取引日 {trade_date} 以前です")
    return business_days(trade_date, expiry) / TRADING_DAYS
