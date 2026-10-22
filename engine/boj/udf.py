"""
BOJシート(SwapAnalysis.xlsm)用のUDF(会合スワップ(RD・M1〜)とスポットの歪み比較)。

PricingSheet.xlsm・`engine/swap_pricing/`には依存しない(分析ブックはそれだけで完結させる)。
メインのカーブは`engine.common`(udf_common._market)で、pricingと同じ引き方のものを使う。

Excel境界の型変換(`_is_blank`等)と評価日解決は`engine.common.udf_common`を
`udf_common.xxx(...)`の形で呼ぶ(名前のコピーはしない。理由はudf_common.pyのdocstring)。
"""

import datetime
from typing import Dict, List

import xlwings as xw

from engine.boj.meeting_curve import MeetingPeriod, build_meeting_curve
from engine.common import udf_common
from engine.common.curve import BootstrapResult, ON_KEYS
from engine.common.market_data import Market
from engine.common.risk import price
from engine.common.terms import PAY, STANDARD, SwapParams, SwapTerms


def _meeting_periods(meeting_range) -> List[MeetingPeriod]:
    """会合スワップの行 [Ticker, Value, 起算日, 満期日] → 期間の列(上から順)。

    値か日付が欠けた行(LSEGで未提示のM10以降など)で打ち切る。途中の期間が欠けたまま先の期間を
    使うと、欠けた期間全体が直前の期間のフォワードで埋まってしまうため。
    """
    periods = []
    for row in meeting_range or []:
        if row is None or len(row) < 4 or udf_common._is_blank(row[0]):
            continue
        ticker, value, start, end = row[:4]
        if any(udf_common._is_blank(v) for v in (value, start, end)) or isinstance(value, str):
            break
        periods.append(
            MeetingPeriod(
                str(ticker).strip().lower(), udf_common._to_date(start), udf_common._to_date(end),
                float(value),
            )
        )
    return periods


_meeting_cache: Dict[tuple, BootstrapResult] = {}


def _meeting_curve(real_time_range, meeting_range, valuation_date) -> BootstrapResult:
    valuation_date = udf_common._resolve_valuation_date(valuation_date)
    if valuation_date != udf_common._today():
        raise ValueError("会合スワップのカーブはリアルタイム(評価日=今日)だけに対応しています")
    on_rate = next(
        (float(value) for ticker, value, _ in udf_common._realtime_rows(real_time_range)
         if ticker.lower() in ON_KEYS and not udf_common._is_blank(value) and not isinstance(value, str)),
        None,
    )
    if on_rate is None:
        raise ValueError("ライブのO/Nの値がありません")
    periods = _meeting_periods(meeting_range)
    key = (valuation_date, on_rate, tuple(periods))
    if key not in _meeting_cache:
        _meeting_cache.clear()  # 直近1件だけ保持
        _meeting_cache[key] = build_meeting_curve(valuation_date, on_rate, periods)
    return _meeting_cache[key]


@xw.func
@xw.arg("periods", ndim=2)
@xw.arg("real_time_range", ndim=2)
def BojSpotForwards(periods, real_time_range, valuation_date=None) -> List[list]:
    """メインのカーブ(pricingと同じ引き方)での、各期間(起算日→満期日)のパーレート(%)を返す
    (periodsと同じ行数の1列。標準コンベンション PA/act365f/STD = 会合スワップと同じ1期間の複利)。

    periods: 起算日・満期日の2列(例: BOJ!B8:C17)。どちらかが日付(シリアル値)でない行は空文字。
    real_time_range: フラグ/Ticker/Value の範囲(BOJシートのカーブ用の範囲の TAKE(…#,,3))。
    valuation_date: 空欄=今日。PricingSheetの FairRate(起算日,"",満期日,"PA","act/365fixed","PA","STD",…)
    と同じ値(同じカーブ・同じ評価)。
    """
    curves = udf_common._market(real_time_range, valuation_date)
    result = []
    for row in periods or []:
        dates = (list(row or []) + [None, None])[:2]
        if not all(isinstance(v, (datetime.date, int, float)) and not isinstance(v, bool) for v in dates):
            result.append([""])  # 会合スワップの日付が未提示の行
            continue
        start, end = (udf_common._to_date(v) for v in dates)
        params = SwapParams(SwapTerms(start, STANDARD, end=end), 1.0, PAY)
        try:
            result.append([price(curves.base, params).target_fixrate])
        except (RuntimeError, ValueError) as exc:
            result.append([f"エラー: {exc}"])
    return result


@xw.func
@xw.arg("tenors", ndim=2)
@xw.arg("real_time_range", ndim=2)
@xw.arg("meeting_range", ndim=2)
def BojImpliedRates(tenors, real_time_range, meeting_range, valuation_date=None) -> List[list]:
    """会合スワップ(RD・M1〜)とO/Nで引いた階段状のカーブから、スポット起算のパーレート(%)を返す
    (tenorsと同じ行数の1列。標準コンベンション PA/act365f/STD = ライブのスポットと同じ)。

    tenors: テナーの列(例: BOJ!A22:A34)。real_time_range: フラグ/Ticker/Value の範囲(O/Nを読む)。
    meeting_range: 会合スワップの Ticker/Value/起算日/満期日 の4列(上から RD, M1, …)。
    valuation_date: 空欄=今日(今日以外はエラー)。
    カーブの引き方は engine/boj/meeting_curve.py。最後の会合期間より先のテナーはその行だけ
    エラーの文字列になる。
    """
    boot = _meeting_curve(real_time_range, meeting_range, valuation_date)
    result = []
    for row in tenors or []:
        label = row[0] if row else None
        if udf_common._is_blank(label):
            result.append([""])
            continue
        params = SwapParams(
            SwapTerms(boot.spot_date, STANDARD, tenor=str(label).strip().lower()), 1.0, PAY,
        )
        try:
            market = Market(boot.valuation_date, boot.curve, boot.index)  # スポット起点なので実績は不要
            result.append([price(market, params).target_fixrate])
        except (RuntimeError, ValueError) as exc:
            result.append([f"エラー: {exc}"])
    return result
