"""
現物オプションのヒストリカル・ボラの比(engine/common/jgb_option/hv.py・table.vol_ratio_table)のテスト。

架空の銘柄・平らなカーブ(日ごとに水準を動かす)で、窓の日付の選び方・HVの定義(2乗平均の平方根)・
行ごとのエラーを確かめる。

実行方法:
    python -m unittest tests.test_bond_option_hv -v
"""

import math
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from typing import NamedTuple

import QuantLib as ql

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from engine.bond_option import table  # noqa: E402
from engine.common.jgb_option import hv  # noqa: E402
from engine.common.bond import Jgb, compound_yield  # noqa: E402
from engine.common.conventions import CALENDAR  # noqa: E402

BOND = Jgb(1.0, date(2031, 3, 20))  # 架空の5年債


def business_days(start: date, n: int):
    out, d = [], start
    while len(out) < n:
        if CALENDAR.isBusinessDay(ql.Date.from_date(d)):
            out.append(d)
        d += timedelta(days=1)
    return out


class _Stored(NamedTuple):
    curve: ql.YieldTermStructureHandle


class FlatStore:
    """日付ごとに平らなカーブ(連続複利 levels[d] %)を返す。load の回数を数える。"""

    def __init__(self, levels):
        self.levels = levels
        self.loads = 0

    def dates(self):
        return sorted(self.levels)

    def load(self, d, until=None):
        self.loads += 1
        curve = ql.FlatForward(ql.Date.from_date(d), self.levels[d] / 100, ql.Actual365Fixed())
        return _Stored(ql.YieldTermStructureHandle(curve))


DAYS = business_days(date(2026, 9, 1), 30)
# 1bpずつ上下に交互に動く(スワップのHVは約1bp/日)
LEVELS = {d: 1.5 + (0.01 if i % 2 else 0.0) for i, d in enumerate(DAYS)}
# 現物は単利で2bpずつ交互に動く
CLOSES = {d: 1.6 + (0.02 if i % 2 else 0.0) for i, d in enumerate(DAYS)}


class WindowTest(unittest.TestCase):
    def test_takes_last_window_plus_one_dates_present_in_both(self):
        closes = [d for d in DAYS if d != DAYS[-3]]  # 引け値が1日欠けている
        dates = hv.window_dates(DAYS, closes, DAYS[-1], 5, BOND.maturity)
        self.assertEqual(dates, [DAYS[-7], DAYS[-6], DAYS[-5], DAYS[-4], DAYS[-2], DAYS[-1]])

    def test_ignores_dates_after_trade_date(self):
        dates = hv.window_dates(DAYS, DAYS, DAYS[10], 3, BOND.maturity)
        self.assertEqual(dates[-1], DAYS[10])
        self.assertEqual(len(dates), 4)

    def test_not_enough_dates_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "4日分"):
            hv.window_dates(DAYS, DAYS[:5], DAYS[-1], 20, BOND.maturity)

    def test_window_must_be_integer_of_two_or_more(self):
        self.assertEqual(hv.parse_window(20.0), 20)
        for bad in (1, 2.5, "x", None):
            with self.assertRaises(ValueError):
                hv.parse_window(bad)


class VolRatioTest(unittest.TestCase):
    def setUp(self):
        self.store = FlatStore(LEVELS)
        self.rates = hv.CurveRates(self.store)

    def _ratio(self, window=10):
        self.rates.fill(hv.needed(self.rates.curve_dates, {"X": CLOSES}, [("X", BOND)], DAYS[-1], window))
        return hv.vol_ratio(BOND, CLOSES, self.rates, DAYS[-1], window)

    def test_hv_is_root_mean_square_of_daily_changes(self):
        r = self._ratio()
        dates = DAYS[-11:]
        ys = [hv.bond_compound_yield(BOND, CLOSES[d], d) for d in dates]
        ss = [self.rates.rate(d, BOND.maturity) for d in dates]
        expect_bond = math.sqrt(sum(((b - a) * 100) ** 2 for a, b in zip(ys, ys[1:])) / 10)
        expect_swap = math.sqrt(sum(((b - a) * 100) ** 2 for a, b in zip(ss, ss[1:])) / 10)
        self.assertAlmostEqual(r.bond_hv, expect_bond, places=12)
        self.assertAlmostEqual(r.swap_hv, expect_swap, places=12)
        self.assertAlmostEqual(r.ratio, expect_bond / expect_swap, places=12)
        self.assertEqual((r.changes, r.first, r.last), (10, DAYS[-11], DAYS[-1]))

    def test_levels_are_about_the_input_moves(self):
        r = self._ratio()
        self.assertAlmostEqual(r.swap_hv, 1.0, delta=0.05)  # 連続複利1bp ≒ パーレート1bp
        self.assertAlmostEqual(r.bond_hv, 2.0, delta=0.15)  # 単利2bp ≒ 複利2bp
        self.assertAlmostEqual(r.ratio, 2.0, delta=0.2)

    def test_swap_rates_are_cached_per_date(self):
        self._ratio()
        loads = self.store.loads
        self._ratio()
        self.assertEqual(self.store.loads, loads)

    def test_compound_yield_is_not_truncated(self):
        settle = date(2026, 9, 2)
        y = compound_yield(BOND, 100.1234, settle, truncate=False)
        self.assertNotEqual(y, compound_yield(BOND, 100.1234, settle))
        self.assertAlmostEqual(y, compound_yield(BOND, 100.1234, settle), delta=0.001)


class TableTest(unittest.TestCase):
    def test_rows_errors_and_blanks(self):
        rates = hv.CurveRates(FlatStore(LEVELS))
        closes = {"JS1": CLOSES, "JS2": {d: CLOSES[d] for d in DAYS[:3]}}
        out = table.vol_ratio_table(
            ["JS1", "", "js2", "JS3"], [1.0, "", 1.0, "x"], [BOND.maturity, "", BOND.maturity, BOND.maturity],
            closes, rates, DAYS[-1], 10,
        )
        self.assertEqual(len(out), 4)
        self.assertEqual(out[0][6], "")
        self.assertGreater(out[0][2], 0)
        self.assertEqual(out[1], [""] * 7)
        self.assertIn("2日分", out[2][6])  # 小文字の銘柄コードも引ける。日数が足りない
        self.assertIn("利率", out[3][6])

    def test_invalid_window_is_reported_on_every_row(self):
        out = table.vol_ratio_table(["JS1", ""], [1.0, ""], [BOND.maturity, ""], {}, hv.CurveRates(FlatStore(LEVELS)),
                                    DAYS[-1], 0)
        self.assertIn("窓", out[0][6])
        self.assertEqual(out[1], [""] * 7)


if __name__ == "__main__":
    unittest.main()
