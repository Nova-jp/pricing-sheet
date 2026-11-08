"""
ポジション・リスク管理(engine/position/)のリグレッションテスト。

- 現物: ピラーごとのデルタの合計がパラレルのデルタ(全ピラーを同時に動かす)と一致し、ロングはマイナス
- 日中の取引: スポットの10Y Payerのデルタは10Yのピラーだけに乗る
- 引け値: PosCloseRatesはピラーの並びで返し、O/NはTONA実績、DBに無いピラーは空欄
- 日中の取引(OIS・現物)のデルタ・ガンマ: デルタは既存のスワップ・現物の計算と一致し、レシーブ・現物のロングは
  ガンマがプラス。グリッドはStatusで足し分け、エラーの行は行ごとのMessageに出して足さない

実行方法:
    python -m unittest tests.test_position -v
"""

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import QuantLib as ql

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from engine.common import bond, market, udf_common  # noqa: E402
from engine.common.historical_store import HistoricalStore  # noqa: E402
from engine.common.jgb_source import JgbClose, JgbRecord  # noqa: E402
from engine.position import bonds, intraday, trades, udf  # noqa: E402
from tests.test_curve_repricing import SPOT_RATES  # noqa: E402
from tests.test_market_risk import _rt_range  # noqa: E402

FRIDAY = date(2026, 9, 25)
PILLARS = ["O/N"] + list(SPOT_RATES)


def _curves():
    market._cache.clear()
    with mock.patch.object(udf_common, "_db_fixings", lambda: {}), \
            mock.patch.object(udf_common, "_today", lambda: FRIDAY):
        return udf_common._market(_rt_range(), None)


class BondRiskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = HistoricalStore(Path(self.tmp.name) / "historical.db")
        self.store.save_jgb_bonds([
            JgbRecord("JB999", "JP09990067=", "XX", 1.7, date(2035, 9, 20)),
            JgbRecord("JN999", "JP09990042=", "YY", 0.5, date(2028, 3, 1)),  # 引け値なし
        ])
        self.store.save_jgb_closes([JgbClose(FRIDAY, "JB999", 1.700)])
        self.curves = _curves()

    def tearDown(self):
        self.tmp.cleanup()

    def _risks(self, positions):
        with self.store:
            return bonds.bond_risks(self.curves, self.store, positions)

    def test_bucket_sum_matches_parallel_and_long_is_negative(self):
        risk = self._risks([bonds.BondPosition("jb999", 100.0)])[0]
        self.assertEqual(risk.code, "JB999")
        self.assertEqual(risk.simple_yield, 1.7)
        total = sum(risk.deltas.values())
        self.assertLess(total, 0)
        # パラレル: 全ピラーを同時に動かしたカーブで、Zスプレッドを固定して評価し直す
        jgb = bond.Jgb(1.7, date(2035, 9, 20))
        settle = ql.Date.from_date(bond.settlement_date(FRIDAY, 1))
        qb = bond.ql_bond(jgb)
        z = risk.z_spread_bp / 1e4
        base = bonds._clean_price(qb, self.curves.base.curve.currentLink(), z, settle)
        bumped = bonds._clean_price(qb, self.curves.parallel().curve.currentLink(), z, settle)
        parallel = (bumped - base) / 100.0 * 100.0 / market.BUMP_BP
        self.assertAlmostEqual(total, parallel, delta=abs(parallel) * 1e-3)
        # 償還(2035年9月)の前後のピラーに集中する
        top = sorted(risk.deltas, key=lambda k: abs(risk.deltas[k]), reverse=True)[:2]
        self.assertEqual(set(top), {"9y", "10y"})

    def test_rows_keep_their_place(self):
        out = self._risks([
            bonds.BondPosition("JB999", -50.0), None, bonds.BondPosition("JN999", 10.0),
            bonds.BondPosition("JN999", 10.0, yield_override=0.9), bonds.BondPosition("ZZ1", 1.0),
        ])
        self.assertGreater(sum(out[0].deltas.values()), 0)  # ショートはプラス
        self.assertIsNone(out[1])
        self.assertIsInstance(out[2], ValueError)  # 引け値なし
        self.assertIsNone(out[3].close_date)  # overrideなら引け値の日付なし
        self.assertIsInstance(out[4], ValueError)

class TradeRiskTest(unittest.TestCase):
    def setUp(self):
        self.curves = _curves()

    def test_spot_payer_sits_on_its_pillar(self):
        risk = trades.trade_risks(self.curves, [trades.Trade(None, "10Y", 10_000.0, "PAY")])[0]
        self.assertAlmostEqual(risk.pv, 0.0, places=6)  # パーレートで組む
        self.assertAlmostEqual(risk.fix_rate, risk.par_rate)
        self.assertGreater(risk.deltas["10y"], 0)
        others = sum(abs(v) for k, v in risk.deltas.items() if k != "10y")
        self.assertLess(others, risk.deltas["10y"] * 1e-3)

