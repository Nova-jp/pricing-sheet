"""
ASWシート用UDF(AswHistoricalRate / AswHistoricalRates)のリグレッションテスト。

実行方法:
    python -m unittest tests.test_asw -v
"""

import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from engine.common import udf_common  # noqa: E402
from engine.common.historical_store import AswRate, HistoricalStore  # noqa: E402
from engine.swap_pricing.asw import udf as asw_udf  # noqa: E402

D0 = date(2026, 10, 7)
D1 = date(2026, 10, 8)
D2 = date(2026, 10, 9)


def _store_with_sample(tmp):
    store = HistoricalStore(Path(tmp) / "historical.db")
    store.save_asw_day(D0, {"AAA": AswRate(0.10, 0.20), "BBB": AswRate(0.40, 0.50), "CCC": AswRate(0.70, 0.80)}, "h")
    store.save_asw_day(D1, {"AAA": AswRate(0.15, 0.25), "BBB": AswRate(0.42, 0.52), "CCC": AswRate(0.72, 0.82)}, "h")
    # D2のBBBは欠損(別銘柄の発行日違い等を想定)
    store.save_asw_day(D2, {"AAA": AswRate(0.20, 0.30), "CCC": AswRate(0.74, 0.84)}, "h")
    return store


class AswHistoricalRateTest(unittest.TestCase):
    def test_returns_pa_or_sa_series(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store_with_sample(tmp)
            with mock.patch.object(udf_common, "_historical_store", lambda: store):
                pa = asw_udf.AswHistoricalRate("AAA", "PA", "", "", D2)
                sa = asw_udf.AswHistoricalRate("AAA", "sa", "", "", D2)  # 大文字小文字は問わない
        self.assertEqual(pa, [["Date", "Rate"], [D0, 0.10], [D1, 0.15], [D2, 0.20]])
        self.assertEqual(sa, [["Date", "Rate"], [D0, 0.20], [D1, 0.25], [D2, 0.30]])

    def test_date_range_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store_with_sample(tmp)
            with mock.patch.object(udf_common, "_historical_store", lambda: store):
                rows = asw_udf.AswHistoricalRate("AAA", "PA", D1, D1, D2)
        self.assertEqual(rows, [["Date", "Rate"], [D1, 0.15]])

    def test_invalid_convention_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store_with_sample(tmp)
            with mock.patch.object(udf_common, "_historical_store", lambda: store):
                with self.assertRaisesRegex(ValueError, "PA/SA"):
                    asw_udf.AswHistoricalRate("AAA", "XX", "", "", D2)


class AswHistoricalRatesTest(unittest.TestCase):
    def test_single_leg_is_outright(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store_with_sample(tmp)
            with mock.patch.object(udf_common, "_historical_store", lambda: store):
                rows = asw_udf.AswHistoricalRates([["AAA"]], "PA", "", "", D2)
        self.assertEqual(rows, [["Date", "A", "B", "C"], [D0, 0.10, "", ""], [D1, 0.15, "", ""], [D2, 0.20, "", ""]])

    def test_two_legs_align_by_date_with_blanks_for_missing(self):
        """BBBがD2に欠損していても、日付の並びは全銘柄の和集合(その脚だけ空欄)。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store_with_sample(tmp)
            with mock.patch.object(udf_common, "_historical_store", lambda: store):
                rows = asw_udf.AswHistoricalRates([["AAA"], ["BBB"]], "PA", "", "", D2)
        self.assertEqual(rows, [
            ["Date", "A", "B", "C"],
            [D0, 0.10, 0.40, ""],
            [D1, 0.15, 0.42, ""],
            [D2, 0.20, "", ""],
        ])

    def test_three_legs_for_fly(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store_with_sample(tmp)
            with mock.patch.object(udf_common, "_historical_store", lambda: store):
                rows = asw_udf.AswHistoricalRates([["AAA"], ["BBB"], ["CCC"]], "SA", "", "", D2)
        self.assertEqual(rows, [
            ["Date", "A", "B", "C"],
            [D0, 0.20, 0.50, 0.80],
            [D1, 0.25, 0.52, 0.82],
            [D2, 0.30, "", 0.84],
        ])

    def test_no_legs_returns_empty_series(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store_with_sample(tmp)
            with mock.patch.object(udf_common, "_historical_store", lambda: store):
                rows = asw_udf.AswHistoricalRates([["" , "", ""]], "PA", "", "", D2)
        self.assertEqual(rows, [["Date", "A", "B", "C"]])

    def test_too_many_legs_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store_with_sample(tmp)
            with mock.patch.object(udf_common, "_historical_store", lambda: store):
                with self.assertRaisesRegex(ValueError, "最大3本"):
                    asw_udf.AswHistoricalRates([["AAA"], ["BBB"], ["CCC"], ["AAA"]], "PA", "", "", D2)

    def test_date_range_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store_with_sample(tmp)
            with mock.patch.object(udf_common, "_historical_store", lambda: store):
                rows = asw_udf.AswHistoricalRates([["AAA"]], "PA", D0, D0 + timedelta(days=0), D2)
        self.assertEqual(rows, [["Date", "A", "B", "C"], [D0, 0.10, "", ""]])

    def test_trade_date_caps_the_end(self):
        """評価日より後の日付は返さない(終了日の空欄・評価日より後の終了日は評価日まで)。評価日は必須。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store_with_sample(tmp)
            with mock.patch.object(udf_common, "_historical_store", lambda: store):
                blank_end = asw_udf.AswHistoricalRates([["AAA"]], "PA", "", "", D1)
                late_end = asw_udf.AswHistoricalRates([["AAA"]], "PA", D0, D2, D1)
                single = asw_udf.AswHistoricalRate("AAA", "PA", "", D2, D1)
                with self.assertRaisesRegex(ValueError, "評価日"):
                    asw_udf.AswHistoricalRates([["AAA"]], "PA", "", "", "")
                with self.assertRaisesRegex(ValueError, "評価日"):
                    asw_udf.AswHistoricalRate("AAA", "PA", "", "", None)
        expect = [["Date", "A", "B", "C"], [D0, 0.10, "", ""], [D1, 0.15, "", ""]]
        self.assertEqual(blank_end, expect)
        self.assertEqual(late_end, expect)
        self.assertEqual(single, [["Date", "Rate"], [D0, 0.10], [D1, 0.15]])


if __name__ == "__main__":
    unittest.main()
