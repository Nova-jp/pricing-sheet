"""
LCHのカーブ(pricing表のindex列で清算機関ごとにカーブを選ぶ)のテスト。

- index=LCHの行は、LCHのレートで引いたカーブで、JSCCの行と同じ計算をすること
  (LCHの範囲をJSCCの範囲として渡したときの結果と一致する)
- 空欄はJSCC、知らない値・LCHのカーブが組めない(過去の評価日・範囲なし)ときはその行だけのエラー
- カーブのキャッシュは清算機関ごとに最新の1組(JSCCとLCHを交互に使っても作り直さない)
- RiskGridはLCHの行をLCHのカーブで動かし、同じTickerの行に足す
- live_feed.tomlの"swap_lch"のTickerは"swap"(MainシートA列)と同じ表記

実行方法:
    python -m unittest tests.test_lch_curve -v
"""

import datetime
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from engine.common import live_feed, market, udf_common  # noqa: E402
from engine.swap_pricing import udf  # noqa: E402
from tests.test_market_risk import BOOTSTRAP_TOL_YEN, _HEADER as _RISK_HEADER, _pricing_row, _rt_range  # noqa: E402
from tests.test_pricing_table import _HEADER, _row  # noqa: E402

INDEX = _HEADER.index("index")
RISK_INDEX = _RISK_HEADER.index("index")


def _lch_range():
    """LCHのレート: JSCCに長いほど大きいベーシスを足したもの(カーブが別物になるように)。"""
    rows = _rt_range()
    for n, row in enumerate(rows):
        if row[1] not in ("O/N", "M1"):
            rows[n] = [row[0], row[1], row[2] + 0.0001 * n]
    return rows


def _with_index(row, index, at=INDEX):
    row = list(row)
    row[at] = index
    return row


class PricingTableLchTest(unittest.TestCase):
    def setUp(self):
        market._cache.clear()
        self.jscc = _rt_range()
        self.lch = _lch_range()
        self.rows = [_row(start="2bd", tenor="5y"), _row(start="1y", tenor="10y", pay_rec="REC", fix_rate=1.5, delta=3.0)]

    def test_lch_rows_use_lch_curve(self):
        result = udf.PricingTable(
            [_HEADER] + [_with_index(r, "JSCC") for r in self.rows] + [_with_index(r, "LCH") for r in self.rows],
            self.jscc, None, self.lch,
        )
        only_jscc = udf.PricingTable([_HEADER] + self.rows, self.jscc)
        only_lch = udf.PricingTable([_HEADER] + self.rows, self.lch)  # LCHのレートをJSCCの範囲として渡す
        self.assertEqual(result[1:3], only_jscc[1:])
        self.assertEqual(result[3:5], only_lch[1:])
        self.assertNotAlmostEqual(result[1][0], result[3][0], delta=1e-6)  # 別のカーブになっている

    def test_blank_index_is_jscc_and_unknown_is_row_error(self):
        result = udf.PricingTable(
            [_HEADER, _with_index(self.rows[0], ""), _with_index(self.rows[0], "tibor")], self.jscc, None, self.lch,
        )
        self.assertEqual(result[1], udf.PricingTable([_HEADER, self.rows[0]], self.jscc)[1])
        self.assertEqual(result[2][:5], [""] * 5)
        self.assertIn("JSCC/LCH", result[2][5])

    def test_lch_without_range_is_row_error(self):
        result = udf.PricingTable([_HEADER, self.rows[0], _with_index(self.rows[0], "LCH")], self.jscc)
        self.assertEqual(result[1][5], "")
        self.assertIn("LCH", result[2][5])

    def test_lch_on_past_valuation_date_is_row_error(self):
        today = udf_common._today()
        past = today - datetime.timedelta(days=1)
        jscc_past = mock.patch.object(udf_common, "_market", return_value=udf_common._market(self.jscc, None))
        with jscc_past:
            result = udf.PricingTable(
                [_HEADER, self.rows[0], _with_index(self.rows[0], "LCH")], self.jscc, past, self.lch,
            )
        self.assertEqual(result[1][5], "")
        self.assertIn("今日のときだけ", result[2][5])

    def test_curves_are_cached_per_clearing_house(self):
        table = [_HEADER, self.rows[0], _with_index(self.rows[0], "LCH")]
        udf.PricingTable(table, self.jscc, None, self.lch)
        self.assertEqual(set(market._cache), {"JSCC", "LCH"})
        with mock.patch.object(market, "bootstrap_curve", wraps=market.bootstrap_curve) as spy:
            udf.PricingTable(table, self.jscc, None, self.lch)
        self.assertEqual(spy.call_count, 0)


class RiskGridLchTest(unittest.TestCase):
    def setUp(self):
        market._cache.clear()
        self.jscc = _rt_range()
        self.lch = _lch_range()
        today = datetime.date.today()
        self.jscc_rows = [_pricing_row(1, today, "5y")]
        self.lch_rows = [_with_index(_pricing_row(1, today, "10y", pay_rec="REC"), "LCH", RISK_INDEX)]

    def test_lch_rows_are_bumped_on_lch_curve_and_added_by_ticker(self):
        grid = udf.RiskGrid([_RISK_HEADER] + self.jscc_rows + self.lch_rows, self.jscc, None, self.lch)
        jscc_only = udf.RiskGrid([_RISK_HEADER] + self.jscc_rows, self.jscc)
        lch_only = udf.RiskGrid(
            [_RISK_HEADER] + [_with_index(r, "JSCC", RISK_INDEX) for r in self.lch_rows], self.lch,
        )
        self.assertEqual([r[0] for r in grid], [r[0] for r in jscc_only])
        for row, a, b in zip(grid[1:], jscc_only[1:], lch_only[1:]):
            if a[1] == "":
                self.assertEqual(row[1], "")
            else:
                # 既存のRiskGridのテストと同じ許容誤差(ブートストラップの収束誤差をバンプ幅で割ったもの)
                self.assertAlmostEqual(row[1], a[1] + b[1], delta=BOOTSTRAP_TOL_YEN / market.BUMP_BP / 1e6)

    def test_lch_row_error_reports_row_number(self):
        with self.assertRaisesRegex(ValueError, "3行目"):
            udf.RiskGrid([_RISK_HEADER] + self.jscc_rows + self.lch_rows, self.jscc)


class LiveFeedConfigTest(unittest.TestCase):
    def test_lch_tickers_match_swap(self):
        config = live_feed.load_config(REPO_ROOT / "live_feed.toml")
        sets = {d.name: d for d in config.datasets}
        self.assertEqual(sets["swap_lch"].kind, "ois")
        swap = [t for t, _ in sets["swap"].rows]
        lch = [t for t, _ in sets["swap_lch"].rows]
        # 会合スワップ(RD・M1〜)以外は全部ある
        self.assertEqual(lch, [t for t in swap if t != "RD" and not t.startswith("M")])


if __name__ == "__main__":
    unittest.main()
