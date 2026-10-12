"""
ASWシート用UDF(AswHistoricalRate / AswHistoricalRates)のリグレッションテスト。

実行方法:
    python -m unittest tests.test_asw -v
"""

import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from engine.common import udf_common  # noqa: E402
from engine.common.historical_store import AswQuoteRow, HistoricalStore  # noqa: E402
from engine.swap_pricing.asw import udf as asw_udf  # noqa: E402

D0 = date(2026, 10, 7)
D1 = date(2026, 10, 8)
D2 = date(2026, 10, 9)


def _store_with_sample(tmp):
    store = HistoricalStore(Path(tmp) / "historical.db")
    store.save_asw_quotes([
        AswQuoteRow(D0, "AAA", 0.10, 0.20, datetime(2026, 10, 9)),
        AswQuoteRow(D1, "AAA", 0.15, 0.25, datetime(2026, 10, 9)),
        AswQuoteRow(D2, "AAA", 0.20, 0.30, datetime(2026, 10, 9)),
        AswQuoteRow(D0, "BBB", 0.40, 0.50, datetime(2026, 10, 9)),
        AswQuoteRow(D1, "BBB", 0.42, 0.52, datetime(2026, 10, 9)),
        # D2のBBBは欠損(別銘柄の発行日違い等を想定)
        AswQuoteRow(D0, "CCC", 0.70, 0.80, datetime(2026, 10, 9)),
        AswQuoteRow(D1, "CCC", 0.72, 0.82, datetime(2026, 10, 9)),
        AswQuoteRow(D2, "CCC", 0.74, 0.84, datetime(2026, 10, 9)),
    ])
    return store


class AswHistoricalRateTest(unittest.TestCase):
    def test_returns_pa_or_sa_series(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store_with_sample(tmp)
            with mock.patch.object(udf_common, "_historical_store", lambda: store):
                pa = asw_udf.AswHistoricalRate("AAA", "PA")
                sa = asw_udf.AswHistoricalRate("AAA", "sa")  # 大文字小文字は問わない
        self.assertEqual(pa, [["Date", "Rate"], [D0, 0.10], [D1, 0.15], [D2, 0.20]])
        self.assertEqual(sa, [["Date", "Rate"], [D0, 0.20], [D1, 0.25], [D2, 0.30]])

    def test_date_range_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store_with_sample(tmp)
            with mock.patch.object(udf_common, "_historical_store", lambda: store):
                rows = asw_udf.AswHistoricalRate("AAA", "PA", from_date=D1, to_date=D1)
        self.assertEqual(rows, [["Date", "Rate"], [D1, 0.15]])

    def test_invalid_convention_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store_with_sample(tmp)
            with mock.patch.object(udf_common, "_historical_store", lambda: store):
                with self.assertRaisesRegex(ValueError, "PA/SA"):
                    asw_udf.AswHistoricalRate("AAA", "XX")


class AswHistoricalRatesTest(unittest.TestCase):
    def test_single_leg_is_outright(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store_with_sample(tmp)
            with mock.patch.object(udf_common, "_historical_store", lambda: store):
                rows = asw_udf.AswHistoricalRates([["AAA"]], "PA")
        self.assertEqual(rows, [["Date", "A", "B", "C"], [D0, 0.10, "", ""], [D1, 0.15, "", ""], [D2, 0.20, "", ""]])

    def test_two_legs_align_by_date_with_blanks_for_missing(self):
        """BBBがD2に欠損していても、日付の並びは全銘柄の和集合(その脚だけ空欄)。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store_with_sample(tmp)
            with mock.patch.object(udf_common, "_historical_store", lambda: store):
                rows = asw_udf.AswHistoricalRates([["AAA"], ["BBB"]], "PA")
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
                rows = asw_udf.AswHistoricalRates([["AAA"], ["BBB"], ["CCC"]], "SA")
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
                rows = asw_udf.AswHistoricalRates([["" , "", ""]], "PA")
        self.assertEqual(rows, [["Date", "A", "B", "C"]])

    def test_too_many_legs_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store_with_sample(tmp)
            with mock.patch.object(udf_common, "_historical_store", lambda: store):
                with self.assertRaisesRegex(ValueError, "最大3本"):
                    asw_udf.AswHistoricalRates([["AAA"], ["BBB"], ["CCC"], ["AAA"]], "PA")

    def test_date_range_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store_with_sample(tmp)
            with mock.patch.object(udf_common, "_historical_store", lambda: store):
                rows = asw_udf.AswHistoricalRates([["AAA"]], "PA", from_date=D0, to_date=D0 + timedelta(days=0))
        self.assertEqual(rows, [["Date", "A", "B", "C"], [D0, 0.10, "", ""]])


if __name__ == "__main__":
    unittest.main()
