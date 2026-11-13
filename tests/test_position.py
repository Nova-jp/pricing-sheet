"""
ポジション・リスク管理(engine/position/)のリグレッションテスト。

- 現物: 修正デュレーション × 単価が複利1bpの単価の変化と一致し、ロングはデルタがマイナス・ガンマがプラス。
  満期が入る区間の長いほうのピラーに全部載る。Raw_JGBのInstrument IDから銘柄を引き、償還日を照合する
- 日中の取引: スポットの10Y Payerのデルタは10Yのピラーだけに乗る
- 引け値: PosCloseRatesはピラーの並びで返し、O/NはTONA実績、DBに無いピラーは空欄
- 日中の取引(OIS・現物)のデルタ・ガンマ: デルタは既存のスワップ・現物の計算と一致し、レシーブ・現物のロングは
  ガンマがプラス。グリッドはStatusで足し分け、エラーの行は行ごとのMessageに出して足さない

実行方法:
    python -m unittest tests.test_position -v
"""

import math
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import QuantLib as ql

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from engine.common import bond, market, swaption, udf_common  # noqa: E402
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
            JgbRecord("JX23", "JP00230068=", "ZZ", 2.5, date(2036, 6, 20)),
        ])
        self.store.save_jgb_closes([JgbClose(FRIDAY, "JB999", 1.700), JgbClose(FRIDAY, "JX23", 2.815)])

    def tearDown(self):
        self.tmp.cleanup()

    def _risks(self, positions):
        with self.store:
            return bonds.bond_risks(self.store, positions, FRIDAY)

    def test_duration_delta_is_compound_bp_price_change(self):
        risk = self._risks([bonds.BondPosition("jb999", 100.0)])[0]
        self.assertEqual(risk.code, "JB999")
        self.assertEqual(risk.simple_yield, 1.7)
        self.assertLess(risk.delta, 0)  # ロングはマイナス
        self.assertGreater(risk.gamma, 0)
        # 複利を動かして単価を出し直した差(額面100円あたり)と一致する
        jgb = bond.Jgb(1.7, date(2035, 9, 20))
        settle = ql.Date.from_date(bond.settlement_date(FRIDAY, 1))
        qb = bond.ql_bond(jgb, "no_leap")

        def dirty(y):
            rate = ql.InterestRate(y / 100, bond.NO_LEAP, ql.Compounded, ql.Semiannual)
            return ql.BondFunctions.cleanPrice(qb, rate, settle) + ql.BondFunctions.accruedAmount(qb, settle)

        y = risk.compound_yield
        self.assertAlmostEqual(dirty(y), risk.dirty, places=9)
        self.assertAlmostEqual(dirty(y + 0.005) - dirty(y - 0.005), risk.dp, delta=abs(risk.dp) * 1e-6)
        self.assertAlmostEqual(dirty(y + 0.01) - 2 * dirty(y) + dirty(y - 0.01), risk.d2p, delta=risk.d2p * 1e-3)
        self.assertAlmostEqual(risk.delta, risk.dp)  # 額面100百万円 → dP × 100 / 100
        self.assertAlmostEqual(risk.dp, -risk.modified_duration * risk.dirty * 1e-4)

    def test_rows_keep_their_place(self):
        out = self._risks([
            bonds.BondPosition("JB999", -50.0), None, bonds.BondPosition("JN999", 10.0),
            bonds.BondPosition("JN999", 10.0, yield_override=0.9), bonds.BondPosition("ZZ1", 1.0),
        ])
        self.assertGreater(out[0].delta, 0)  # ショートはプラス
        self.assertIsNone(out[1])
        self.assertIsInstance(out[2], ValueError)  # 引け値なし
        self.assertIsNone(out[3].close_date)  # overrideなら引け値の日付なし
        self.assertIsInstance(out[4], ValueError)

    def test_bucket_is_longer_pillar_of_the_interval(self):
        spot = date(2026, 9, 29)
        pillars = bonds.pillar_dates(["O/N", "1W", "12M", "24M", "36M", "40Y", "RD"], spot)
        self.assertEqual([label for _, label in pillars], ["1W", "12M", "24M", "36M", "40Y"])  # O/N・読めない表記は除く
        self.assertEqual(bonds.bucket(date(2029, 5, 1), pillars), "36M")  # 残存2.6年 → 2Yと3Yの間 → 3Y
        self.assertEqual(bonds.bucket(date(2028, 9, 29), pillars), "24M")  # ピラーの日付ちょうど
        self.assertEqual(bonds.bucket(date(2026, 10, 1), pillars), "1W")
        self.assertEqual(bonds.bucket(date(2070, 3, 20), pillars), "40Y")  # 最も長いピラーより先

    def test_instrument_id(self):
        self.assertEqual(bonds.jgb_code("0-02-0023-00068"), "JX23")
        self.assertEqual(bonds.jgb_code(" 0-02-0380-00067 "), "JB380")
        for bad in ("JX23", "0-02-0023-00099", "", None):
            with self.assertRaises(ValueError):
                bonds.jgb_code(bad)

    def test_raw_jgb_udfs(self):
        ids = ["0-02-0023-00068", "0-02-0999-00067", None, "0-02-0023-00068", "0-02-0999-00067", "bad"]
        maturities = [49846, date(2035, 9, 20), None, date(2036, 6, 21), None, None]  # 49846 = 2036-06-20
        faces = [500, 100, None, 1, 0, 1]
        yields = ["", 1.8, None, None, None, None]
        with mock.patch.object(udf_common, "_historical_store", lambda: self.store):
            udf._bond_cache.clear()
            rows = udf.PosJgbRisk(ids, maturities, faces, yields, PILLARS, FRIDAY)
            grid = udf.PosJgbGrid(ids, maturities, faces, yields, PILLARS, FRIDAY)
        col = {name: i for i, name in enumerate(udf.JGB_HEADER)}
        self.assertEqual(rows[0], udf.JGB_HEADER)
        self.assertEqual(len(rows), len(ids) + 1)
        self.assertEqual(rows[1][col["Code"]], "JX23")
        self.assertEqual(rows[1][col["Yield"]], 2.815)  # 空欄ならDBの引け値
        self.assertEqual(rows[1][col["Pillar"]], "10Y")  # 残存9.7年 → 9Yと10Yの間
        self.assertEqual(rows[2][col["Yield"]], 1.8)
        self.assertEqual(rows[2][col["Pillar"]], "9Y")
        self.assertEqual(rows[3], [""] * len(udf.JGB_HEADER))
        self.assertIn("償還日", rows[4][col["Message"]])
        self.assertEqual(rows[5], [""] * len(udf.JGB_HEADER))  # 額面0
        self.assertIn("Instrument ID", rows[6][col["Message"]])
        self.assertEqual(grid[0], udf.JGB_GRID_HEADER)
        self.assertEqual(len(grid), len(PILLARS) + 1)
        by_pillar = dict(zip(PILLARS, grid[1:]))
        self.assertAlmostEqual(by_pillar["10Y"][0], rows[1][col["Delta"]])
        self.assertAlmostEqual(by_pillar["9Y"][1], rows[2][col["Gamma"]])
        self.assertAlmostEqual(sum(g[0] for g in grid[1:]), rows[1][col["Delta"]] + rows[2][col["Delta"]])


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


    def test_close_vols_grid(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = HistoricalStore(Path(tmp) / "historical.db")
            store.save_vol_quotes({FRIDAY: {"1Mx1Y": 30.0, "1Yx10Y": 65.5}})
            with mock.patch.object(udf_common, "_historical_store", lambda: store):
                out = udf.PosCloseVols(["1M", "1Y"], ["1Y", "10Y"], FRIDAY)
                self.assertEqual(out, [[30.0, ""], ["", 65.5]])  # 行=満期、列=原資産。無い点は空欄
                with self.assertRaises(ValueError):
                    udf.PosCloseVols(["1M"], ["1Y"], date(2026, 9, 28))

    def test_vol_calendar_factor(self):
        # 2026-09-25(金)の1M: 満期 10/25(日)→ 10/26(月)。暦日31日、営業日20日(10/12 スポーツの日は休み)
        factor = math.sqrt(20 / 252) * math.sqrt(365 / 31)
        self.assertAlmostEqual(swaption.calendar_factor(FRIDAY, "1M"), factor, places=12)
        out = udf.PosVolCalendarFactor(["1M", "1Y"], FRIDAY)
        self.assertAlmostEqual(out[0][0], factor, places=12)
        self.assertEqual(len(out), 2)
        with mock.patch.object(udf_common, "_today", lambda: FRIDAY):  # 空欄 = 今日(ライブ)
            self.assertAlmostEqual(udf.PosVolCalendarFactor(["1M"], "")[0][0], factor, places=12)

    def test_business_days_from_base_date(self):
        self.assertEqual(udf.PosBusinessDays(FRIDAY, date(2026, 9, 28)), 1)  # 金曜の引け → 月曜
        self.assertEqual(udf.PosBusinessDays(FRIDAY, date(2026, 9, 30)), 3)
        with mock.patch.object(udf_common, "_today", lambda: date(2026, 9, 28)):
            self.assertEqual(udf.PosBusinessDays(FRIDAY), 1)  # 空欄 = 今日

    def test_company_vega_grid_udf(self):
        out = udf.PosVegaGrid(["A", "A"], ["1M", "3D"], ["1Y", "1Y"], [2e6, 1e6], "A", ["1W", "2W", "1M"], ["1Y"])
        self.assertEqual(out, [[1e6], [""], [2e6]])

    def test_base_date_is_latest_db_date_before_today(self):
        class _Store:
            def __init__(self, dates):
                self._dates = dates

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

            def dates(self):
                return self._dates

        monday = date(2026, 9, 28)
        store = _Store([date(2026, 9, 24), FRIDAY, monday])
        with mock.patch.object(udf_common, "_historical_store", lambda: store), \
                mock.patch.object(udf_common, "_today", lambda: monday):
            self.assertEqual(udf.PosBaseDate(None), FRIDAY)  # 今日(月曜)のカーブがあっても昨日までの直近
            self.assertEqual(udf.PosBaseDate(""), FRIDAY)
            self.assertEqual(udf.PosBaseDate(date(2026, 9, 24)), date(2026, 9, 24))  # 上書き
        with mock.patch.object(udf_common, "_historical_store", lambda: _Store([monday])), \
                mock.patch.object(udf_common, "_today", lambda: monday):
            with self.assertRaises(ValueError):
                udf.PosBaseDate(None)

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
            ref_bond = bonds.bond_risk(self.store, bond_pos, FRIDAY)
        ref_swap = trades.trade_risks(self.curves, [swap])[0]
        self.assertEqual(set(out[0].deltas), set(ref_swap.deltas))
        for k in ref_swap.deltas:
            self.assertAlmostEqual(out[0].deltas[k], ref_swap.deltas[k], delta=1e-6, msg=k)  # 解き直しの誤差(1e-8程度)
        self.assertEqual(out[1].deltas, {"9y": ref_bond.delta})  # 現物は満期の区間(8Y〜9Y)の9Yだけ
        self.assertEqual(out[1].gammas, {"9y": ref_bond.gamma})
        self.assertAlmostEqual(out[0].rate, ref_swap.fix_rate)
        self.assertEqual(out[1].rate, 1.8)
        self.assertIsNone(out[1].pv)
        self.assertEqual(out[1].pillar, "9Y")
        with self.store:  # 約定の複利 − 引けの複利(単利1.8%と1.7%の差を複利にしたもの)
            close = bonds.bond_risk(self.store, bond_pos._replace(yield_override=None), FRIDAY)
        self.assertAlmostEqual(out[1].trade_move, (ref_bond.compound_yield - close.compound_yield) * 100, places=10)
        self.assertGreater(out[1].trade_move, 9.0)
        self.assertIsNone(out[0].pillar)
        self.assertGreater(out[0].gammas["10y"], 0)  # レシーブはガンマがプラス
        self.assertGreater(out[1].gammas["9y"], 0)  # 現物のロングもプラス
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
        self.assertEqual(rows[3][udf.INTRADAY_HEADER.index("Pillar")], "9Y")
        self.assertEqual(rows[3][udf.INTRADAY_HEADER.index("Trade move")], 0.0)  # Rate空欄 = 引け値で約定
        self.assertEqual(rows[1][udf.INTRADAY_HEADER.index("Trade move")], "")  # OIS
        self.assertEqual(rows[1][msg], "")
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
