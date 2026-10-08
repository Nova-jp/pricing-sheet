"""
会合スワップのカーブ(swap_pricing/meeting_curve.py)のリグレッションテスト。

- 各会合スワップとO/Nが、QuantLibの評価(price_swap / Tonarの予測フィキシング)で
  入力レートに機械精度で戻ること(ブートストラップの再現精度)
- 決定日当日の夜(会合期間の隙間)が直前の期間(決定前)のフォワードになること
- 評価日が休日の場合、日付の重なり・範囲外のエラー

日付は2026-10-02(金)時点のLSEG(GV1_DATE/GV2_DATE)の実際の値、レートは架空。
実行方法: python -m unittest tests.test_meeting_curve -v
"""

import sys
import datetime
import unittest
from datetime import date
from unittest import mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import QuantLib as ql  # noqa: E402

from swap_pricing.curve import spot_date_for  # noqa: E402
from swap_pricing.meeting_curve import MeetingPeriod, build_meeting_curve, on_period  # noqa: E402
from swap_pricing.swap_pricer import price_swap  # noqa: E402

D = date.fromisoformat
PERIODS = [
    MeetingPeriod("rd", D("2026-10-06"), D("2026-10-30"), 1.2275),
    MeetingPeriod("m1", D("2026-11-02"), D("2026-12-18"), 1.265),
    MeetingPeriod("m2", D("2026-12-21"), D("2027-01-22"), 1.465),
    MeetingPeriod("m3", D("2027-01-25"), D("2027-03-18"), 1.5575),
    MeetingPeriod("m4", D("2027-03-19"), D("2027-04-28"), 1.68625),
    MeetingPeriod("m5", D("2027-04-30"), D("2027-06-11"), 1.76375),
    MeetingPeriod("m6", D("2027-06-14"), D("2027-07-22"), 1.85625),
    MeetingPeriod("m7", D("2027-07-23"), D("2027-09-22"), 1.94),
    MeetingPeriod("m8", D("2027-09-24"), D("2027-10-29"), 2.0125),
    MeetingPeriod("m9", D("2027-11-01"), D("2027-12-17"), 2.0875),
]
ON_RATE = 1.227
VALUATION = D("2026-10-02")  # 金曜
TOLERANCE_PCT = 1e-8


def _par(boot, valuation_date, start, end=None, tenor=None):
    return price_swap(
        boot.curve, valuation_date, start, "PA", "act/365fixed", "PA", "act/365fixed", "STD",
        notional=1.0, pay_rec="PAY", tenor=tenor, end=end, index=boot.index,
    ).target_fixrate


def _forward(boot, start, end):
    return boot.curve.forwardRate(
        ql.Date.from_date(start), ql.Date.from_date(end), ql.Actual365Fixed(), ql.Continuous
    ).rate()


class MeetingCurveRepricingTest(unittest.TestCase):
    def test_meeting_swaps_reprice(self):
        boot = build_meeting_curve(VALUATION, ON_RATE, PERIODS)
        for p in PERIODS:
            with self.subTest(period=p.label):
                self.assertAlmostEqual(_par(boot, VALUATION, p.start, end=p.end), p.rate, delta=TOLERANCE_PCT)

    def test_on_reprices_as_tona_forecast(self):
        boot = build_meeting_curve(VALUATION, ON_RATE, PERIODS)
        on_start, on_end = on_period(VALUATION)
        self.assertEqual((on_start, on_end), (D("2026-10-02"), D("2026-10-05")))  # 金曜→月曜の3泊
        fixing = boot.index.fixing(ql.Date.from_date(on_start), True) * 100.0
        self.assertAlmostEqual(fixing, ON_RATE, delta=TOLERANCE_PCT)

    def test_spot_date_matches_main_curve(self):
        for valuation_date in (VALUATION, D("2026-10-04"), D("2026-10-05")):
            boot = build_meeting_curve(valuation_date, ON_RATE, PERIODS[1:])
            self.assertEqual(boot.spot_date, spot_date_for(valuation_date))


