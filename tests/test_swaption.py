"""
スワップションのプレミアム → 営業日ベースのvol(engine/common/swaption.py)と、
Traditionのプレミアムの取得(engine/common/swaption_source.py、LSEGには接続しない)のテスト。

実行方法:
    python -m unittest tests.test_swaption -v
"""

import math
import sys
import unittest
from datetime import date, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import QuantLib as ql  # noqa: E402

from engine.common import swaption_source  # noqa: E402
from engine.common.curve import bootstrap_curve  # noqa: E402
from engine.common.swaption import (  # noqa: E402
    PremiumConverter, business_days, expiry_date, parse_label, vol_from_stdev,
)
from tests.test_curve_repricing import SPOT_RATES  # noqa: E402

T0 = date(2026, 10, 23)  # 金曜


class DatesTest(unittest.TestCase):
    def test_expiry_is_trade_date_plus_period_following(self):
        self.assertEqual(expiry_date(T0, "1Y"), date(2027, 10, 25))  # 2027-10-23は土曜
        self.assertEqual(expiry_date(T0, "1M"), date(2026, 11, 24))  # 11/23は勤労感謝の日
        self.assertEqual(expiry_date(T0, "3M"), date(2027, 1, 25))  # 1/23は土曜

    def test_business_days_count_today_and_not_the_expiry_day(self):
        self.assertEqual(business_days(T0, date(2026, 10, 26)), 1)  # 金曜の当日だけ
        self.assertEqual(business_days(T0, date(2026, 10, 27)), 2)
        self.assertEqual(business_days(T0, date(2026, 11, 24)), 20)  # 11/3・11/23の祝日を除く

    def test_parse_label(self):
        self.assertEqual(parse_label("1yx10y"), ("1Y", "10Y"))
        with self.assertRaisesRegex(ValueError, "満期x原資産"):
            parse_label("10Y")


class ConverterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = bootstrap_curve(T0, {"O/N": 0.477, **SPOT_RATES})
        ql.Settings.instance().evaluationDate = ql.Date.from_date(T0)
        cls.converter = PremiumConverter(result.curve, T0)

    def test_premium_is_the_forward_atm_straddle_on_the_annuity(self):
        # 逆向き: 求めたσ√TでQuantLibのバシュリエ式からストラドルを組むと、元のプレミアムに戻る
        v = self.converter.convert("1Yx10Y", 450.0)
        straddle = 2 * ql.bachelierBlackFormula(ql.Option.Call, v.forward / 100, v.forward / 100, v.stdev / 1e4, 1.0)
        self.assertAlmostEqual(straddle * v.annuity * 1e4, 450.0, places=8)
        self.assertEqual((v.expiry, v.start), (date(2027, 10, 25), date(2027, 10, 27)))
        self.assertEqual(v.days, business_days(T0, v.expiry))

    def test_vol_is_stdev_per_business_day_annualized_by_252(self):
        v = self.converter.convert("1Mx10Y", 130.0)
        self.assertEqual(v.days, 20)
        self.assertAlmostEqual(v.vol, v.stdev / math.sqrt(20) * math.sqrt(252), places=12)
        self.assertEqual(vol_from_stdev(v.stdev, v.days), v.vol)

    def test_forward_and_annuity_are_the_forward_swap(self):
        from engine.common.swap_pricer import PAY, _build_swap
        from engine.common.terms import STANDARD, SwapTerms

        v = self.converter.convert("2Yx5Y", 300.0)
        swap = _build_swap(self.converter.index, SwapTerms(start=v.start, convention=STANDARD, tenor="5Y"), 1.0, PAY, 0.0)
        swap.setPricingEngine(ql.DiscountingSwapEngine(self.converter.curve))
        self.assertAlmostEqual(v.forward, swap.fairRate() * 100, places=12)
        df = self.converter.curve.discount(ql.Date.from_date(v.start))
        self.assertAlmostEqual(v.annuity, abs(swap.fixedLegBPS()) / 1e-4 / df, places=12)

    def test_missing_premium_is_none(self):
        for premium in (None, 0.0, -1.0, float("nan"), True):
            self.assertIsNone(self.converter.convert("1Yx10Y", premium))


class SourceTest(unittest.TestCase):
    def test_ric_and_labels(self):
        self.assertEqual(swaption_source.ric("1Y", "10Y"), "JPY1Y10YTOATM=TRDT")
        labels = swaption_source.labels()
        self.assertEqual(len(labels), 13 * 12)
        self.assertNotIn("2Mx1Y", labels)  # 2Mにヒストリカルは無い
        self.assertNotIn("6Yx10Y", labels)

    def test_parse_history_table(self):
        import pandas as pd

        idx = pd.DatetimeIndex([datetime(2026, 10, 22), datetime(2026, 10, 23)], name="Date")
        cols = pd.MultiIndex.from_tuples([
            ("JPY1Y10YTOATM=TRDT", "FWPM_OCPHM"), ("JPY1Y10YTOATM=TRDT", "ATM_STRIKE"),
            ("JPY1M1YTOATM=TRDT", "FWPM_OCPHM"), ("JPY1M1YTOATM=TRDT", "ATM_STRIKE"),
        ])
        df = pd.DataFrame([[440.0, 3.0, None, None], [450.0, 2.95, 7.0, 1.2]], index=idx, columns=cols)
        quotes = swaption_source._parse(df, {"JPY1Y10YTOATM=TRDT": "1Yx10Y", "JPY1M1YTOATM=TRDT": "1Mx1Y"})
        self.assertEqual(sorted(quotes), sorted([
            swaption_source.PremiumQuote(date(2026, 10, 22), "1Yx10Y", 440.0, 3.0),
            swaption_source.PremiumQuote(date(2026, 10, 23), "1Yx10Y", 450.0, 2.95),
            swaption_source.PremiumQuote(date(2026, 10, 23), "1Mx1Y", 7.0, 1.2),
        ]))

    def test_parse_single_ric_table(self):
        import pandas as pd

        idx = pd.DatetimeIndex([datetime(2026, 10, 23)])
        df = pd.DataFrame({"FWPM_OCPHM": [450.0], "ATM_STRIKE": [2.9]}, index=idx)
        self.assertEqual(swaption_source._parse(df, {"JPY1Y10YTOATM=TRDT": "1Yx10Y"}),
                         [swaption_source.PremiumQuote(date(2026, 10, 23), "1Yx10Y", 450.0, 2.9)])


if __name__ == "__main__":
    unittest.main()
