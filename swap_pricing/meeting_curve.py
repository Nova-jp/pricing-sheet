"""
BOJ会合スワップ(TraditionのJPBOJ{n}ONI=TRDT)とO/Nから、会合期間ごとにフォワードが一定の
カーブ(階段状のカーブ)を引くモジュール。BOJシートの「会合スワップ → 1年以下のスポット」の
比較(BojImpliedRates)だけで使う。pricingシートのメインのカーブ(curve.py)には影響しない。

■ 入力(日付はLSEGの値をそのまま使う。自前で会合日を計算しない)
  - O/N: 評価日(休日なら翌営業日)→翌営業日の単利(ACT/365F)。メインのカーブの
    DepositRateHelper(1D、settlementDays=0)と同じ日付
  - 会合期間: [(起算日, 満期日, レート%)]。RD=スポット日→会合1の決定日、
    M{n}=会合nの翌営業日→会合n+1の決定日(RealTimeシートのGV1_DATE/GV2_DATE)

■ 会合期間の間の隙間(決定日当日の夜)は直前の期間のフォワードを延ばす
  TONAのフィキシングは営業日だけで、日付Dの値がD→翌営業日の全泊に適用される。決定日当日の
  フィキシングは旧レートで、新しい誘導目標は翌営業日から適用される(日銀の声明と2024〜2026年の
  実績で確認済み。docs/decisions.md)。よって決定日→翌営業日の夜は直前の期間(決定前)のレート。
  評価日→O/N起算日(評価日が休日の場合)と、O/Nの満期→RDの起算日もO/Nのフォワードを延ばす。

■ QuantLibのヘルパー(OISRateHelper)でブートストラップしない理由
  ピラーは商品の満期日以前にしか置けない(QuantLibの制約)。満期=決定日なので、隙間の夜は
  必ず次の期間(決定後)のレートで埋まってしまう(3Mで約0.8bp、12Mで約0.5bpの差)。
  そこで節目のDFだけを「DF(満期) = DF(起算) / (1 + レート × 日数/365)」で順に決め、
  カーブは ql.DiscountCurve(DFの対数線形補間=区間内でフォワード一定)で組む。
  この式は1期間・満期一括払いの複利OIS(支払い遅れなし)のパーレートの定義そのもので、
  QuantLibの評価(price_swap)で各会合スワップが機械精度で戻ることをテストで確認している
  (tests/test_meeting_curve.py)。

■ 範囲外
  最後の会合期間の満期より先はカーブを延ばさない(外挿しない。超える評価はエラー)。
"""

import math
from datetime import date
from typing import List, NamedTuple, Optional, Sequence, Tuple

import QuantLib as ql

from swap_pricing.conventions import CALENDAR
from swap_pricing.curve import BootstrapResult, _standard_spot_date

_DAY_COUNT = ql.Actual365Fixed()


class MeetingPeriod(NamedTuple):
    label: str
    start: date
    end: date
    rate: float  # %


def _year_fraction(start: date, end: date) -> float:
    return _DAY_COUNT.yearFraction(ql.Date.from_date(start), ql.Date.from_date(end))


def on_period(valuation_date: date) -> Tuple[date, date]:
    """O/Nの期間。DepositRateHelper(1D、settlementDays=0、Modified Following)と同じ日付。"""
    start = CALENDAR.adjust(ql.Date.from_date(valuation_date))
    end = CALENDAR.advance(start, 1, ql.Days, ql.ModifiedFollowing)
    return start.to_date(), end.to_date()


def build_meeting_curve(
    valuation_date: date, on_rate: float, periods: Sequence[MeetingPeriod]
) -> BootstrapResult:
    """O/N(%)と会合期間の列からカーブを組む。periodsは起算日の昇順で、重なりがないこと。"""
    if not periods:
        raise ValueError("会合スワップのレートがありません")
    ql.Settings.instance().evaluationDate = ql.Date.from_date(valuation_date)

    on_start, on_end = on_period(valuation_date)
    segments = [MeetingPeriod("o/n", on_start, on_end, on_rate)] + list(periods)

    dates: List[date] = [valuation_date]
    log_dfs: List[float] = [0.0]
    forward: Optional[float] = None  # 直前の区間の連続複利フォワード(ACT/365F)
    for seg in segments:
        if seg.start >= seg.end:
            raise ValueError(f"{seg.label}: 起算日{seg.start}が満期日{seg.end}以降です")
        if seg.start < dates[-1]:
            raise ValueError(
                f"{seg.label}: 起算日{seg.start}が直前の区間の満期{dates[-1]}より前です"
                "(LSEGの会合スワップの日付が古い可能性があります)"
            )
        accrual = _year_fraction(seg.start, seg.end)
        seg_forward = math.log(1.0 + seg.rate / 100.0 * accrual) / accrual
        if seg.start > dates[-1]:
            # 隙間は直前の区間のフォワードを延ばす(最初の区間の前はその区間自身のフォワード)
            gap_forward = seg_forward if forward is None else forward
            log_dfs.append(log_dfs[-1] - gap_forward * _year_fraction(dates[-1], seg.start))
            dates.append(seg.start)
        log_dfs.append(log_dfs[-1] - seg_forward * accrual)
        dates.append(seg.end)
        forward = seg_forward

    curve = ql.DiscountCurve(
        [ql.Date.from_date(d) for d in dates], [math.exp(x) for x in log_dfs], _DAY_COUNT, CALENDAR
    )
    handle = ql.YieldTermStructureHandle(curve)
    index = ql.Tonar(handle)
    return BootstrapResult(
        curve=handle,
        index=index,
        valuation_date=valuation_date,
        spot_date=_standard_spot_date(index),
        used_tenors=[seg.label for seg in segments],
        skipped_tenors=[],
    )
