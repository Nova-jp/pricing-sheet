"""
オプションの満期までの時間(engine/common/option/term.py)のテスト。
スワップションのvolの換算・現物オプションのT・ボラの格子の補間が同じ数え方を使うことも確かめる。
"""

import math
import sys
import tempfile
import unittest
from datetime import date, datetime, time
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
        self.assertIs(vol.remaining_days, term.remaining_days)
        self.assertIs(convert.TRADING_DAYS, term.TRADING_DAYS)

    def test_theta_is_one_business_day(self):
        t = term.option_term(T0, date(2026, 11, 24))
        g = pricer.black_greeks("Call", 100.0, 100.0, 4.0, t)
        later = pricer.black_premium("Call", 100.0, 100.0, 4.0, t - term.ONE_DAY)
        self.assertAlmostEqual(g.theta, later - g.premium, places=12)


def at(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(d, time(hh, mm))


THU, FRI, MON = date(2026, 11, 19), date(2026, 11, 20), date(2026, 11, 24)  # 11/23(月)は勤労感謝の日
EXPIRY = date(2026, 12, 21)  # 月曜


class IntradayTest(unittest.TestCase):
    """満期日の15時に0。1営業日をイブニング(15:30〜翌6:00)1 : 日中(8:45〜15:00)2 で減らす(option_time.toml)。"""

    def setUp(self):
        self._previous = term.set_config(term.TimeConfig())
        self.addCleanup(term.set_config, self._previous)
        self.n = term.business_days(FRI, EXPIRY)  # 金曜の引け時点

    def test_close_equals_the_date_count(self):
        self.assertEqual(term.remaining_days(at(FRI, 15), EXPIRY), self.n)
        self.assertEqual(term.remaining_days(FRI, EXPIRY), self.n)

    def test_day_session_decreases_linearly(self):
        self.assertAlmostEqual(term.remaining_days(at(FRI, 8, 45), EXPIRY), self.n + 2 / 3, places=12)
        # 8:45〜15:00の真ん中(11:52:30)で日中の半分(1/3日)が減る
        mid = datetime.combine(FRI, time(11, 52, 30))
        self.assertAlmostEqual(term.remaining_days(mid, EXPIRY), self.n + 1 / 3, places=12)

    def test_evening_decreases_linearly_and_gaps_are_flat(self):
        # 木曜の15時過ぎ〜15:30は止まる(金曜の1営業日が丸々残る)
        self.assertAlmostEqual(term.remaining_days(at(THU, 15, 1), EXPIRY), self.n + 1, places=12)
        self.assertAlmostEqual(term.remaining_days(at(THU, 15, 30), EXPIRY), self.n + 1, places=12)
        # 15:30〜翌6:00(14.5時間)の真ん中の22:45でイブニングの半分(1/6日)が減る
        self.assertAlmostEqual(term.remaining_days(at(THU, 22, 45), EXPIRY), self.n + 5 / 6, places=12)
        # 6:00〜8:45は止まる
        self.assertAlmostEqual(term.remaining_days(at(FRI, 6), EXPIRY), self.n + 2 / 3, places=12)
        self.assertAlmostEqual(term.remaining_days(at(FRI, 7), EXPIRY), self.n + 2 / 3, places=12)

    def test_continuous_across_the_close(self):
        before = term.remaining_days(datetime.combine(FRI, time(14, 59, 59)), EXPIRY)
        after = term.remaining_days(datetime.combine(FRI, time(15, 0, 1)), EXPIRY)
        self.assertAlmostEqual(before, self.n, delta=1e-4)
        self.assertAlmostEqual(after, self.n, places=12)

    def test_weekend_and_holiday_only_the_friday_evening_decreases(self):
        # 金曜の夜〜土曜の朝がイブニング。その後は火曜(月曜は祝日)の8:45まで減らない
        self.assertAlmostEqual(term.remaining_days(at(date(2026, 11, 21), 12), EXPIRY), self.n - 1 + 2 / 3, places=12)
        self.assertAlmostEqual(term.remaining_days(at(date(2026, 11, 23), 20), EXPIRY), self.n - 1 + 2 / 3, places=12)
        self.assertAlmostEqual(term.remaining_days(at(MON, 15), EXPIRY), self.n - 1, places=12)

    def test_expiry_day(self):
        self.assertAlmostEqual(term.remaining_days(at(EXPIRY, 8, 45), EXPIRY), 2 / 3, places=12)
        self.assertAlmostEqual(term.option_term(at(EXPIRY, 8, 45), EXPIRY), 2 / 3 / 252, places=15)
        self.assertEqual(term.remaining_days(at(EXPIRY, 15), EXPIRY), 0)
        for when in (at(EXPIRY, 15), at(EXPIRY, 16), EXPIRY):
            with self.assertRaises(ValueError):
                term.option_term(when, EXPIRY)

    def test_daily_method_ignores_the_time(self):
        term.set_config(term.TimeConfig(method=term.DAILY))
        self.assertEqual(term.remaining_days(at(FRI, 8, 45), EXPIRY), self.n)

    def test_calendar_days(self):
        self.assertEqual(term.calendar_days(FRI, EXPIRY), (EXPIRY - FRI).days)
        self.assertAlmostEqual(term.calendar_days(at(FRI, 9), EXPIRY), (EXPIRY - FRI).days + 0.25, places=12)

    def test_swaption_vol_divides_the_same_stdev_by_the_remaining_days(self):
        u = convert.underlying(FRI, "1Mx10Y")
        close = term.remaining_days(FRI, u.expiry)
        morning = term.remaining_days(at(FRI, 8, 45), u.expiry)
        self.assertAlmostEqual(convert.vol_from_stdev(10.0, morning) / convert.vol_from_stdev(10.0, close),
                               math.sqrt(close / morning), places=12)


class ConfigFileTest(unittest.TestCase):
    def _load(self, text: str) -> term.TimeConfig:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "option_time.toml"
            path.write_text(text, encoding="utf-8")
            return term.load_config(path)

    def test_repository_file_is_the_decided_default(self):
        self.assertEqual(term.load_config(), term.TimeConfig())

    def test_missing_file_is_the_default(self):
        self.assertEqual(term.load_config(Path(tempfile.gettempdir()) / "no_such_option_time.toml"), term.TimeConfig())

    def test_custom_sessions(self):
        cfg = self._load('expiry_time = "15:00"\nmethod = "session"\n'
                         '[[session]]\nname = "day"\nstart = "09:00"\nend = "15:00"\nweight = 1\n')
        self.assertEqual(cfg.sessions, (term.Session("day", time(9), time(15), 1.0),))
        self.assertAlmostEqual(term.remaining_fraction(at(FRI, 12), cfg), 0.5, places=12)

    def test_bad_files_are_rejected(self):
        for text, message in [
            ('method = "hourly"\n', "method"),
            ('foo = 1\n', "知らない項目"),
            ('[[session]]\nstart = "08:45"\nend = "15:00"\nweight = 0\n', "weight"),
            ('[[session]]\nstart = "08:45"\nend = "16:00"\nweight = 1\n', "はみ出し"),
            ('[[session]]\nstart = "08:45"\nend = "15:00"\nweight = 1\n'
             '[[session]]\nstart = "12:00"\nend = "13:00"\nweight = 1\n', "重なって"),
            ('[[session]]\nstart = "8時"\nend = "15:00"\nweight = 1\n', "HH:MM"),
        ]:
            with self.assertRaisesRegex(ValueError, message):
                self._load(text)


if __name__ == "__main__":
    unittest.main()
