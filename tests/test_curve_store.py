"""
日次カーブDB(暦日ごとのDF保存)の保存・復元のリグレッションテスト。

「どの引き方のカーブも暦日ごとのDFに落とせば、復元後も同じパーレートになる」ことが
カーブの引き方に依存しない保存形式の前提。ここが崩れると市場レート逆算の精度要件
(機械精度〜0.1bp未満)を満たせなくなるため、固定ロール/IMM/EOM・フォワード起点まで確認する。

実行方法:
    python -m unittest tests.test_curve_store -v
"""

import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import QuantLib as ql  # noqa: E402

from swap_pricing.curve import bootstrap_curve  # noqa: E402
from swap_pricing.curve_store import GRID_DAYS, CurveStore, discount_grid  # noqa: E402
from swap_pricing.swap_pricer import price_swap  # noqa: E402
from tests.test_curve_repricing import SPOT_RATES  # noqa: E402

AS_OF = date(2025, 3, 31)  # 月末(EOMロールの確認のため)
TOLERANCE_PCT = 1e-9


def _par(curve, index, start, tenor, roll="STD"):
    return price_swap(
        curve, AS_OF, start, "PA", "act/365fixed", "PA", "act/365fixed", roll,
        notional=1.0, pay_rec="PAY", tenor=tenor, index=index,
    ).target_fixrate


class CurveStoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.store = CurveStore(Path(cls.tmp.name) / "historical.db")
        cls.boot = bootstrap_curve(AS_OF, SPOT_RATES)
        cls.store.save_curve(
            AS_OF, "convex_monotone", "hash", cls.boot.used_tenors, [],
            discount_grid(cls.boot.curve, AS_OF),
        )

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_restored_curve_reproduces_par_rates(self):
        stored = self.store.load(AS_OF)
        starts = [self.boot.spot_date, date(2026, 6, 17), date(2027, 3, 31)]
        for tenor in ["1M", "6M", "1Y", "2Y", "5Y", "10Y", "20Y", "30Y", "40Y"]:
            for start in starts:
                for roll in ["STD", "IMM", "EOM"]:
                    with self.subTest(tenor=tenor, start=start, roll=roll):
                        expected = _par(self.boot.curve, self.boot.index, start, tenor, roll)
                        actual = _par(stored.curve, stored.index, start, tenor, roll)
                        self.assertAlmostEqual(actual, expected, delta=TOLERANCE_PCT)

    def test_until_limits_range(self):
        stored = self.store.load(AS_OF, until=AS_OF + timedelta(days=6 * 366))
        full = self.store.load(AS_OF)
        spot = self.boot.spot_date
        self.assertEqual(
            _par(stored.curve, stored.index, spot, "5Y"), _par(full.curve, full.index, spot, "5Y"),
        )
        with self.assertRaises(RuntimeError):  # 範囲外は外挿せずQuantLibがエラーにする
            _par(stored.curve, stored.index, spot, "10Y")

    def test_independent_of_evaluation_date(self):
        stored = self.store.load(AS_OF)
        target = ql.Date.from_date(AS_OF) + 1000
        ql.Settings.instance().evaluationDate = ql.Date.from_date(AS_OF)
        before = stored.curve.discount(target)
        ql.Settings.instance().evaluationDate = ql.Date.from_date(AS_OF) + 30
        self.assertEqual(stored.curve.discount(target), before)

    def test_beyond_grid_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.load(AS_OF, until=AS_OF + timedelta(days=GRID_DAYS + 1))

    def test_missing_date_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.load(date(2025, 4, 1))


class QuotesTest(unittest.TestCase):
    def test_quotes_are_replaced_per_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CurveStore(Path(tmp) / "historical.db")
            store.save_quotes({AS_OF: {"1M": 1.0, "5Y": 2.0}, date(2025, 4, 1): {"1M": 1.1}})
            store.save_quotes({AS_OF: {"5Y": 2.5}})
            self.assertEqual(store.quotes(), {AS_OF: {"5Y": 2.5}, date(2025, 4, 1): {"1M": 1.1}})


if __name__ == "__main__":
    unittest.main()