class GapTest(unittest.TestCase):
    """決定日→翌営業日の夜は直前の期間(決定前)のフォワード。"""

    def test_decision_night_uses_previous_period_forward(self):
        boot = build_meeting_curve(VALUATION, ON_RATE, PERIODS)
        for prev, nxt in zip(PERIODS, PERIODS[1:]):
            with self.subTest(gap=f"{prev.end}->{nxt.start}"):
                gap = _forward(boot, prev.end, nxt.start)
                self.assertAlmostEqual(gap, _forward(boot, prev.start, prev.end), delta=1e-12)
                self.assertNotAlmostEqual(gap, _forward(boot, nxt.start, nxt.end), delta=1e-6)

    def test_tona_forecast_on_decision_day_is_pre_decision(self):
        """10/30(金)のフィキシングは10/30→11/2の3泊。RD期間内の同じ曜日の3泊と同じ単利。"""
        boot = build_meeting_curve(VALUATION, ON_RATE, PERIODS)
        decision_day = boot.index.fixing(ql.Date(30, 10, 2026), True)
        friday_in_rd = boot.index.fixing(ql.Date(23, 10, 2026), True)
        self.assertAlmostEqual(decision_day, friday_in_rd, delta=1e-12)

    def test_on_forward_extends_to_rd_start(self):
        """月曜評価: O/Nは10/5→10/6、RDは10/7起算。10/6→10/7はO/Nのフォワード。"""
        valuation_date = D("2026-10-05")
        periods = [PERIODS[0]._replace(start=D("2026-10-07"))] + PERIODS[1:]
        boot = build_meeting_curve(valuation_date, ON_RATE, periods)
        self.assertAlmostEqual(
            _forward(boot, D("2026-10-06"), D("2026-10-07")),
            _forward(boot, D("2026-10-05"), D("2026-10-06")),
            delta=1e-12,
        )
        self.assertAlmostEqual(_par(boot, valuation_date, periods[0].start, end=periods[0].end),
                               periods[0].rate, delta=TOLERANCE_PCT)


class WeekendAndErrorTest(unittest.TestCase):
    def test_weekend_valuation(self):
        """日曜評価: O/Nは翌営業日(月)→火。日曜→月曜もO/Nのフォワード。"""
        valuation_date = D("2026-10-04")
        self.assertEqual(on_period(valuation_date), (D("2026-10-05"), D("2026-10-06")))
        boot = build_meeting_curve(valuation_date, ON_RATE, PERIODS)
        self.assertAlmostEqual(
            _forward(boot, valuation_date, D("2026-10-05")),
            _forward(boot, D("2026-10-05"), D("2026-10-06")),
            delta=1e-12,
        )
        for p in PERIODS:
            with self.subTest(period=p.label):
                self.assertAlmostEqual(_par(boot, valuation_date, p.start, end=p.end), p.rate, delta=TOLERANCE_PCT)

    def test_start_before_on_end_is_error(self):
        """LSEGの日付が古く、RDの起算日がO/Nの満期より前なら黙って使わない。"""
        with self.assertRaises(ValueError):
            build_meeting_curve(D("2026-10-06"), ON_RATE, PERIODS)

    def test_no_extrapolation_beyond_last_period(self):
        boot = build_meeting_curve(VALUATION, ON_RATE, PERIODS[:3])  # 2027-01-22まで
        _par(boot, VALUATION, D("2026-10-06"), tenor="3m")  # 2027-01-06満期: 範囲内
        with self.assertRaises(RuntimeError):
            _par(boot, VALUATION, D("2026-10-06"), tenor="6m")


class BojImpliedRatesUdfTest(unittest.TestCase):
    """UDF(BojImpliedRates): RealTimeの範囲の読み方と行ごとの結果。"""

    REAL_TIME = [[1, "O/N", ON_RATE], [1, "1W", 1.23]]

    def _meeting_range(self, periods):
        rows = [[p.label.upper(), p.rate, datetime.datetime.combine(p.start, datetime.time()), p.end]
                for p in periods]
        return rows + [["M10", None, None, None]]

    def _call(self, tenors, meeting_range, today=VALUATION, valuation_date=None):
        from swap_pricing import udf
        with mock.patch.object(udf, "_today", lambda: today):
            return udf.BojImpliedRates([[t] for t in tenors], self.REAL_TIME, meeting_range, valuation_date)

    def test_rates_match_curve(self):
        from swap_pricing.meeting_curve import build_meeting_curve as build
        boot = build(VALUATION, ON_RATE, PERIODS)
        result = self._call(["1W", "3M", "12M", ""], self._meeting_range(PERIODS))
        self.assertEqual(result[3], [""])
        for (value,), tenor in zip(result[:3], ["1w", "3m", "12m"]):
            with self.subTest(tenor=tenor):
                self.assertAlmostEqual(value, _par(boot, VALUATION, boot.spot_date, tenor=tenor), delta=1e-12)

    def test_missing_period_cuts_off_later_periods(self):
        """M3の値が欠けたら、M4以降は使わない(M3の期間をM2で埋めない)。12Mは範囲外でエラー文字列。"""
        rows = self._meeting_range(PERIODS)
        rows[3][1] = None
        result = self._call(["2M", "12M"], rows)
        self.assertIsInstance(result[0][0], float)
        self.assertTrue(str(result[1][0]).startswith("エラー"))

    def test_past_valuation_date_is_error(self):
        with self.assertRaises(ValueError):
            self._call(["1W"], self._meeting_range(PERIODS), today=date(2026, 10, 5), valuation_date=VALUATION)


if __name__ == "__main__":
    unittest.main()
