"""
FutBasisシート(engine.fut)のテスト。

実行方法:
    python -m unittest tests.test_fut -v
    (LSEGのデータには依存しない。架空の清算値・引け値・TONA実績を一時DBに入れ、つなぎ足の乗り換え・
     同じ限月の変化・CTDが jgb_futures の計算(basis_table・cheapest)と一致すること、レポの段の自動と
     手入力、重ね合わせの営業日数、1日分の表の既定値(清算値・TONA)と上書きを確かめる。
     bond.coupon_dates の二分探索が、スケジュールを走査する元のやり方と一致することも確かめる)
"""

import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from engine.common import bond, udf_common  # noqa: E402
from engine.common import jgb_futures as jf  # noqa: E402
from engine.common.historical_store import HistoricalStore  # noqa: E402
from engine.common.jgb_source import JgbClose, JgbRecord  # noqa: E402
from engine.fut import history, udf  # noqa: E402

SEP, DEC = jf.Contract(2026, 9), jf.Contract(2026, 12)
DATES = [date(2026, 9, 10), date(2026, 9, 11), date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 16)]
# 9月限の取引最終日は 2026-09-14。9/15 から12月限
SETTLES = [
    (date(2026, 9, 10), "2026-09", 125.50), (date(2026, 9, 11), "2026-09", 125.70),
    (date(2026, 9, 14), "2026-09", 125.82),
    (date(2026, 9, 11), "2026-12", 125.10), (date(2026, 9, 14), "2026-12", 125.30),
    (date(2026, 9, 15), "2026-12", 125.00), (date(2026, 9, 16), "2026-12", 125.40),
]
BONDS = [
    JgbRecord("JB373", "JP103730GD12=", "JP1103731", 0.6, date(2033, 12, 20)),
    JgbRecord("JB380", "JP103800GF12=", "JP1103801", 1.7, date(2035, 9, 20)),
    JgbRecord("JL190", "JP201900GF12=", "JP1201901", 1.9, date(2044, 9, 20)),  # 20年債(受渡適格でない)
]
YIELDS = {"JB373": 2.60, "JB380": 2.80, "JL190": 3.30}
FIXINGS = {date(2026, 9, 14): 0.977, date(2026, 9, 15): 0.978, date(2026, 9, 16): 0.976}


class FutUdfTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = HistoricalStore(Path(self._tmp.name) / "historical.db")
        self.store.save_fut_settles(SETTLES)
        self.store.save_jgb_bonds(BONDS)
        self.store.save_jgb_closes([
            JgbClose(d, code, y + 0.01 * i) for i, d in enumerate(DATES) for code, y in YIELDS.items()
        ])
        self.store.save_fixings(FIXINGS)
        patcher = mock.patch.object(udf_common, "_historical_store", return_value=self.store)
        patcher.start()
        self.addCleanup(patcher.stop)
        for f in (udf._data_cached, udf._front_cached, udf._overlay_cached):
            f.cache_clear()

    def tearDown(self):
        self._tmp.cleanup()

    def _expected_ctd(self, contract, d, price, repo):
        quotes = [
            jf.BondQuote(b.code, b.coupon, b.maturity, YIELDS[b.code] + 0.01 * DATES.index(d)) for b in BONDS
        ]
        return jf.cheapest(jf.basis_table(contract, price, d, quotes, repo))

    def test_front_history(self):
        rows = udf.FutHistory(None)
        self.assertEqual(rows[0], udf.FRONT_HEADER)
        by_date = {r[0]: dict(zip(udf.FRONT_HEADER, r)) for r in rows[1:]}
        self.assertEqual(list(by_date), DATES)
        self.assertEqual([by_date[d]["Contract"] for d in DATES], ["2026-09"] * 3 + ["2026-12"] * 2)
        # ロールの日(9/15)の変化は12月限どうし(9/14 125.30 → 125.00)
        self.assertEqual(by_date[date(2026, 9, 15)]["Roll"], 1)
        self.assertAlmostEqual(by_date[date(2026, 9, 15)]["dSettle"], -0.30)
        self.assertEqual(by_date[date(2026, 9, 10)]["dSettle"], "")
        # レポの段(TONA実績から自動、9/14から)より前はCTD以降が空欄
        self.assertEqual(by_date[date(2026, 9, 11)]["Repo"], "")
        self.assertEqual(by_date[date(2026, 9, 11)]["CTD"], "")
        for d, contract in ((date(2026, 9, 14), SEP), (date(2026, 9, 16), DEC)):
            row = by_date[d]
            expected = self._expected_ctd(contract, d, row["Settle"], 0.977)
            self.assertEqual(row["Repo"], 0.977)
            self.assertEqual(row["CTD"], expected.code)
            self.assertEqual(row["Net"], expected.net_basis)
            self.assertEqual(row["IRR"], expected.implied_repo)
            self.assertEqual(row["CF"], expected.conversion_factor)

    def test_manual_steps(self):
        steps = [["Start", "Rate"], [None, None], [date(2026, 9, 1), 0.5], [date(2026, 9, 15), 0.75]]
        rows = udf.FutHistory(steps[1:])
        by_date = {r[0]: dict(zip(udf.FRONT_HEADER, r)) for r in rows[1:]}
        self.assertEqual(by_date[date(2026, 9, 11)]["Repo"], 0.5)
        self.assertEqual(by_date[date(2026, 9, 16)]["Repo"], 0.75)
        expected = self._expected_ctd(SEP, date(2026, 9, 11), 125.70, 0.5)
        self.assertEqual(by_date[date(2026, 9, 11)]["Net"], expected.net_basis)
        with self.assertRaises(ValueError):
            udf.FutHistory([[date(2026, 9, 1), None]])

    def test_repo_steps(self):
        self.assertEqual(udf.FutRepoSteps(), [["Start", "Rate"], [date(2026, 9, 14), 0.977]])

    def test_overlay(self):
        steps = [[date(2026, 9, 1), 0.5]]
        rows = udf.FutOverlay(["2026-09", "JGBZ6"], "net", steps)
        self.assertEqual(rows[0], ["BD", "2026-09", "2026-12"])
        bds = [r[0] for r in rows[1:]]
        self.assertEqual(bds, sorted(bds, reverse=True))
        by_bd = {r[0]: r[1:] for r in rows[1:]}
        # 9月限: 9/14 が取引最終日(0)、9/11 が1、9/10 が2
        self.assertEqual(by_bd[0][0], self._expected_ctd(SEP, date(2026, 9, 14), 125.82, 0.5).net_basis)
        self.assertEqual(by_bd[2][0], self._expected_ctd(SEP, date(2026, 9, 10), 125.50, 0.5).net_basis)
        # 12月限: 期近になる前(9/11)も含む
        bd = history.business_days_to(date(2026, 9, 11), jf.last_trade_date(DEC))
        self.assertEqual(by_bd[bd][1], self._expected_ctd(DEC, date(2026, 9, 11), 125.10, 0.5).net_basis)
        self.assertEqual(by_bd[0][1], "")
        with self.assertRaises(ValueError):
            udf.FutOverlay(["2026-09"], "PRICE")

    def test_day_basis_defaults(self):
        rows = udf.FutBasis("2026-12", date(2026, 9, 16))
        self.assertEqual(rows[0], udf.DAY_HEADER)
        self.assertEqual(rows[-1][:4], ["Futures", 125.40, "Repo", 0.976])
        body = [dict(zip(udf.DAY_HEADER, r)) for r in rows[1:-1]]
        self.assertEqual([r["Code"] for r in body], ["JB373", "JB380"])  # 20年債は含めない
        expected = self._expected_ctd(DEC, date(2026, 9, 16), 125.40, 0.976)
        self.assertEqual([r["CTD"] for r in body], [int(r["Code"] == expected.code) for r in body])
        self.assertEqual(body[0]["Close date"], date(2026, 9, 16))

    def test_day_basis_overrides(self):
        # 引け値の無い日(9/17)は直近の引け値(9/16)、先物価格・レポ・単利を上書き
        rows = udf.FutBasis("JGBZ6", date(2026, 9, 17), 126.0, 1.0, [["JB380", 2.5], [None, None]])
        body = {r[0]: dict(zip(udf.DAY_HEADER, r)) for r in rows[1:-1]}
        self.assertEqual(rows[-1][:4], ["Futures", 126.0, "Repo", 1.0])
        self.assertEqual(body["JB373"]["Close date"], date(2026, 9, 16))
        self.assertEqual(body["JB380"]["Yield"], 2.5)
        self.assertEqual(body["JB380"]["Close date"], date(2026, 9, 17))
        expected = jf.basis_row(DEC, 126.0, date(2026, 9, 17), jf.BondQuote("JB380", 1.7, date(2035, 9, 20), 2.5), 1.0)
        self.assertEqual(body["JB380"]["Net"], expected.net_basis)
        with self.assertRaises(ValueError):  # 清算値の無い日は先物価格が要る
            udf.FutBasis("2026-12", date(2026, 9, 17))


class CouponDatesTest(unittest.TestCase):
    def test_same_as_schedule_scan(self):
        for jgb in (bond.Jgb(0.6, date(2033, 12, 20)), bond.Jgb(1.7, date(2035, 9, 20)), bond.Jgb(1.9, date(2044, 3, 20))):
            for after in (date(2026, 3, 20), date(2026, 3, 19), date(2026, 9, 21)):
                for days in (0, 1, 90, 184, 400):
                    until = after + timedelta(days=days)
                    scan = [d.to_date() for d in bond.schedule(jgb) if bond._qd(after) < d <= bond._qd(until)]
                    self.assertEqual(bond.coupon_dates(jgb, after, until), scan)


if __name__ == "__main__":
    unittest.main()
