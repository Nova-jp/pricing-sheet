"""
スポットのスワップの日付と、前の営業日と日数(長さ)が違うことによるレートのずれ(LiveScreen.xlsm)。

■ 日付
  取引日のスポット日(T+2、curve.spot_date_for)を起算日に、テナーのスワップを標準の規約(STANDARD)で組んだときの
  満期日(休日調整後)。日付はQuantLibのスケジュール(swap_pricer.swap_dates)から読む。長さ = 満期日 − 起算日(暦日)。

■ 日数の調整幅(ユーザー決定 2026-11-15「長さだけ」)
  前の営業日のスポットのスワップと今日のスポットのスワップで、長さ(暦日)が違う分のレートのずれ。
    調整幅 = パーレート(今日の起算日 → 今日の満期日)
           − パーレート(今日の起算日 → 今日の起算日 + 前の営業日の長さ)
  - どちらも同じカーブ(今のライブのカーブ)で評価し、起算日は同じにする。起算日が1日先へずれる分(期間の前進)は
    入れない(入れると、長さが同じ日でも1Yで毎日約+0.2bp出る。2026-11-15に実DBで確認)。
  - 2本目の満期日は休日調整しない(SwapTerms.adjust_end=False)。調整すると休日に落ちた日が翌営業日へ寄って
    今日の満期日と同じになり、長くなった日の調整幅が0になってしまう。比べやすいように1本目も同じ組み方
    (満期日を日付で指定、休日調整なし。満期日は営業日なので値は普通のスワップと同じ)にする。
  - 今日のほうが短く、カーブが右上がりなら負になる。カーブ・フライの調整幅はExcelでアウトライトを足し引きする。
"""

from datetime import date, timedelta
from functools import lru_cache
from typing import NamedTuple, Optional

import QuantLib as ql

from engine.common.conventions import CALENDAR
from engine.common.curve import normalize_tenor, spot_date_for
from engine.common.market_data import Market
from engine.common.swap_pricer import par_rate, swap_dates
from engine.common.terms import STANDARD, SwapTerms

ON_LABEL = "o/n"


class SwapDates(NamedTuple):
    trade_date: date
    spot: date  # 起算日
    maturity: date  # 満期日(休日調整後)

    @property
    def days(self) -> int:
        """長さ(暦日)。"""
        return (self.maturity - self.spot).days


class LengthAdjustment(NamedTuple):
    days: int  # 今日の長さ(暦日)
    prev_days: int  # 前の営業日の長さ(暦日)
    adjustment: float  # 調整幅(bp)。長さが同じなら0


def previous_business_day(d: date) -> date:
    return CALENDAR.advance(ql.Date.from_date(d), -1, ql.Days).to_date()


def business_days_around(center: date, before: int, after: int) -> list:
    """center の前後の営業日(昇順)。center が休日なら、その次の営業日を中心にする。"""
    if before < 0 or after < 0:
        raise ValueError("前後の日数は0以上にしてください")
    mid = CALENDAR.adjust(ql.Date.from_date(center), ql.Following)
    return [CALENDAR.advance(mid, n, ql.Days).to_date() for n in range(-before, after + 1)]


def _tenor(label) -> str:
    key = normalize_tenor(str(label))
    if key == ON_LABEL:
        raise ValueError("O/Nは対象外です(長さが常に1営業日)")
    try:
        ql.PeriodParser.parse(key.upper())
    except RuntimeError:
        raise ValueError(f"テナーとして解釈できません(会合スワップは対象外): {label!r}") from None
    return key


def swap_dates_on(trade_date: date, label) -> SwapDates:
    """取引日 trade_date のスポットのスワップの起算日・満期日。"""
    return _swap_dates_on(trade_date, _tenor(label))


@lru_cache(maxsize=4096)
def _swap_dates_on(trade_date: date, tenor: str) -> SwapDates:
    # 日付は取引日とテナーだけで決まる(カーブに依らない)ので覚えておく。ティックのたびに組み直すと
    # 調整幅の計算が1回0.7秒かかった(34テナー)。spot_date_for は評価日を書き換えるが、par_rate は入口で設定し直す
    spot = spot_date_for(trade_date)
    start, end = swap_dates(SwapTerms(start=spot, convention=STANDARD, tenor=tenor))
    return SwapDates(trade_date, start, end)


def _par(market: Market, start: date, end: date) -> float:
    return par_rate(market, SwapTerms(start=start, convention=STANDARD, end=end, adjust_end=False)).target_fixrate


def length_adjustment(market: Market, label, trade_date: Optional[date] = None) -> LengthAdjustment:
    """market(今のカーブ)で、取引日(省略=評価日)と前の営業日の長さの違いによる調整幅(bp)。"""
    trade_date = market.valuation_date if trade_date is None else trade_date
    today = swap_dates_on(trade_date, label)
    prev = swap_dates_on(previous_business_day(trade_date), label)
    if today.days == prev.days:
        return LengthAdjustment(today.days, prev.days, 0.0)
    shifted = today.spot + timedelta(days=prev.days)
    diff = _par(market, today.spot, today.maturity) - _par(market, today.spot, shifted)
    return LengthAdjustment(today.days, prev.days, diff * 100.0)
