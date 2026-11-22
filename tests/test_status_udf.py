"""ValuationDate・DbStamp・DataStatus(engine/common/status_udf.py)のテスト。"""

import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.common import status_udf, udf_common  # noqa: E402
from engine.common.historical_store import HistoricalStore  # noqa: E402

TODAY = date(2026, 11, 18)


class DataStatusTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = HistoricalStore(Path(self._tmp.name) / "historical.db")
        p1 = mock.patch.object(udf_common, "_historical_store", lambda: self.store)
        p2 = mock.patch.object(udf_common, "_today", lambda: TODAY)
        for p in (p1, p2):
            p.start()
            self.addCleanup(p.stop)

    def test_volatile(self):
        """今日・DBと比べ直すためvolatile(下流を持たせないので毎回呼ばれても軽い)。"""
        self.assertTrue(status_udf.DataStatus.__xlfunc__["volatile"])

    def test_not_volatile(self):
        """ValuationDate・DbStamp は評価日のセルの下流で、入れ直したときだけ計算される。"""
        for f in (status_udf.ValuationDate, status_udf.DbStamp):
            self.assertFalse(f.__xlfunc__.get("volatile", False))

    def test_valuation_date(self):
        self.assertEqual(status_udf.ValuationDate(date(2026, 11, 17)), date(2026, 11, 17))
        self.assertEqual(status_udf.ValuationDate(TODAY), TODAY)
        # 空欄・未来日はエラー(今日で補わない。DataStatus は B3 を直接見て理由を出す)
        for blank in (None, ""):
            with self.assertRaisesRegex(ValueError, "空欄"):
                status_udf.ValuationDate(blank)
        with self.assertRaisesRegex(ValueError, "未来日"):
            status_udf.ValuationDate(date(2026, 11, 19))

    def test_blank_trade_date_keeps_db_stamp_and_status(self):
        """B3 が空欄で B4 がエラーでも、B5 DbStamp(B3) は時刻を返し、B6 DataStatus(B3,B5) は空欄の理由を出す。"""
        stamp = self._run(datetime(2026, 11, 18, 7, 0, 0))
        self.assertEqual(status_udf.DbStamp(None), stamp)
        self.assertEqual(status_udf.DataStatus(None, status_udf.DbStamp(None)), status_udf.BLANK)

    def _run(self, at, ok=True, message=""):
        return self.store.record_batch_run(at, ok, message).finished_at

    def test_db_stamp(self):
        self.assertEqual(status_udf.DbStamp(TODAY), "")  # 記録が無ければ空欄
        self._run(datetime(2026, 11, 17, 7, 30, 1, 999999))
        stamp = self._run(datetime(2026, 11, 18, 7, 31, 2))
        self.assertEqual(stamp, "2026-11-18 07:31:02")
        self.assertEqual(status_udf.DbStamp(TODAY), stamp)  # 最後の実行(秒まで)

    def test_trade_date(self):
        stamp = self._run(datetime(2026, 11, 18, 7, 0, 0))
        self.assertEqual(status_udf.DataStatus(None, stamp), status_udf.BLANK)
        self.assertEqual(status_udf.DataStatus("", stamp), status_udf.BLANK)
        self.assertEqual(status_udf.DataStatus(TODAY, stamp), status_udf.OK)
        self.assertEqual(status_udf.DataStatus(date(2026, 11, 17), stamp), status_udf.NOT_TODAY)
        self.assertEqual(status_udf.DataStatus(date(2026, 11, 19), stamp), status_udf.FUTURE)

    def test_batch_run(self):
        """今日の朝バッチの実行が無い・最後の実行が失敗なら知らせる(データの日付ではなく実行の記録で判定)。"""
        self.assertEqual(status_udf.DataStatus(TODAY, ""), status_udf.NO_RUN_TODAY)  # 記録が無い
        yesterday = self._run(datetime(2026, 11, 17, 7, 0, 0))
        self.assertEqual(status_udf.DataStatus(TODAY, yesterday), status_udf.NO_RUN_TODAY)
        failed = self._run(datetime(2026, 11, 18, 7, 0, 0), ok=False, message="LsegError: timeout")
        self.assertEqual(status_udf.DataStatus(TODAY, failed), status_udf.BATCH_FAILED)
        ok = self._run(datetime(2026, 11, 18, 7, 20, 0))
        self.assertEqual(status_udf.DataStatus(TODAY, ok), status_udf.OK)

    def test_batch_run_after_trade_date(self):
        """評価日を入れた後に朝バッチを流したら、DbStamp(入れたときの実行時刻)が最後の実行と違うので知らせる。"""
        first = self._run(datetime(2026, 11, 18, 7, 0, 0))
        seen = status_udf.DbStamp(TODAY)
        self.assertEqual(seen, first)
        self._run(datetime(2026, 11, 18, 9, 0, 0))
        self.assertEqual(status_udf.DataStatus(TODAY, seen), status_udf.NEWER)
        self.assertEqual(status_udf.DataStatus(TODAY, status_udf.DbStamp(TODAY)), status_udf.OK)  # 入れ直した
        self.assertEqual(status_udf.DataStatus(TODAY, ""), status_udf.OK)  # 比べる時刻が無ければ比べない

    def test_no_db(self):
        with mock.patch.object(udf_common, "_historical_store", side_effect=ValueError("no db")):
            self.assertEqual(status_udf.DataStatus(TODAY, ""), status_udf.NO_DB)
            self.assertEqual(status_udf.DbStamp(TODAY), "")


if __name__ == "__main__":
    unittest.main()
