"""
オプションの満期までの時間(engine/common/option/term.py)のテスト。
スワップションのvolの換算・現物オプションのT・ボラの格子の補間が同じ数え方を使うことも確かめる。
"""

import sys
import unittest
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from engine.common.option import term  # noqa: E402
from engine.common.option.bond import pricer, vol  # noqa: E402
from engine.common.option.swaption import convert  # noqa: E402

T0 = date(2026, 10, 23)  # 金曜


class TermTest(unittest.TestCase):
    def test_business_days_count_today_and_not_the_expiry_day(self):
        self.assertEqual(term.business_days(T0, date(2026, 10, 26)), 1)  # 金曜の当日だけ
        self.assertEqual(term.business_days(T0, date(2026, 11, 24)), 20)  # 11/3・11/23の祝日を除く

    def test_option_term_is_business_days_over_252(self):
        self.assertEqual(term.option_term(T0, date(2026, 11, 24)), 20 / 252)

    def test_expiry_on_or_before_trade_date_is_rejected(self):
        for expiry in (T0, date(2026, 10, 22)):
            with self.assertRaises(ValueError):
                term.option_term(T0, expiry)

    def test_one_day_is_one_business_day_in_years(self):
        self.assertEqual(term.ONE_DAY, 1 / term.TRADING_DAYS)
        self.assertEqual(term.TRADING_DAYS, 252)


class SharedCountTest(unittest.TestCase):
    """volを割った日数とTの日数が同じ関数から来る(ずれるとプレミアムを再現できない)。"""

    def test_swaption_contract_uses_the_same_count(self):
        u = convert.underlying(T0, "1Mx10Y")
        self.assertEqual(u.days, term.business_days(T0, u.expiry))
        self.assertEqual(term.option_term(T0, u.expiry), u.days / 252)

    def test_modules_share_the_same_objects(self):
        self.assertIs(convert.business_days, term.business_days)
        self.assertIs(vol.business_days, term.business_days)
        self.assertIs(convert.TRADING_DAYS, term.TRADING_DAYS)

    def test_theta_is_one_business_day(self):
        t = term.option_term(T0, date(2026, 11, 24))
        g = pricer.black_greeks("Call", 100.0, 100.0, 4.0, t)
        later = pricer.black_premium("Call", 100.0, 100.0, 4.0, t - term.ONE_DAY)
        self.assertAlmostEqual(g.theta, later - g.premium, places=12)


if __name__ == "__main__":
    unittest.main()
