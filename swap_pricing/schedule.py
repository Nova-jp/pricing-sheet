"""
スワップのキャッシュフロースケジュール生成モジュール。

roll convention(無調整日付の生成規則)と、休日調整(Modified Following、
Phase 1では常にMF)を分離して扱う:

  - STD: start の day-of-month を保ったまま月を進める(月末オーバーフローはクランプ)
  - EOM: start が月末日なら、以降も常に月末日にロールする
  - IMM: 各periodの月の第3水曜日(IMM日)にロールする

支払日・アニュイティ計算用の日付は、いずれも休日調整後の日付を使う
(Phase 1の簡略化。無調整日でaccrualを計算する規約は今回は扱わない)。
"""

import calendar
from collections import namedtuple
from datetime import date, timedelta
from typing import Optional

from swap_pricing.calendars import modified_following
from swap_pricing.daycount import year_fraction

ROLL_STD = "STD"
ROLL_EOM = "EOM"
ROLL_IMM = "IMM"
SUPPORTED_ROLL = (ROLL_STD, ROLL_EOM, ROLL_IMM)

FREQ_STEP_MONTHS = {
    "PA": 12,
    "SA": 6,
    "QA": 3,
    "1m": 1,
}

Period = namedtuple("Period", ["accrual_start", "accrual_end", "payment_date", "dcf"])


def parse_tenor_months(tenor: str) -> int:
    """'10y' -> 120, '15m' -> 15 のように、年/月表記のテナーを月数に変換する。"""
    s = tenor.strip().lower()
    if s.endswith("y"):
        return int(s[:-1]) * 12
    if s.endswith("m"):
        return int(s[:-1])
    raise ValueError(f"未対応のtenor表記です: {tenor!r} ('y'または'm'サフィックスのみ対応)")


def add_months_clamped(d: date, months: int, force_eom: bool) -> date:
    total = d.month - 1 + months
    year = d.year + total // 12
    month = total % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    day = last_day if force_eom else min(d.day, last_day)
    return date(year, month, day)


def _imm_date(year: int, month: int) -> date:
    """指定した年月の第3水曜日(IMM日)を返す。"""
    first_of_month = date(year, month, 1)
    days_to_wed = (2 - first_of_month.weekday()) % 7  # 水曜日=2
    first_wed = first_of_month + timedelta(days=days_to_wed)
    return first_wed + timedelta(days=14)


def _unadjusted_date(start: date, months_from_start: int, roll_conv: str) -> date:
    if roll_conv == ROLL_IMM:
        total = start.month - 1 + months_from_start
        year = start.year + total // 12
        month = total % 12 + 1
        return _imm_date(year, month)

    force_eom = roll_conv == ROLL_EOM and start.day == calendar.monthrange(start.year, start.month)[1]
    return add_months_clamped(start, months_from_start, force_eom)


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

    ■ 端株(stub)の扱い
      tenor/end が freq の周期で割り切れない場合、"ショート・バックスタブ"
      (start起点でfreqの倍数分の正規期間を並べ、最後に残りの端数期間を
      1本追加する)を自動生成する。端数期間のdcfは通常通りaccrual_start〜
      accrual_endで計算する。
    """
    if roll_conv not in SUPPORTED_ROLL:
        raise ValueError(f"未対応のroll conventionです: {roll_conv!r} (対応: {SUPPORTED_ROLL})")
    if freq not in FREQ_STEP_MONTHS:
        raise ValueError(f"未対応のfreqです: {freq!r} (対応: {tuple(FREQ_STEP_MONTHS)})")
    if (tenor is None) == (end is None):
        raise ValueError("tenor と end のどちらか一方だけを指定してください")

    step_months = FREQ_STEP_MONTHS[freq]

    if tenor is not None:
        total_months = parse_tenor_months(tenor)
        maturity_unadjusted = _unadjusted_date(start, total_months, roll_conv)
    else:
        total_months = None
        maturity_unadjusted = end

    unadjusted_dates = []
    i = 1
    while True:
        if total_months is not None:
            candidate_months = i * step_months
            if candidate_months >= total_months:
                break
            unadjusted_dates.append(_unadjusted_date(start, candidate_months, roll_conv))
        else:
            candidate = _unadjusted_date(start, i * step_months, roll_conv)
            if candidate >= maturity_unadjusted:
                break
            unadjusted_dates.append(candidate)
        i += 1
    unadjusted_dates.append(maturity_unadjusted)  # 最終期間(端株の場合あり)

    adjusted_start = modified_following(start)
    adjusted_dates = [modified_following(d) for d in unadjusted_dates]

    periods = []
    prev = adjusted_start
    for d in adjusted_dates:
        dcf = year_fraction(prev, d, dcf_convention)
        periods.append(Period(accrual_start=prev, accrual_end=d, payment_date=d, dcf=dcf))
        prev = d

    return periods
