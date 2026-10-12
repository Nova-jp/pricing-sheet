"""
日次カーブDB(暦日ごとのDF保存)の保存・復元のリグレッションテスト。

「どの引き方のカーブも暦日ごとのDFに落とせば、復元後も同じパーレートになる」ことが
カーブの引き方に依存しない保存形式の前提。ここが崩れると市場レート逆算の精度要件
(機械精度〜0.1bp未満)を満たせなくなるため、固定ロール/IMM/EOM・フォワード起点まで確認する。

実行方法:
    python -m unittest tests.test_historical_store -v
"""

import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import QuantLib as ql  # noqa: E402

from swap_pricing.curve import bootstrap_curve  # noqa: E402
from swap_pricing.historical_store import (  # noqa: E402
    GRID_DAYS, AswQuoteRow, AswRate, HistoricalStore, _grid_dates, discount_grid,
)
from swap_pricing.market_data import Market  # noqa: E402
from swap_pricing.swap_pricer import price_swap  # noqa: E402
from swap_pricing.terms import SwapConvention, SwapTerms  # noqa: E402
from tests.test_curve_repricing import SPOT_RATES  # noqa: E402

AS_OF = date(2025, 3, 31)  # 月末(EOMロールの確認のため)
TOLERANCE_PCT = 1e-9


def _par(curve, index, start, tenor, roll="STD"):
    terms = SwapTerms(start, SwapConvention("PA", "act/365fixed", "PA", roll), tenor=tenor)
    return price_swap(Market(AS_OF, curve, index), terms, notional=1.0, pay_rec="PAY").target_fixrate


class HistoricalStoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.store = HistoricalStore(Path(cls.tmp.name) / "historical.db")
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


class GridDatesTest(unittest.TestCase):
    """日付の並びの使い回し(切り出し・範囲外での作り直し)で、連続した暦日がずれないこと。"""

    def assert_grid(self, start, n):
        grid = _grid_dates(start, n)
        first = ql.Date.from_date(start).serialNumber()
        self.assertEqual([d.serialNumber() for d in grid], list(range(first, first + n)))

    def test_slices_are_consecutive_calendar_days(self):
        # 同じ範囲内 → 過去にはみ出す → 未来にはみ出す(作り直し)の順に確認する
        for start, n in [(AS_OF, 3700), (AS_OF + timedelta(days=1), 10), (date(1990, 1, 1), 400),
                         (date(2025, 3, 30), GRID_DAYS), (date(2090, 1, 1), 5)]:
            with self.subTest(start=start, n=n):
                self.assert_grid(start, n)


class QuotesTest(unittest.TestCase):
    def test_quotes_are_replaced_per_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = HistoricalStore(Path(tmp) / "historical.db")
            store.save_quotes({AS_OF: {"1M": 1.0, "5Y": 2.0}, date(2025, 4, 1): {"1M": 1.1}})
            store.save_quotes({AS_OF: {"5Y": 2.5}})
            self.assertEqual(store.quotes(), {AS_OF: {"5Y": 2.5}, date(2025, 4, 1): {"1M": 1.1}})


class AswQuotesTest(unittest.TestCase):
    def _store(self, tmp):
        return HistoricalStore(Path(tmp) / "historical.db")

    def test_upsert_by_date_and_bond_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            store.save_asw_quotes([
                AswQuoteRow(AS_OF, "000190054", 0.448, 0.4804, datetime(2026, 10, 9, 12, 30, 6)),
                AswQuoteRow(AS_OF, "000180054", 0.4488, 0.4813, datetime(2026, 10, 9, 12, 30, 6)),
            ])
            # 同じ(日付, 銘柄)を更新 → 上書き(行は増えない)
            store.save_asw_quotes([
                AswQuoteRow(AS_OF, "000190054", 0.45, 0.482, datetime(2026, 10, 10, 9, 0, 0)),
            ])
            quotes = store.asw_quotes()
            self.assertEqual(quotes, {
                AS_OF: {
                    "000190054": AswRate(0.45, 0.482),
                    "000180054": AswRate(0.4488, 0.4813),
                },
            })

    def test_max_updated_at_is_the_watermark(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self.assertIsNone(store.asw_quotes_max_updated_at())  # 保存前はNone(全件取得の指示)
            store.save_asw_quotes([
                AswQuoteRow(AS_OF, "000190054", 0.448, 0.4804, datetime(2026, 10, 9, 12, 30, 6)),
                AswQuoteRow(AS_OF, "000180054", 0.4488, 0.4813, datetime(2026, 10, 10, 9, 0, 0)),
            ])
            self.assertEqual(store.asw_quotes_max_updated_at(), datetime(2026, 10, 10, 9, 0, 0))


if __name__ == "__main__":
    unittest.main()
