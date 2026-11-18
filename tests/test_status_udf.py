"""DataStatus(engine/common/status_udf.py)のテスト。"""

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.common import status_udf, udf_common  # noqa: E402
from engine.common.historical_store import HistoricalStore  # noqa: E402
from engine.common.jgb_source import JgbClose, JgbRecord  # noqa: E402

TODAY = date(2026, 11, 18)


class DataStatusTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = HistoricalStore(Path(self._tmp.name) / "historical.db")
        self.store.save_jgb_bonds([JgbRecord("JS999", "JP09990045=", "XX", 2.0, date(2031, 3, 20))])
        self.store.save_jgb_closes([JgbClose(date(2026, 11, 16), "JS999", 2.10), JgbClose(date(2026, 11, 17), "JS999", 2.12)])
        p1 = mock.patch.object(udf_common, "_historical_store", lambda: self.store)
        p2 = mock.patch.object(udf_common, "_today", lambda: TODAY)
        for p in (p1, p2):
            p.start()
            self.addCleanup(p.stop)

    def test_volatile(self):
        """今日・DBと比べ直すためvolatile(下流を持たせないので毎回呼ばれても軽い)。"""
        self.assertTrue(status_udf.DataStatus.__xlfunc__["volatile"])

    def test_trade_date(self):
        self.assertEqual(status_udf.DataStatus(None), status_udf.BLANK)
        self.assertEqual(status_udf.DataStatus(""), status_udf.BLANK)
        self.assertEqual(status_udf.DataStatus(TODAY), status_udf.OK)
        self.assertEqual(status_udf.DataStatus(date(2026, 11, 17)), status_udf.NOT_TODAY)
        self.assertEqual(status_udf.DataStatus(date(2026, 11, 19)), status_udf.FUTURE)

    def test_db_newer(self):
        """評価日を入れた後に朝バッチが新しい日付を書いたら知らせる(計算に使った日付より新しいデータがある)。"""
        self.assertEqual(status_udf.DataStatus(TODAY, date(2026, 11, 17), "jgb"), status_udf.OK)
        self.assertEqual(status_udf.DataStatus(TODAY, date(2026, 11, 16), "jgb"), status_udf.NEWER)
        self.assertEqual(status_udf.DataStatus(TODAY, "", "jgb"), status_udf.OK)  # 比べる日付が無ければ比べない
        # 過去の評価日はその日以前のデータと比べる
        self.assertEqual(status_udf.DataStatus(date(2026, 11, 16), date(2026, 11, 16), "jgb"), status_udf.NOT_TODAY)

    def test_unknown_data(self):
        with self.assertRaisesRegex(ValueError, "data"):
            status_udf.DataStatus(TODAY, TODAY, "vol")


if __name__ == "__main__":
    unittest.main()
