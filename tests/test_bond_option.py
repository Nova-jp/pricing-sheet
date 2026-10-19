"""
現物オプション(engine/common/bond.py・engine/bond_option/)のテスト。

コミットするテストは架空の銘柄で、式の性質(端数処理・日数・利払日・パリティ・補間)を確かめる。
ユーザーのプライサー(正)のサンプル(data/bond_option_samples.toml、LSEGデータ・取引の実データを含むため
コミットしない)があるときだけ、プレミアムまでの全桁一致も確かめる。

実行方法:
    python -m unittest tests.test_bond_option -v
"""

import math
import sys
import tempfile
import tomllib
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from engine.bond_option import pricer, vol  # noqa: E402
from engine.bond_option import udf as bo_udf  # noqa: E402
from engine.common import udf_common  # noqa: E402
from engine.common.bond import (  # noqa: E402
    Jgb, accrued_interest, coupon_dates, price_from_simple_yield, settlement_date,
    simple_yield_from_price, truncate3, years_to_maturity,
)
from engine.common.historical_store import HistoricalStore  # noqa: E402
from engine.common.jgb_source import JgbClose, JgbRecord, code_from_ric  # noqa: E402

SAMPLES = REPO_ROOT / "data" / "bond_option_samples.toml"
BOND = Jgb(2.0, date(2031, 3, 20))  # 架空の5年債(利払 3/20・9/20)


class TruncateTest(unittest.TestCase):
    def test_truncates_to_three_decimals(self):
        self.assertEqual(truncate3(91.3019), 91.301)
        self.assertEqual(truncate3(1.8759999), 1.875)

    def test_float_noise_does_not_drop_a_digit(self):
        self.assertEqual(truncate3(0.1 + 0.2 + 91.0), 91.3)  # 91.30000000000001
        self.assertEqual(truncate3(91.30099999999999), 91.301)


class SimpleYieldTest(unittest.TestCase):
    def test_price_and_yield_round_trip_within_truncation(self):
        start = date(2026, 10, 19)
        for y in (0.5, 1.5, 3.0, 4.0):
            p = price_from_simple_yield(BOND, y, start)
            back = simple_yield_from_price(BOND, p, start)
            self.assertLessEqual(abs(back - y), 0.002, (y, p, back))

    def test_par_when_yield_equals_coupon(self):
        self.assertEqual(price_from_simple_yield(BOND, 2.0, date(2026, 10, 19)), 100.0)

    def test_formula(self):
        start = date(2026, 10, 19)
        n = years_to_maturity(BOND, start)
        expected = (2.0 + 100 / n) / (2.5 + 100 / n) * 100
        self.assertEqual(price_from_simple_yield(BOND, 2.5, start), math.floor(expected * 1000) / 1000)

    def test_leap_day_is_excluded(self):
        # 2028-02-29 をまたぐ: 暦日 366日 → うるう日除き 365日 = 1年
        self.assertEqual(years_to_maturity(Jgb(1.0, date(2028, 10, 19)), date(2027, 10, 19)), 1.0)

    def test_start_on_or_after_maturity_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "償還日"):
            price_from_simple_yield(BOND, 1.0, BOND.maturity)


class AccruedAndCouponTest(unittest.TestCase):
    def test_accrued_is_calendar_days_over_365(self):
        self.assertAlmostEqual(accrued_interest(BOND, date(2026, 10, 19)), 2.0 * 29 / 365, places=12)

    def test_zero_on_coupon_date(self):
        self.assertAlmostEqual(accrued_interest(BOND, date(2026, 9, 20)), 0.0, places=12)

    def test_irregular_first_period_counts_from_nominal_coupon_date(self):
        # 発行日が名目の利払日(8/1)より後でも、経過利子は名目の利払日から数える
        bond = Jgb(1.5, date(2028, 8, 1))
        self.assertAlmostEqual(accrued_interest(bond, date(2026, 10, 19)), 1.5 * 79 / 365, places=12)

    def test_coupon_dates_between(self):
        self.assertEqual(coupon_dates(BOND, date(2026, 10, 19), date(2027, 4, 20)), [date(2027, 3, 20)])
        self.assertEqual(coupon_dates(BOND, date(2026, 10, 19), date(2026, 11, 18)), [])

    def test_coupon_dates_are_not_holiday_adjusted(self):
        # 2027-03-20 は土曜(春分の日の前後)でもずらさない
        self.assertIn(date(2027, 3, 20), coupon_dates(BOND, date(2027, 1, 1), date(2027, 12, 31)))

    def test_settlement_skips_holidays(self):
        self.assertEqual(settlement_date(date(2026, 10, 16), 1), date(2026, 10, 19))  # 金→月
        self.assertEqual(settlement_date(date(2026, 11, 2), 1), date(2026, 11, 4))  # 11/3 文化の日


