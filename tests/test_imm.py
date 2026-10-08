"""
IMMコード(M27等)の解釈とIMMロールのスケジュールに対するリグレッションテスト。

実行方法:
    python -m unittest tests.test_imm -v
"""

import sys
import unittest
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import QuantLib as ql  # noqa: E402

from swap_pricing.conventions import forward_start_date, imm_code_to_date, is_imm_code, is_tenor  # noqa: E402
from swap_pricing.curve import bootstrap_curve  # noqa: E402
from swap_pricing.swap_pricer import _build_swap, price_swap  # noqa: E402
from tests.test_curve_repricing import SPOT_RATES  # noqa: E402

VALUATION = date(2026, 9, 25)


def _schedule(start, end, freq, roll):
    ql.Settings.instance().evaluationDate = ql.Date.from_date(VALUATION)
    swap = _build_swap(ql.Tonar(), start, freq, "act/365fixed", freq, "act/365fixed", roll,
                       1.0, "PAY", 0.0, end=end)
    return [d.to_date() for d in swap.fixedSchedule().dates()]


class ImmCodeTest(unittest.TestCase):
    def test_codes_map_to_third_wednesday(self) -> None:
        self.assertEqual(imm_code_to_date("M27"), date(2027, 6, 16))
        self.assertEqual(imm_code_to_date("Z26"), date(2026, 12, 16))
        self.assertEqual(imm_code_to_date(" u28 "), date(2028, 9, 20))  # 小文字・空白可

    def test_past_imm_date_is_not_shifted_a_decade(self) -> None:
        """ql.IMM.date('U6')は基準日以降の2036年になるが、U26は2026年のまま。"""
        self.assertEqual(imm_code_to_date("U26"), date(2026, 9, 16))

    def test_holiday_imm_date_is_returned_unadjusted(self) -> None:
        self.assertEqual(imm_code_to_date("H30"), date(2030, 3, 20))  # 春分の日

    def test_invalid_codes(self) -> None:
        for code in ("M7", "X27", "M2027", "27M", ""):
            with self.subTest(code=code):
                self.assertFalse(is_imm_code(code))
                with self.assertRaises(ValueError):
                    imm_code_to_date(code)


class TenorStartTest(unittest.TestCase):
    def test_is_tenor(self) -> None:
        for v in ("10y", "18M", " 2w ", "1D"):
            self.assertTrue(is_tenor(v), v)
        for v in ("M27", "10", "y10", "", None, date(2027, 1, 1)):
            self.assertFalse(is_tenor(v), v)

    def test_forward_start_is_unadjusted_spot_plus_tenor(self) -> None:
        self.assertEqual(forward_start_date(date(2026, 9, 29), "10y"), date(2036, 9, 29))
        self.assertEqual(forward_start_date(date(2026, 9, 29), "18m"), date(2028, 3, 29))


class ImmRollScheduleTest(unittest.TestCase):
    def test_imm_roll_quarterly_all_dates_on_imm(self) -> None:
        dates = _schedule(imm_code_to_date("Z26"), imm_code_to_date("U28"), "QA", "IMM")
        expected = [imm_code_to_date(c) for c in ("Z26", "H27", "M27", "U27", "Z27", "H28", "M28", "U28")]
        self.assertEqual(dates, expected)

    def test_imm_roll_annual_with_short_back_stub(self) -> None:
        dates = _schedule(imm_code_to_date("Z26"), imm_code_to_date("M28"), "PA", "IMM")
        self.assertEqual(dates, [date(2026, 12, 16), date(2027, 12, 15), date(2028, 6, 21)])

    def test_std_override_keeps_calendar_day_roll(self) -> None:
        """roll convをSTDで上書きすると、中間日は起算日と同じ日付(16日)でロールする。"""
        dates = _schedule(imm_code_to_date("Z26"), imm_code_to_date("U28"), "PA", "STD")
        self.assertEqual(dates, [date(2026, 12, 16), date(2027, 12, 16), date(2028, 9, 20)])

    def test_holiday_imm_date_is_adjusted_modified_following(self) -> None:
        dates = _schedule(imm_code_to_date("Z29"), imm_code_to_date("Z30"), "QA", "IMM")
        self.assertIn(date(2030, 3, 21), dates)  # 2030-03-20(春分の日)→翌営業日
        self.assertNotIn(date(2030, 3, 20), dates)

    def test_past_start_imm_swap_prices_with_fixings(self) -> None:
        from tests.test_past_start import FIXINGS
        boot = bootstrap_curve(VALUATION, SPOT_RATES)
        result = price_swap(
            boot.curve, VALUATION, imm_code_to_date("U26"), "QA", "act/365fixed", "QA",
            "act/365fixed", "IMM", 1e10, "PAY", end=imm_code_to_date("U28"), fix_rate=1.0,
            index=boot.index, fixings=FIXINGS,
        )
        self.assertEqual(result.maturity_date, date(2028, 9, 20))


if __name__ == "__main__":
    unittest.main()
