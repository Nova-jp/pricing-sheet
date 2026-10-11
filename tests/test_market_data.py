"""
市場データの型(swap_pricing/market_data.py)のテスト。先読みの排除(評価日より前の実績だけ)を
作る時点で行うこと。

実行方法:
    python -m unittest tests.test_market_data -v
"""

import sys
import unittest
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import QuantLib as ql  # noqa: E402

from swap_pricing.market_data import Market  # noqa: E402

VAL = date(2026, 9, 25)


def _market(fixings=None):
    curve = ql.YieldTermStructureHandle(ql.FlatForward(ql.Date.from_date(VAL), 0.01, ql.Actual365Fixed()))
    return Market(VAL, curve, ql.Tonar(curve), fixings if fixings is not None else {})


class MarketTest(unittest.TestCase):
    def test_fixings_on_or_after_valuation_date_are_dropped(self):
        market = _market({date(2026, 9, 24): 0.5, VAL: 0.6, date(2026, 9, 28): 0.7})
        self.assertEqual(dict(market.fixings), {date(2026, 9, 24): 0.5})

    def test_fixings_are_read_only_copy(self):
        source = {date(2026, 9, 24): 0.5}
        market = _market(source)
        source[date(2026, 9, 22)] = 0.4  # 元の辞書を変えても市場データは変わらない
        self.assertEqual(dict(market.fixings), {date(2026, 9, 24): 0.5})
        with self.assertRaises(TypeError):
            market.fixings[date(2026, 9, 22)] = 0.4

    def test_default_has_no_fixings(self):
        self.assertEqual(dict(_market().fixings), {})


if __name__ == "__main__":
    unittest.main()
