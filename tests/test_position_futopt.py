"""PositionSheetの国債先物オプション(engine/position/futures_options.py、PosFutOptRisk)のテスト。"""

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.common import jgb_futures, udf_common  # noqa: E402
from engine.common.historical_store import HistoricalStore  # noqa: E402
from engine.common.jgb_source import JgbClose, JgbRecord  # noqa: E402
from engine.common.option.bond import pricer  # noqa: E402
from engine.position import futures_options as fo, udf  # noqa: E402
from engine.position.bonds import BondPosition, bond_risk  # noqa: E402

BASE = date(2026, 11, 10)
EXPIRY = date(2026, 11, 30)
FUT = 128.10
REPO = 0.5
COL = {h: i for i, h in enumerate(fo.HEADER)}


class FutOptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = HistoricalStore(Path(self.tmp.name) / "historical.db")
        self.store.save_jgb_bonds([
            JgbRecord("JB375", "JP03750067=", "XX1", 1.1, date(2034, 6, 20)),
            JgbRecord("JB380", "JP03800067=", "XX2", 1.7, date(2035, 9, 20)),
            JgbRecord("JB360", "JP03600067=", "XX3", 0.1, date(2030, 9, 20)),  # 残存が短く受渡適格でない
        ])
        self.store.save_jgb_closes([
            JgbClose(BASE, "JB375", 1.55), JgbClose(BASE, "JB380", 1.70), JgbClose(BASE, "JB360", 1.0),
        ])
        self.store.save_fut_settles([(BASE, "2026-12", FUT)])

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_name(self):
        opt = fo.parse_name("債先2612C127.50")
        self.assertEqual(opt, fo.FutOption(jgb_futures.Contract(2026, 12), pricer.CALL, 127.5))
        self.assertEqual(fo.parse_name("債先2703P126").option_type, pricer.PUT)
        self.assertTrue(fo.is_futures_option("債先2612C128.25"))
        self.assertFalse(fo.is_futures_option(None))
        with self.assertRaises(ValueError):
            fo.parse_name("債先2611C127.50")  # 限月は3・6・9・12月

    def test_implied_vol_reprices_the_mark(self):
        vol = pricer.implied_price_vol(pricer.CALL, FUT, 127.5, 0.723519, 14 / 252)
        self.assertAlmostEqual(pricer.black_premium(pricer.CALL, FUT, 127.5, vol, 14 / 252), 0.723519, places=9)
        with self.assertRaises(ValueError):
            pricer.implied_price_vol(pricer.CALL, FUT, 127.5, 0.5, 14 / 252)  # 本源的価値0.6以下

    def test_rows(self):
        names = ["", "債先2612C127.50", "債先2612C128.25", "債先2612C128.25"]
        expiries = [None, EXPIRY, EXPIRY, BASE]
        marks = [None, 0.723519, 0.353621, 0.353621]
        with self.store:
            rows = fo.risk_rows(self.store, names, expiries, marks, BASE, REPO)
        self.assertEqual(rows[0], [""] * len(fo.HEADER))  # 空行
        r = rows[1]
        self.assertEqual(r[COL["Message"]], "")
        self.assertEqual(r[COL["Contract"]], "2026-12")
        self.assertEqual(r[COL["Futures"]], FUT)
        self.assertAlmostEqual(r[COL["Premium"]], 0.723519, places=9)
        self.assertTrue(0 < r[COL["Delta"]] < 1)
        self.assertGreater(r[COL["Gamma"]], 0)
        self.assertLess(r[COL["Theta"]], 0)
        self.assertIn(r[COL["CTD"]], ("JB375", "JB380"))
        self.assertEqual(rows[2][COL["CTD"]], r[COL["CTD"]])
        self.assertIn("Expired", rows[3][COL["Message"]])
        # CTDの dP・d2P は現物のデルタと同じ計算
        with self.store:
            ref = bond_risk(self.store, BondPosition(r[COL["CTD"]], 100.0), BASE)
        self.assertAlmostEqual(r[COL["dP"]], ref.dp)
        self.assertAlmostEqual(r[COL["d2P"]], ref.d2p)
        # シートの換算: コールのロングは金利が上がると損(デルタがマイナス)
        e = 20 * fo.LOT_FACE * r[COL["Delta"]] / r[COL["CF"]]
        self.assertLess(e * r[COL["dP"]] / 100, 0)

    def test_missing_settle_and_repo(self):
        with self.store:
            rows = fo.risk_rows(self.store, ["債先2703C127.50", "債先2612C127.50"], [date(2027, 2, 26), EXPIRY],
                                [0.8, 0.723519], BASE, None)
        self.assertIn("清算値", rows[0][-1])
        self.assertIn("レポ", rows[1][-1])
        self.assertNotEqual(rows[1][COL["Delta"]], "")  # グリークスまでは出す

    def test_udf_header_and_row_count(self):
        with mock.patch.object(udf_common, "_historical_store", lambda: self.store), \
                mock.patch.object(udf, "_db_key", lambda: None):
            out = udf.PosFutOptRisk(["", "債先2612C127.50", "JX72 CALL"], ["", EXPIRY, EXPIRY], ["", 0.723519, 0.5], BASE, REPO)
        self.assertEqual(out[0], fo.HEADER)
        self.assertEqual(len(out), 4)
        self.assertEqual(out[1], [""] * len(fo.HEADER))  # 空行
        self.assertEqual(out[2][-1], "")
        self.assertIn("読めません", out[3][-1])  # 債先の形でない名前は黙って飛ばさない

if __name__ == "__main__":
    unittest.main()