class CloseRatesTest(unittest.TestCase):
    def test_pillar_order_fixing_and_blanks(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = HistoricalStore(Path(tmp) / "historical.db")
            store.save_quotes({FRIDAY: {"12M": 1.5, "10y": 2.1}})
            store.save_fixings({FRIDAY: 0.98})
            with mock.patch.object(udf_common, "_historical_store", lambda: store):
                out = udf.PosCloseRates(["O/N", "12M", "1W", "10Y"], FRIDAY)
                self.assertEqual(out, [[0.98], [1.5], [""], [2.1]])  # 表記の大小文字は問わない。1WはDBに無い
                with self.assertRaises(ValueError):
                    udf.PosCloseRates(["10Y"], date(2026, 9, 28))  # その日の引けが無い


class IntradayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = HistoricalStore(Path(self.tmp.name) / "historical.db")
        self.store.save_jgb_bonds([JgbRecord("JB999", "JP09990067=", "XX", 1.7, date(2035, 9, 20))])
        self.store.save_jgb_closes([JgbClose(FRIDAY, "JB999", 1.700)])
        self.curves = _curves()

    def tearDown(self):
        self.tmp.cleanup()

    def test_deltas_match_existing_and_gamma_sign(self):
        swap = trades.Trade(None, "10Y", 10_000.0, "REC")
        bond_pos = bonds.BondPosition("JB999", 100.0, yield_override=1.8)
        with self.store:
            out = intraday.intraday_risks(self.curves, self.store, [
                intraday.IntradayTrade(intraday.DONE, intraday.OIS, swap=swap),
                intraday.IntradayTrade(intraday.DONE, intraday.BOND, bond=bond_pos),
            ])
            ref_bond = bonds.bond_risks(self.curves, self.store, [bond_pos])[0]
        ref_swap = trades.trade_risks(self.curves, [swap])[0]
        for got, ref in ((out[0].deltas, ref_swap.deltas), (out[1].deltas, ref_bond.deltas)):
            self.assertEqual(set(got), set(ref))
            for k in ref:
                self.assertAlmostEqual(got[k], ref[k], delta=1e-6, msg=k)  # ブートストラップの解き直しの誤差(1e-8程度)
        self.assertAlmostEqual(out[0].rate, ref_swap.fix_rate)
        self.assertEqual(out[1].rate, 1.8)
        self.assertIsNone(out[1].pv)
        self.assertGreater(out[0].gammas["10y"], 0)  # レシーブはガンマがプラス
        self.assertGreater(sum(out[1].gammas.values()), 0)  # 現物のロングもプラス
        # 10Yのスワップのガンマ: 1bpの中心差分(スワップは補間の影響を受けない)とほぼ一致
        params = trades.trade_params(self.curves, swap)
        pv = [0.0, 0.0]
        for n, h in enumerate((1.0, -1.0)):
            with self.curves.bumped("10y", h):
                pv[n] = trades.price(self.curves.base, params).pv
        base = trades.price(self.curves.base, params).pv
        self.assertAlmostEqual(out[0].gammas["10y"], pv[0] - 2 * base + pv[1], delta=abs(out[0].gammas["10y"]) * 0.01)

    def _udf(self, func, table, *args):
        with mock.patch.object(udf_common, "_historical_store", lambda: self.store),                 mock.patch.object(udf_common, "_db_fixings", lambda: {}),                 mock.patch.object(udf_common, "_today", lambda: FRIDAY):
            udf._intraday_cache.clear()
            return func(table, PILLARS, _rt_range(), None, *args)

    def test_udf_rows_and_grid(self):
        table = [
            ["Done", "OIS", None, "10Y", None, 10_000, "Pay", None],
            ["What-if", "ois", None, "5Y", None, 10_000, "Rec", 2.0],
            ["Done", "Bond", None, None, "JB999", 100, "Sell", None],
            [None] * 8,
            ["Maybe", "OIS", None, "2Y", None, 1, "Pay", None],
            ["Done", "Bond", None, None, None, 1, "Buy", None],
            ["Done", "Bond", None, None, "JB999", 1, "Pay", None],
        ]
        rows = self._udf(udf.PosIntradayRisk, table)
        self.assertEqual(rows[0], udf.INTRADAY_HEADER)
        self.assertEqual(len(rows), len(table) + 1)
        msg = udf.INTRADAY_HEADER.index("Message")
        dv01 = udf.INTRADAY_HEADER.index("DV01")
        self.assertGreater(rows[1][dv01], 0)
        self.assertGreater(rows[3][dv01], 0)  # 現物の売り(ショート)はプラス
        self.assertEqual(rows[3][udf.INTRADAY_HEADER.index("PV")], "")
        self.assertEqual(rows[4], [""] * len(udf.INTRADAY_HEADER))
        for n, word in ((5, "Status"), (6, "Code"), (7, "Buy")):
            self.assertIn(word, rows[n][msg])
        done = self._udf(udf.PosIntradayGrid, table)
        whatif = self._udf(udf.PosIntradayGrid, table, "What-if")
        both = self._udf(udf.PosIntradayGrid, table, "All")
        self.assertEqual(done[0], udf.GRID_HEADER)
        self.assertEqual(len(done), len(PILLARS) + 1)
        col = {name: i for i, name in enumerate(udf.GRID_HEADER)}
        swap_done = sum(r[col["Delta Swap"]] for r in done[1:])
        self.assertAlmostEqual(swap_done, rows[1][dv01], places=6)
        self.assertAlmostEqual(sum(r[col["Delta Bond"]] for r in done[1:]), rows[3][dv01], places=6)
        self.assertEqual(sum(abs(r[col["Delta Bond"]]) for r in whatif[1:]), 0.0)
        for a, b, c in zip(done[1:], whatif[1:], both[1:]):
            for i in range(len(udf.GRID_HEADER)):
                self.assertAlmostEqual(a[i] + b[i], c[i], places=6)
        with self.assertRaises(ValueError):
            self._udf(udf.PosIntradayGrid, table, "Pending")


if __name__ == "__main__":
    unittest.main()