class ForwardTest(unittest.TestCase):
    S0 = date(2026, 10, 19)

    def test_zero_repo_without_coupon_keeps_dirty_price(self):
        fwd = pricer.forward_price(BOND, 99.0, self.S0, date(2026, 11, 18), 0.0)
        self.assertAlmostEqual(fwd.dirty, 99.0 + accrued_interest(BOND, self.S0), places=12)
        self.assertAlmostEqual(fwd.clean, fwd.dirty - accrued_interest(BOND, date(2026, 11, 18)), places=12)

    def test_formula_with_coupon_in_between(self):
        se, r = date(2027, 4, 20), 20.0
        fwd = pricer.forward_price(BOND, 99.0, self.S0, se, r)
        dirty = (99.0 + accrued_interest(BOND, self.S0)) * (1 + 0.2 * (se - self.S0).days / 365)
        dirty -= 1.0 * (1 + 0.2 * (se - date(2027, 3, 20)).days / 365)
        self.assertAlmostEqual(fwd.dirty, dirty, places=12)

    def test_forward_settle_before_spot_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "スポット受渡日"):
            pricer.forward_price(BOND, 99.0, self.S0, date(2026, 10, 1), 1.0)

    def test_duration(self):
        fwd = pricer.forward_price(BOND, 99.0, self.S0, date(2026, 11, 18), 0.5)
        d = pricer.forward_duration(BOND, fwd.dirty, date(2026, 11, 18))
        self.assertEqual(d.compound_yield, truncate3(d.compound_yield))  # 小数3桁で切り捨て済み
        self.assertAlmostEqual(d.modified_duration, d.dollar_duration / fwd.dirty * 100, places=12)
        self.assertTrue(4.0 < d.modified_duration < 4.5, d)  # 残存約4.3年


class OptionTest(unittest.TestCase):
    def test_term_is_business_days_over_252(self):
        # 2026-10-16 → 2026-11-16: 11/3が祝日で20営業日
        self.assertEqual(pricer.option_term(date(2026, 10, 16), date(2026, 11, 16)), 20 / 252)

    def test_put_call_parity_without_discounting(self):
        f, k, v, t = 99.5, 99.0, 4.0, 0.25
        call = pricer.black_premium("Call", f, k, v, t)
        put = pricer.black_premium("put", f, k, v, t)
        self.assertAlmostEqual(call - put, f - k, places=12)

    def test_zero_vol_is_intrinsic(self):
        self.assertAlmostEqual(pricer.black_premium("C", 99.5, 99.0, 0.0, 0.25), 0.5, places=12)

    def test_unknown_type_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Call/Put"):
            pricer.black_premium("straddle", 99.5, 99.0, 4.0, 0.25)


class VolTest(unittest.TestCase):
    T0 = date(2026, 10, 16)
    ROWS = [
        ["1Mx1Y", "R", 40.0], ["1Mx5Y", "R", 60.0], ["1Mx10Y", "R", 80.0],
        ["1Yx1Y", "R", 50.0], ["1Yx5Y", "R", 70.0], ["1Yx10Y", "R", "#N/A"],
        ["2Yx5Y", "R", None],  # 値の無い満期は使わない
    ]

    def _vol(self, expiry, maturity):
        return vol.bond_option_vol(self.ROWS, self.T0, expiry, maturity)

    def test_on_pillar(self):
        self.assertAlmostEqual(self._vol(date(2026, 11, 16), date(2031, 11, 16)), 60.0, places=9)

    def test_linear_in_tenor(self):
        # 残存7.5年: 1Mの 5Y(60)と10Y(80)の中間 → 70
        self.assertAlmostEqual(self._vol(date(2026, 11, 16), date(2034, 5, 17)), 70.0, delta=0.01)

    def test_flat_outside_the_grid(self):
        self.assertAlmostEqual(self._vol(date(2026, 10, 30), date(2027, 10, 30)), 40.0, delta=0.05)  # 1M未満・1Y未満
        self.assertAlmostEqual(self._vol(date(2026, 11, 16), date(2056, 11, 16)), 80.0, places=9)  # 10Y超
        self.assertAlmostEqual(self._vol(date(2029, 10, 16), date(2034, 10, 16)), 70.0, places=9)  # 1Y超(2Yは値なし)

    def test_variance_interpolation_in_expiry(self):
        expiry, maturity = date(2027, 4, 16), date(2032, 4, 16)  # 6M満期、残存5年
        t, t1, t2 = 182 / 365, 31 / 365, 365 / 365  # うるう日除き/365
        w = 60.0 ** 2 * t1 + (70.0 ** 2 * t2 - 60.0 ** 2 * t1) * (t - t1) / (t2 - t1)
        self.assertAlmostEqual(self._vol(expiry, maturity), math.sqrt(w / t), places=9)

    def test_missing_points_are_skipped_in_tenor(self):
        # 1Yの10Yは#N/A → 1Yは5Yまでの点で平らに延ばす
        self.assertAlmostEqual(self._vol(date(2027, 10, 16), date(2037, 10, 16)), 70.0, places=9)

    def test_bad_label_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "満期x原資産"):
            vol.bond_option_vol([["10Y", "R", 1.0]], self.T0, date(2026, 11, 16), date(2031, 11, 16))

    def test_empty_grid_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "格子に値がありません"):
            vol.bond_option_vol([["1Mx1Y", "R", "#N/A"]], self.T0, date(2026, 11, 16), date(2031, 11, 16))


