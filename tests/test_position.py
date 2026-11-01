"""
ポジション・リスク管理(engine/position/)のリグレッションテスト。

- 現物: ピラーごとのデルタの合計がパラレルのデルタ(全ピラーを同時に動かす)と一致し、ロングはマイナス
- 日中の取引: スポットの10Y Payerのデルタは10Yのピラーだけに乗り、空行・エラーの行も行数を保つ
- フォワードの換算行列: 各ピラーのパーレートは自分より先の区間で動かず、全区間の和はほぼ1。O/Nは独立
- PCA: 使わないピラー(フラグ0)の主成分ベクトルは0の行で返る
- 日中の取引(OIS・現物)のデルタ・ガンマ: デルタは既存のスワップ・現物の計算と一致し、レシーブ・現物のロングは
  ガンマがプラス。グリッドはStatusで足し分け、エラーの行は行ごとのMessageに出して足さない

実行方法:
    python -m unittest tests.test_position -v
"""

import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import numpy as np
import QuantLib as ql

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from engine.common import bond, market, udf_common  # noqa: E402
from engine.common.conventions import CALENDAR  # noqa: E402
from engine.common.historical_store import HistoricalStore  # noqa: E402
from engine.common.jgb_source import JgbClose, JgbRecord  # noqa: E402
from engine.position import bonds, forward, intraday, trades, udf  # noqa: E402
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

    def test_udf_shape(self):
        with mock.patch.object(udf_common, "_historical_store", lambda: self.store), \
                mock.patch.object(udf_common, "_db_fixings", lambda: {}), \
                mock.patch.object(udf_common, "_today", lambda: FRIDAY):
            out = udf.PosBondRisk([["JB999", 100, None], [None, None, None], ["ZZ1", 1, None]],
                                  PILLARS, _rt_range(), None)
        self.assertEqual(out[0], udf.BOND_HEADER + PILLARS)
        self.assertEqual(len(out), 4)
        n = len(udf.BOND_HEADER)
        self.assertAlmostEqual(out[1][udf.BOND_HEADER.index("DV01")], sum(out[1][n:]))
        self.assertLess(abs(out[1][n]), abs(out[1][udf.BOND_HEADER.index("DV01")]) * 1e-3)  # O/Nはほぼ0
        self.assertEqual(out[2], [""] * len(out[0]))
        self.assertIn("ZZ1", out[3][udf.BOND_HEADER.index("Message")])


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

    def test_forward_start_and_udf_rows(self):
        with mock.patch.object(udf_common, "_db_fixings", lambda: {}), \
                mock.patch.object(udf_common, "_today", lambda: FRIDAY):
            out = udf.PosTradeRisk([
                ["What-if", "5Y", "5Y", 10_000, "rec", 2.5],
                [None] * 6,
                ["Done", None, None, 1, "PAY", None],
                ["Done", None, "2Y", 1, "BUY", None],
            ], PILLARS, _rt_range(), None)
        self.assertEqual(out[0], udf.TRADE_HEADER + PILLARS)
        deltas = dict(zip(PILLARS, out[1][len(udf.TRADE_HEADER):]))
        self.assertLess(deltas["10Y"], 0)  # 5y5yのレシーブ: 10Yがマイナス、5Yがプラス
        self.assertGreater(deltas["5Y"], 0)
        self.assertEqual(out[2], [""] * len(out[0]))
        msg = udf.TRADE_HEADER.index("Message")
        self.assertIn("Tenor/End", out[3][msg])
        self.assertIn("Pay/Rec", out[4][msg])


class ForwardJacobianTest(unittest.TestCase):
    def test_structure(self):
        jac = forward.forward_jacobian(_curves(), PILLARS)
        j = np.array(jac.matrix)
        self.assertEqual(jac.segments[:3], ["O/N", "Spot-1W", "1W-1M"])
        self.assertEqual(jac.segments[-1], "35Y-40Y")
        on = PILLARS.index("O/N")
        expected = np.zeros(len(PILLARS))
        expected[on] = 1.0
        np.testing.assert_array_equal(j[on], expected)  # O/Nは自分自身の区間だけ
        np.testing.assert_array_equal(j[:, on], expected)
        for row, p in enumerate(PILLARS):
            if row == on:
                continue
            self.assertTrue(np.all(np.abs(j[row, row + 1:]) < 1e-10), p)  # 満期より先の区間では動かない
            self.assertAlmostEqual(j[row].sum(), 1.0, delta=0.05, msg=p)
            self.assertTrue(np.all(j[row, 1:row + 1] > 0), p)

    def test_forward_delta_of_a_spot_swap_covers_its_life(self):
        curves = _curves()
        jac = forward.forward_jacobian(curves, PILLARS)
        risk = trades.trade_risks(curves, [trades.Trade(None, "10Y", 10_000.0, "PAY")])[0]
        d = np.array([risk.deltas.get(p.lower(), 0.0) for p in PILLARS])
        fwd = np.array(jac.matrix).T @ d
        ten = PILLARS.index("10Y")
        self.assertTrue(np.all(fwd[1:ten + 1] > 0))
        self.assertTrue(np.all(np.abs(fwd[ten + 1:]) < 1e-9))
        self.assertAlmostEqual(fwd.sum(), d.sum(), delta=abs(d.sum()) * 0.03)


class PcaUdfTest(unittest.TestCase):
    def test_unused_pillars_get_zero_rows(self):
        rng = np.random.default_rng(0)
        labels = ["O/N", "1Y", "5Y", "10Y"]
        days, d = [], date(2026, 1, 5)
        while len(days) < 60:
            if CALENDAR.isBusinessDay(ql.Date.from_date(d)):
                days.append(d)
            d += timedelta(days=1)
        level = np.cumsum(rng.normal(0, 0.02, size=(len(days), 3)), axis=0) + 1.0
        history = {day: {"1Y": v[0], "5Y": v[1], "10Y": v[2]} for day, v in zip(days, level)}
        with mock.patch.object(udf, "load_history", return_value=history), \
                mock.patch.object(udf, "_db_key", return_value=1):
            udf._pca_cached.cache_clear()
            loadings = udf.PosPcaLoadings(labels, [0, 1, 1, 1], 40, days[-1])
            eigen = udf.PosPcaEigen(labels, [0, 1, 1, 1], 40, days[-1])
            window = dict(udf.PosPcaWindow(labels, [0, 1, 1, 1], 40, days[-1]))
        self.assertEqual(loadings[0], ["Pillar", "PC1", "PC2", "PC3"])
        self.assertEqual(loadings[1], ["O/N", 0.0, 0.0, 0.0])
        v = np.array([r[1:] for r in loadings[2:]])
        np.testing.assert_allclose(v.T @ v, np.eye(3), atol=1e-12)
        self.assertGreater(eigen[1][1], eigen[2][1])
        self.assertEqual(window["Samples"], 40)
        self.assertEqual(window["End"], days[-1])


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
