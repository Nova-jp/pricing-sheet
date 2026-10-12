"""
ヘッジのヒストリカル(HistChartで系列1に重ねる仮想金利)用のテナー別パーレートのテスト。

実行方法:
    python -m unittest tests.test_hedge -v
    (カーブは全日付で同じ合成スポットレートからその場で組む。パーレートの値そのものは
     historical.pricer と同じ経路なので、ここでは日付の並び・欠損の扱い・入力の確認だけを見る)
"""

import sys
import unittest
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from engine.common.curve import bootstrap_curve, normalize_tenor  # noqa: E402
from engine.common.terms import STANDARD  # noqa: E402
from engine.swap_pricing.historical.hedge import hedge_rate_rows, hedge_tenor  # noqa: E402
from engine.swap_pricing.historical.pricer import (  # noqa: E402
    FIXED,
    ROLLING,
    SwapSpec,
    historical_multi_rate_series,
)
from tests.test_curve_repricing import SPOT_RATES  # noqa: E402

BASE = date(2026, 9, 25)
AS_OF_DATES = [date(2026, 3, 25), date(2026, 6, 25), BASE]
ALL_TENORS = {normalize_tenor(k) for k in SPOT_RATES}


class _Source:
    """テスト用のカーブ供給元。used で日付ごとのカーブに使ったテナーを差し替えられる。"""

    def __init__(self, dates=AS_OF_DATES, used=None):
        self._dates = list(dates)
        self._used = used or {}

    def dates(self):
        return self._dates

    def load(self, as_of_date, until=None):
        return bootstrap_curve(as_of_date, SPOT_RATES)

    def used_tenors(self):
        return {d: set(self._used.get(d, ALL_TENORS)) for d in self._dates}


def _expected(tenors, mode):
    series = historical_multi_rate_series(
        [SwapSpec(STANDARD, tenor=t) for t in tenors], mode, base_date=BASE, curves=_Source(),
    )
    return {d: [leg.par_rate for leg in legs] for d, legs in series}


class HedgeRateRowsTest(unittest.TestCase):
    def test_rows_follow_input_dates(self):
        """行は dates の並び(降順・カーブの無い日を含む)どおり。値はスポットの標準スワップのヒストリカル。"""
        tenors = ["2Y", "5Y", "10Y"]
        expected = _expected(tenors, ROLLING)
        dates = [BASE, date(2026, 6, 26), date(2026, 3, 25)]  # 6/26はカーブ無し
        rows = hedge_rate_rows(tenors, ROLLING, dates, base_date=BASE, curves=_Source())
        self.assertEqual(rows, [expected[BASE], [None, None, None], expected[date(2026, 3, 25)]])

    def test_fixed_mode_is_passed_through(self):
        tenors = ["5Y"]
        expected = _expected(tenors, FIXED)
        rows = hedge_rate_rows(tenors, FIXED, AS_OF_DATES, base_date=BASE, curves=_Source())
        self.assertEqual(rows, [expected[d] for d in AS_OF_DATES])
        self.assertNotEqual(rows, hedge_rate_rows(tenors, ROLLING, AS_OF_DATES, base_date=BASE, curves=_Source()))

    def test_tenor_not_used_in_curve_is_blank(self):
        """その日のカーブに使っていないテナー(クォートが無かった日)は補間値を出さず空欄。"""
        missing = date(2026, 6, 25)
        source = _Source(used={missing: ALL_TENORS - {"5y"}})
        rows = hedge_rate_rows(["2Y", "5Y"], ROLLING, AS_OF_DATES, base_date=BASE, curves=source)
        self.assertIsNone(rows[1][1])
        self.assertIsNotNone(rows[1][0])
        self.assertTrue(all(v is not None for v in rows[0] + rows[2]))

    def test_alias_tenor_matches_used_tenors(self):
        """RealTimeのTicker表記(12M/24M/36M)はカーブの表記(1y/2y/3y)と突き合わせる。"""
        rows = hedge_rate_rows(["12M", "36M"], ROLLING, [BASE], base_date=BASE, curves=_Source())
        self.assertTrue(all(v is not None for v in rows[0]))

    def test_no_tenors_returns_empty_rows(self):
        self.assertEqual(hedge_rate_rows([], ROLLING, AS_OF_DATES, curves=_Source()), [[], [], []])


class HedgeTenorTest(unittest.TestCase):
    def test_spot_tenors_are_accepted(self):
        for label in ["1W", "12M", " 5y ", "40Y"]:
            self.assertEqual(hedge_tenor(label), label.strip())

    def test_non_spot_tenors_are_rejected(self):
        for label in ["O/N", "RD", "M1", "abc"]:
            with self.subTest(label=label), self.assertRaises(ValueError):
                hedge_tenor(label)

    def test_invalid_tenor_is_reported_with_position(self):
        with self.assertRaisesRegex(ValueError, "2本目"):
            hedge_rate_rows(["5Y", "M1"], ROLLING, AS_OF_DATES, curves=_Source())


if __name__ == "__main__":
    unittest.main()
