"""
リアルタイムの受け渡し(live_store / live_udf)のリグレッションテスト。

実行方法:
    python -m unittest tests.test_live -v
"""

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from engine.common import live_udf  # noqa: E402
from engine.common.live_store import LiveStore  # noqa: E402

ROWS = [
    ["O/N", 0.477],
    ["1W", 0.48],
    ["RD", 0.49, datetime(2026, 10, 20), datetime(2026, 10, 30)],
    ["2W", None],
]


class LiveStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = LiveStore(Path(self._tmp.name) / "live.db")

    def tearDown(self):
        self._tmp.cleanup()

    def test_round_trip_keeps_values_and_dates(self):
        t0 = datetime(2026, 10, 18, 10, 0, 0)
        self.assertEqual(self.store.publish("Swap", ROWS, now=t0), t0)
        data = self.store.read(" swap ")
        self.assertEqual(data.rows, ROWS)
        self.assertEqual(data.updated_at, t0)

    def test_unchanged_values_are_not_rewritten(self):
        t0, t1, t2 = (datetime(2026, 10, 18, 10, 0, s) for s in (0, 1, 2))
        self.store.publish("swap", ROWS, now=t0)
        self.assertIsNone(self.store.publish("swap", ROWS, now=t1))
        self.assertEqual(self.store.read("swap").updated_at, t0)

        changed = [row[:] for row in ROWS]
        changed[0][1] = 0.478
        self.assertEqual(self.store.publish("swap", changed, now=t2), t2)
        self.assertEqual(self.store.read("swap").rows[0], ["O/N", 0.478])
        self.assertEqual(self.store.read("swap").updated_at, t2)

    def test_datasets_are_independent(self):
        self.store.publish("swap", ROWS)
        self.store.publish("vol", [["1Yx10Y", 70.1]])
        self.assertEqual(self.store.read("swap").rows, ROWS)
        self.assertEqual(self.store.read("vol").rows, [["1Yx10Y", 70.1]])

    def test_missing_db_or_dataset_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "LiveFeed"):
            self.store.read("swap")
        self.store.publish("swap", ROWS)
        with self.assertRaisesRegex(ValueError, "vol"):
            self.store.read("vol")
        with self.assertRaises(ValueError):
            self.store.publish(" ", ROWS)

    def test_read_live_saves_snapshot_per_sheet(self):
        t0, t1 = datetime(2026, 10, 18, 10, 0, 0), datetime(2026, 10, 18, 10, 0, 5)
        self.store.publish("swap", ROWS, now=t0)
        self.assertEqual(self.store.read_live("swap", "PricingSheet.xlsm", "Main").rows, ROWS)
        changed = [row[:] for row in ROWS]
        changed[0][1] = 0.478
        self.store.publish("swap", changed, now=t1)
        self.store.read_live("swap", "PricingSheet.xlsm", "pricing")

        # Mainは最後に読んだ(変わる前の)値のまま、pricingは変わった後の値。最新(live)には影響しない
        main = self.store.read_snapshot(" Swap ", "PricingSheet.xlsm", "Main")
        self.assertEqual((main.rows, main.updated_at), (ROWS, t0))
        self.assertEqual(self.store.read_snapshot("swap", "PricingSheet.xlsm", "pricing").rows[0], ["O/N", 0.478])
        self.assertEqual(self.store.read("swap").updated_at, t1)

        # 読み直すと上書きされる
        self.store.read_live("swap", "PricingSheet.xlsm", "Main")
        self.assertEqual(self.store.read_snapshot("swap", "PricingSheet.xlsm", "Main").updated_at, t1)

    def test_missing_snapshot_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "Live"):
            self.store.read_snapshot("swap", "PricingSheet.xlsm", "Main")
        self.store.publish("swap", ROWS)
        self.store.read_live("swap", "PricingSheet.xlsm", "Main")
        with self.assertRaisesRegex(ValueError, "BOJ"):
            self.store.read_snapshot("swap", "PricingSheet.xlsm", "BOJ")
        with self.assertRaisesRegex(ValueError, "vol"):
            self.store.read_snapshot("vol", "PricingSheet.xlsm", "Main")

    def test_nan_is_stored_as_blank(self):
        self.store.publish("swap", [["1W", float("nan")]])
        self.assertEqual(self.store.read("swap").rows, [["1W", None]])


class _Caller:
    """xlwingsがUDFの引数 caller に渡す呼び出し元のセル(Range)の代わり。"""

    def __init__(self, book, sheet):
        self.sheet = mock.Mock()
        self.sheet.name = sheet
        self.sheet.book.name = book


MAIN = _Caller("PricingSheet.xlsm", "Main")


class LiveUdfTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        store = LiveStore(Path(self._tmp.name) / "live.db")
        self._patch = mock.patch.object(live_udf, "_live_store", lambda: store)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_publish_then_read(self):
        message = live_udf.PublishLive("swap", ROWS)
        self.assertIn("swap:", message)
        self.assertIn("4行", message)
        # 2回目は値が同じなので書かないが、最後に書いた時刻を表示する
        self.assertEqual(live_udf.PublishLive("swap", ROWS), message)

        quotes = live_udf.LiveQuotes("swap", caller=MAIN)
        # 空欄は""、行の長さは一番長い行にそろえる
        self.assertEqual(quotes[0], ["O/N", 0.477, "", ""])
        self.assertEqual(quotes[2], ["RD", 0.49, datetime(2026, 10, 20), datetime(2026, 10, 30)])
        self.assertEqual(quotes[3], ["2W", "", "", ""])
        self.assertIsInstance(live_udf.LiveInfo("swap", quotes, caller=MAIN), datetime)

    def test_snapshot_keeps_the_rates_last_read_on_the_sheet(self):
        live_udf.PublishLive("swap", ROWS)
        before = live_udf.LiveQuotes("swap", caller=MAIN)
        changed = [row[:] for row in ROWS]
        changed[0][1] = 0.478
        live_udf.PublishLive("swap", changed)

        # Fixedに切り替えると、切り替える前に読んだ値(最新ではない)
        self.assertEqual(live_udf.LiveSnapshot("swap", caller=MAIN), before)
        self.assertEqual(live_udf.LiveQuotes("swap", caller=_Caller("PricingSheet.xlsm", "BOJ"))[0][1], 0.478)
        self.assertEqual(live_udf.LiveSnapshot("swap", caller=MAIN), before)
        # 別のブックの同じ名前のシートとは混ざらない
        with self.assertRaises(ValueError):
            live_udf.LiveSnapshot("swap", caller=_Caller("SwapAnalysis.xlsm", "Main"))

    def test_info_needs_the_quotes_and_a_caller(self):
        live_udf.PublishLive("swap", ROWS)
        quotes = live_udf.LiveQuotes("swap", caller=MAIN)
        with self.assertRaisesRegex(ValueError, "LiveQuotes"):
            live_udf.LiveInfo("swap", [["ValueError: ..."]], caller=MAIN)
        with self.assertRaises(ValueError):
            live_udf.LiveQuotes("swap")
        with self.assertRaises(ValueError):
            live_udf.LiveInfo("swap", quotes)

    def test_publish_accepts_blank_rows(self):
        live_udf.PublishLive("swap", [["O/N", 0.477], None])
        self.assertEqual(live_udf.LiveQuotes("swap", caller=MAIN), [["O/N", 0.477], ["", ""]])


if __name__ == "__main__":
    unittest.main()
