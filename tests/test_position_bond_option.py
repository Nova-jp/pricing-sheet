"""
PositionSheetの現物オプションのグリークス(engine/position/bond_options.py、jgb_option.pricer.black_greeks)のテスト。

- Black式のグリークス: プレミアムはF10(black_premium)と一致し、デルタ・ガンマ・ベガは差分と一致、
  セータは期間を1営業日縮めた差
- 市場の表: 先渡・MD・ボラの補間・Vol ratio・Tが部品を直接呼んだ値と一致。空欄の行は空欄、満期済み・
  DBに無い銘柄はmessageだけ
- ピラーへの分解: ガンマが空欄なら現物のキーレート・デルタ(bonds.py)と一致し、ガンマの項は G × dP² / 100
- ベガの格子: 合計は約定ごとのベガの合計と一致し、格子に無い原資産(6Y)は前後(5Y・7Y)に分かれる

実行方法:
    python -m unittest tests.test_position_bond_option -v
"""

import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from engine.common import bond  # noqa: E402
from engine.common.historical_store import HistoricalStore  # noqa: E402
from engine.common.jgb_option import hv, pricer, vol  # noqa: E402
from engine.common.jgb_source import JgbClose, JgbRecord  # noqa: E402
from engine.position import bond_options, bonds  # noqa: E402
from tests.test_position import FRIDAY, _curves  # noqa: E402

TAILS = ["1Y", "2Y", "3Y", "4Y", "5Y", "7Y", "10Y", "15Y", "20Y", "30Y"]
EXPIRIES = ["1M", "3M", "6M", "9M", "1Y", "2Y", "3Y", "4Y", "5Y", "7Y", "10Y", "15Y", "20Y", "25Y", "30Y"]
VOL_ROWS = [[f"{e}x{t}", 40.0 + 2 * i + j] for i, e in enumerate(["1M", "3M", "6M", "1Y"])
            for j, t in enumerate(["1Y", "5Y", "6Y", "7Y", "10Y"])]


class _Rates:
    """hv.CurveRates の代わり(日付×満期日のスワップレート)。"""

    def __init__(self, dates):
        self.curve_dates = dates

    def fill(self, needs):
        pass

    def rate(self, d, maturity):
        return 1.0 + 0.01 * (d.toordinal() % 3)


class BlackGreeksTest(unittest.TestCase):
    def test_matches_premium_and_differences(self):
        for kind in ("Call", "Put"):
            f, k, s, t = 91.3, 91.0, 3.2, 21 / 252
            g = pricer.black_greeks(kind, f, k, s, t)
            self.assertAlmostEqual(g.premium, pricer.black_premium(kind, f, k, s, t), places=12)
            h = 1e-4
            up, down = pricer.black_premium(kind, f + h, k, s, t), pricer.black_premium(kind, f - h, k, s, t)
            self.assertAlmostEqual(g.delta, (up - down) / (2 * h), places=6)
            self.assertAlmostEqual(g.gamma, (up - 2 * g.premium + down) / (h * h), places=3)
            vega = (pricer.black_premium(kind, f, k, s + h, t) - pricer.black_premium(kind, f, k, s - h, t)) / (2 * h)
            self.assertAlmostEqual(g.vega, vega, places=6)
            self.assertAlmostEqual(g.theta, pricer.black_premium(kind, f, k, s, 20 / 252) - g.premium, places=12)
        self.assertLess(pricer.black_greeks("Put", 91.3, 91.0, 3.2, 1 / 252).theta, 0)  # 最終日は本源的価値まで


class MarketRowsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = HistoricalStore(Path(self.tmp.name) / "historical.db")
        self.store.save_jgb_bonds([JgbRecord("JB999", "JP09990067=", "XX", 1.7, date(2035, 9, 20))])
        self.days = [FRIDAY - timedelta(days=n) for n in (7, 6, 5, 4, 3, 0)]  # 金曜まで(平日)
        self.store.save_jgb_closes([JgbClose(d, "JB999", 1.70 + 0.005 * (i % 2)) for i, d in enumerate(self.days)])

    def tearDown(self):
        self.tmp.cleanup()

    def test_rows(self):
        te, se = date(2026, 10, 26), date(2026, 10, 28)
        with self.store:
            rows = bond_options.market_rows(
                ["JB999", "", "jb999", "JX000"], [te, None, FRIDAY, te], [se, None, se, se], FRIDAY, 0.5, 3,
                self.store, _Rates(self.days), VOL_ROWS,
            )
        self.assertEqual(len(rows[0]), bond_options.MARKET_COLUMNS)
        self.assertEqual(rows[0][-1], "")
        jgb = bond.Jgb(1.7, date(2035, 9, 20))
        spot_settle = bond.settlement_date(FRIDAY, 1)
        spot = bond.price_from_simple_yield(jgb, 1.70 + 0.005 * 1, spot_settle)  # 金曜の引け(6日目)
        fwd = bond.forward_price(jgb, spot, spot_settle, se, 0.5)
        risk = pricer.forward_duration(jgb, fwd.dirty, se)
        self.assertEqual(rows[0][:2], [1.7, date(2035, 9, 20)])
        self.assertAlmostEqual(rows[0][2], 1.705, places=12)
        self.assertEqual(rows[0][3], FRIDAY)
        self.assertEqual(rows[0][4:8], [spot, fwd.clean, risk.modified_duration, risk.dollar_duration])
        self.assertAlmostEqual(rows[0][8], vol.bond_option_vol(VOL_ROWS, FRIDAY, te, jgb.maturity), places=12)
        closes = {d: 1.70 + 0.005 * (i % 2) for i, d in enumerate(self.days)}
        self.assertAlmostEqual(rows[0][9], hv.vol_ratio(jgb, closes, _Rates(self.days), FRIDAY, 3).ratio, places=12)
        self.assertEqual(rows[0][10], pricer.option_term(FRIDAY, te))
        self.assertEqual(rows[1], [""] * bond_options.MARKET_COLUMNS)
        self.assertIn("Expired", rows[2][-1])
        self.assertEqual(rows[2][:-1], [""] * (bond_options.MARKET_COLUMNS - 1))
        self.assertIn("JX000", rows[3][-1])


class PillarRiskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = HistoricalStore(Path(self.tmp.name) / "historical.db")
        self.store.save_jgb_bonds([JgbRecord("JB999", "JP09990067=", "XX", 1.7, date(2035, 9, 20))])
        self.store.save_jgb_closes([JgbClose(FRIDAY, "JB999", 1.700)])
        self.curves = _curves()

    def tearDown(self):
        self.tmp.cleanup()

    def test_delta_matches_bond_and_gamma_term(self):
        with self.store:
            plain = bonds.bond_risks(self.curves, self.store, [bonds.BondPosition("JB999", 300.0)])[0]
            d0, g0, e0 = bond_options.pillar_risk(self.curves, self.store, ["JB999", "JB999"], [100.0, 200.0], ["", ""])
            d1, g1, _ = bond_options.pillar_risk(self.curves, self.store, ["JB999"], [300.0], [50.0])
            unit, _, _ = bond_options.pillar_risk(self.curves, self.store, ["JB999"], [100.0], [""])
        self.assertEqual(e0, [])
        for label, v in plain.deltas.items():
            self.assertAlmostEqual(d0[label], v, places=7)  # 銘柄ごとに足してから分ける(差分の丸めの誤差 1e-9程度)
            self.assertAlmostEqual(d1[label], v, places=7)
            dp = unit[label]  # 額面100百万円のデルタ = dP(円/bp)
            self.assertAlmostEqual(g1[label] - g0[label], 50.0 * dp * dp / 100.0, places=7)  # 2階の差分の誤差 1e-9程度
        with self.store:
            _, _, errors = bond_options.pillar_risk(self.curves, self.store, ["JX000"], [1.0], [""])
        self.assertIn("JX000", errors[0])


class VegaGridTest(unittest.TestCase):
    def test_total_and_split(self):
        te = date(2026, 10, 26)  # 取引日の1か月後(1Mの格子の点)
        maturity = te + timedelta(days=round(6 * 365))  # 原資産 ≈ 6Y(格子の6Yの点)
        out = bond_options.vega_grid(VOL_ROWS, FRIDAY, [2.0, "", 1.0], [te, te, te], [maturity] * 3, EXPIRIES, TAILS)
        self.assertEqual((len(out), len(out[0])), (len(EXPIRIES), len(TAILS)))
        self.assertAlmostEqual(sum(map(sum, out)), 3.0, places=6)  # 補間の重みの合計は1
        self.assertAlmostEqual(sum(out[0]), 3.0, places=6)  # 満期は1Mの点ちょうど
        five, seven = out[0][TAILS.index("5Y")], out[0][TAILS.index("7Y")]
        self.assertAlmostEqual(five + seven, 3.0, places=6)  # 格子の6Yの点(≈6年)→ 5Y・7Yにほぼ半分ずつ
        self.assertGreater(min(five, seven), 1.4)
        self.assertEqual(sum(map(sum, bond_options.vega_grid(VOL_ROWS, FRIDAY, [], [], [], EXPIRIES, TAILS))), 0.0)


if __name__ == "__main__":
    unittest.main()