class JgbSourceTest(unittest.TestCase):
    def test_code_from_ric(self):
        self.assertEqual(code_from_ric("JP03800067="), "JB380")
        self.assertEqual(code_from_ric("JP04870042="), "JN487")
        self.assertEqual(code_from_ric("JP00150054="), "JU15")
        self.assertIsNone(code_from_ric("JP03800067=JCL"))
        self.assertIsNone(code_from_ric("JP03800099="))


class UdfTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = HistoricalStore(Path(self._tmp.name) / "historical.db")
        self.store.save_jgb_bonds([JgbRecord("JS999", "JP09990045=", "XX", 2.0, date(2031, 3, 20))])
        self.store.save_jgb_closes([JgbClose(date(2026, 10, 15), "JS999", 2.10), JgbClose(date(2026, 10, 16), "JS999", 2.12)])
        patcher = mock.patch.object(udf_common, "_historical_store", lambda: self.store)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def test_info_and_close(self):
        self.assertEqual(bo_udf.JgbInfo("js999"), [[2.0, date(2031, 3, 20)]])
        self.assertEqual(bo_udf.JgbClose("JS999", date(2026, 10, 19)), [[2.12, date(2026, 10, 16)]])
        self.assertEqual(bo_udf.JgbClose("JS999", date(2026, 10, 15)), [[2.10, date(2026, 10, 15)]])

    def test_unknown_code(self):
        with self.assertRaisesRegex(ValueError, "DBにありません"):
            bo_udf.JgbInfo("JB000")

    def test_row_chain(self):
        """1行の組み合わせ(シートと同じ順)が計算できる。"""
        t0, expiry = date(2026, 10, 16), date(2026, 11, 16)
        atm = bo_udf.JgbClose("JS999", t0)[0][0] - 0.5 / 100
        s0, se = bo_udf.JgbSettle(t0, 1), bo_udf.JgbSettle(expiry, 2)
        spot = bo_udf.JgbPrice("JS999", atm, s0)
        strike = bo_udf.JgbPrice("JS999", atm + 3 / 100, expiry)
        clean, dirty = bo_udf.BondForward("JS999", spot, s0, se, 0.5)[0]
        _, _, md = bo_udf.ForwardDuration("JS999", dirty, se)[0]
        normal = bo_udf.SwaptionVol([["1Mx1Y", "R", 40.0], ["1Mx10Y", "R", 80.0]], t0, expiry, "JS999")
        price_vol = normal / math.sqrt(252) * math.sqrt(252) * md / 100
        premium = bo_udf.BlackPremium("Put", clean, strike, price_vol, bo_udf.OptionTerm(t0, expiry))
        self.assertGreater(premium, 0.0)
        self.assertLess(strike, spot)  # 利回りを上にずらすと単価は下がる


@unittest.skipUnless(SAMPLES.exists(), "ユーザーのサンプル(data/bond_option_samples.toml)はローカルにだけある")
class UserSamplesTest(unittest.TestCase):
    """ユーザーのプライサー(正)との照合。入力からプレミアムまで通しで計算する。"""

    SWAP_VOLS = {"JN487_put_1M": 1.72121, "JB380_call_6M_repo20": 3.409683}  # JN487は画面の値からの逆算

    def test_samples(self):
        data = tomllib.loads(SAMPLES.read_text(encoding="utf-8"))
        bonds = {b["code"]: Jgb(b["coupon"], b["maturity"]) for b in data["bond"]}
        for s in data["sample"]:
            with self.subTest(s["id"]):
                b, t0, te = bonds[s["bond"]], s["trade_date"], s["expiry"]
                s0, se = settlement_date(t0, 1), settlement_date(te, s["exercise_settle_lag"])
                atm = s["atm_simple_yield"]
                spot = price_from_simple_yield(b, atm, s0)
                self.assertEqual(spot, s["atm_price"])
                fwd = pricer.forward_price(b, spot, s0, se, s["repo"])
                self.assertAlmostEqual(fwd.clean, s["bs_fwd_price"], delta=0.0005)
                if "premium" not in s:
                    continue
                strike = price_from_simple_yield(b, atm + s.get("strike_slide", 0) / 100, te)
                self.assertEqual(strike, s["bs_strike"])
                self.assertEqual(simple_yield_from_price(b, strike, se), s["strike_simple_yield_display"])
                md = pricer.forward_duration(b, fwd.dirty, se).modified_duration
                swap_vol = s.get("swap_vol", self.SWAP_VOLS.get(s["id"]))
                price_vol = swap_vol * math.sqrt(252) * md / 100
                self.assertAlmostEqual(price_vol, s["bs_price_vol"], delta=2e-5)
                premium = pricer.black_premium(s["type"], fwd.clean, strike, price_vol, pricer.option_term(t0, te))
                self.assertEqual(round(premium * 100, 4), s["premium"])


if __name__ == "__main__":
    unittest.main()
