"""
LiveScreen.xlsm(engine/live_screen/)のテスト: スポットのスワップの日付、長さの違いによる調整幅、
プレミアム → σ√T と満期までの日数。

実行方法:
    python -m unittest tests.test_live_screen -v
"""

import math
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import QuantLib as ql  # noqa: E402

from engine.common import udf_common  # noqa: E402
from engine.common.market import CurveSet, realtime_snapshot  # noqa: E402
from engine.common.option.swaption.convert import PremiumConverter, calendar_factor  # noqa: E402
from engine.common.swap_pricer import _build_swap, PAY, par_rate  # noqa: E402
from engine.common.terms import STANDARD, SwapTerms  # noqa: E402
from engine.live_screen import day_length, udf, vol  # noqa: E402
from tests.test_curve_repricing import SPOT_RATES  # noqa: E402

T0 = date(2026, 10, 23)  # 金曜


def _market(trade_date=T0):
    return CurveSet(realtime_snapshot(trade_date, {"o/n": 0.477, **{k.lower(): v for k, v in SPOT_RATES.items()}})).base


class DatesTest(unittest.TestCase):
    def test_business_days_around_skip_weekends_and_holidays(self):
        days = day_length.business_days_around(date(2026, 11, 3), 1, 1)  # 11/3(文化の日)→ 11/4を中心に
        self.assertEqual(days, [date(2026, 11, 2), date(2026, 11, 4), date(2026, 11, 5)])
        self.assertEqual(day_length.previous_business_day(date(2026, 10, 26)), T0)  # 月曜 → 金曜

    def test_swap_dates_are_spot_and_the_quantlib_maturity(self):
        d = day_length.swap_dates_on(T0, "1Y")
        self.assertEqual(d.spot, date(2026, 10, 27))
        self.assertEqual(d.maturity, date(2027, 10, 27))
        self.assertEqual(d.days, 365)
        self.assertEqual(day_length.swap_dates_on(T0, "12M"), d)  # 12M = 1Y

    def test_overnight_and_meeting_swaps_are_rejected(self):
        for label in ("O/N", "M1", "RD"):
            with self.assertRaises(ValueError):
                day_length.swap_dates_on(T0, label)


class LengthAdjustmentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.market = _market()

    def test_unadjusted_end_keeps_a_holiday_maturity(self):
        end = date(2027, 10, 30)  # 土曜
        swap = _build_swap(ql.Tonar(), SwapTerms(start=date(2026, 10, 27), convention=STANDARD, end=end,
                                                 adjust_end=False), 1.0, PAY, 0.0)
        self.assertEqual(swap.fixedSchedule().dates()[-1].to_date(), end)
        adjusted = _build_swap(ql.Tonar(), SwapTerms(start=date(2026, 10, 27), convention=STANDARD, end=end),
                               1.0, PAY, 0.0)
        self.assertEqual(adjusted.fixedSchedule().dates()[-1].to_date(), date(2027, 10, 29))  # Modified Following

    def test_explicit_business_day_end_equals_the_tenor_swap(self):
        # 1本目(日付で指定・休日調整なし)は、満期日が営業日なら普通のスポットのスワップと同じパーレート
        d = day_length.swap_dates_on(T0, "5Y")
        a = par_rate(self.market, SwapTerms(start=d.spot, convention=STANDARD, tenor="5Y")).target_fixrate
        self.assertAlmostEqual(day_length._par(self.market, d.spot, d.maturity), a, places=12)

    def test_zero_when_the_length_is_the_same(self):
        same = 0
        for label in ("1M", "3M", "6M", "1Y", "2Y", "5Y", "10Y", "20Y"):
            today, prev = day_length.swap_dates_on(T0, label), day_length.swap_dates_on(date(2026, 10, 22), label)
            if today.days == prev.days:
                same += 1
                self.assertEqual(day_length.length_adjustment(self.market, label), (today.days, prev.days, 0.0))
        self.assertGreater(same, 0)

    def test_shorter_than_yesterday_on_an_upward_curve_is_negative(self):
        # 2026-10-23(金)の1Mは 10/27→11/27(31日)、前の営業日 10/22 は 10/26→11/26(31日)。
        # 長さの違う日を探して、符号が「短いほど低い」になることを確かめる(カーブは右上がり)
        found = False
        for n in range(1, 40):
            d = T0 + timedelta(days=n)
            if not ql.Japan().isBusinessDay(ql.Date.from_date(d)):
                continue
            m = _market(d)
            a = day_length.length_adjustment(m, "3M")
            if a.days != a.prev_days:
                found = True
                expect = (day_length._par(m, *self._dates(d, "3M"))
                          - day_length._par(m, self._dates(d, "3M")[0],
                                            self._dates(d, "3M")[0] + timedelta(days=a.prev_days)))
                self.assertAlmostEqual(a.adjustment, expect * 100, places=12)
                self.assertEqual(a.adjustment < 0, a.days < a.prev_days)
                self.assertNotEqual(a.adjustment, 0.0)  # 長くなった日も0にならない(満期日を休日調整しない)
        self.assertTrue(found)

    @staticmethod
    def _dates(d, label):
        x = day_length.swap_dates_on(d, label)
        return x.spot, x.maturity


class VolTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        m = _market()
        cls.curve = m.curve

    def test_stdev_gives_the_feed_vol_and_the_calendar_vol(self):
        grid = vol.stdev_grid(self.curve, T0, T0, ["1M", "1Y"], ["2Y", "10Y"], [[130.0, ""], [None, 450.0]])
        self.assertIsNone(grid[0][1])
        self.assertIsNone(grid[1][0])
        ql.Settings.instance().evaluationDate = ql.Date.from_date(T0)
        v = PremiumConverter(self.curve, T0).convert("1Yx10Y", 450.0)
        self.assertAlmostEqual(grid[1][1], v.stdev, places=12)
        days = vol.expiry_days(T0, T0, "1Y")
        self.assertEqual(days.expiry, v.expiry)
        self.assertEqual(days.business_days, v.days)
        # 営業日ベース = σ√T/√N×√252、暦日ベース = σ√T/√C×√365 = 営業日ベース × calendar_factor
        self.assertAlmostEqual(grid[1][1] / math.sqrt(days.business_days) * math.sqrt(252), v.vol, places=10)
        self.assertAlmostEqual(grid[1][1] / math.sqrt(days.calendar_days) * math.sqrt(365),
                               v.vol * calendar_factor(T0, "1Y"), places=10)

    def test_shape_mismatch_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "満期2行"):
            vol.stdev_grid(self.curve, T0, T0, ["1M", "1Y"], ["2Y"], [[1.0]])


class UdfTest(unittest.TestCase):
    RATES = [[1, "O/N", 0.477]] + [["", k, v] for k, v in SPOT_RATES.items()]

    def setUp(self):
        patches = [
            mock.patch.object(udf_common, "_today", return_value=T0),
            mock.patch.object(udf_common, "_now", return_value=datetime(2026, 10, 23, 15, 0)),
            mock.patch.object(udf_common, "_db_fixings", return_value={}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_date_grid_items_and_blanks(self):
        dates = [T0, "", date(2026, 10, 26)]
        grid = udf.SwapDateGrid(dates, ["1Y", "", "O/N"], "Days")
        self.assertEqual(grid[0], [365, "", ""])
        self.assertEqual(grid[1], ["", "", ""])
        self.assertEqual(udf.SwapDateGrid([T0], ["1Y"], "Spot"), [[date(2026, 10, 27)]])
        self.assertEqual(udf.SwapDateGrid([T0], ["1Y"], "Maturity"), [[date(2027, 10, 27)]])
        chg = udf.SwapDateGrid([T0], ["1Y"], "DaysChg")[0][0]
        prev = day_length.swap_dates_on(date(2026, 10, 22), "1Y").days
        self.assertEqual(chg, 365 - prev)
        with self.assertRaisesRegex(ValueError, "item"):
            udf.SwapDateGrid([T0], ["1Y"], "Length")

    def test_business_days(self):
        self.assertEqual(udf.SwapBusinessDays(T0, 1, 1), [[date(2026, 10, 22)], [T0], [date(2026, 10, 26)]])
        # 暦だけで決まるので将来の日付も受け付ける(2026-10-24は土曜 → 次の営業日の10-26が中心)
        self.assertEqual(udf.SwapBusinessDays(date(2026, 10, 24), 0, 0), [[date(2026, 10, 26)]])
        with self.assertRaisesRegex(ValueError, "空欄"):
            udf.SwapBusinessDays("", 1, 1)

    def test_length_adjust_rows(self):
        rows = udf.SwapLengthAdjust(self.RATES, ["5Y", "", "O/N"], T0)
        a = day_length.length_adjustment(_market(), "5Y")  # 1826日 ← 前の営業日 1827日
        self.assertEqual(rows[0], [a.days, a.prev_days, a.days - a.prev_days, a.adjustment, ""])
        self.assertEqual(rows[0][2], -1)
        self.assertLess(rows[0][3], 0)
        self.assertEqual(rows[1], ["", "", "", "", ""])
        self.assertEqual(rows[2][:4], ["", "", "", ""])
        self.assertIn("O/N", rows[2][4])

    def test_vol_udfs(self):
        stdev = udf.SwaptionStdevGrid(self.RATES, ["1Y"], ["10Y"], [[450.0]], T0)
        ql.Settings.instance().evaluationDate = ql.Date.from_date(T0)
        expect = PremiumConverter(_market().curve, T0, when=datetime(2026, 10, 23, 15, 0)).convert("1Yx10Y", 450.0)
        self.assertAlmostEqual(stdev[0][0], expect.stdev, places=10)
        days = udf.SwaptionExpiryDays(["1Y", ""], [[450.0]], T0)
        self.assertEqual(days[0], [date(2027, 10, 25), expect.days, (date(2027, 10, 25) - T0).days])
        self.assertEqual(days[1], ["", "", ""])

    def test_live_udfs_need_today(self):
        """ライブのレートを使うUDFは取引日を今日だけ受け付ける(空欄・過去日・未来日はエラー。DBの引けと混ぜない)。"""
        calls = [
            lambda d: udf.SwapLengthAdjust(self.RATES, ["5Y"], d),
            lambda d: udf.SwaptionStdevGrid(self.RATES, ["1Y"], ["10Y"], [[450.0]], d),
            lambda d: udf.SwaptionExpiryDays(["1Y"], [[450.0]], d),
        ]
        for call in calls:
            with self.assertRaisesRegex(ValueError, "空欄"):
                call("")
            with self.assertRaisesRegex(ValueError, "今日にしてください"):
                call(date(2026, 10, 22))
            with self.assertRaisesRegex(ValueError, "未来日"):
                call(date(2026, 10, 26))


if __name__ == "__main__":
    unittest.main()
