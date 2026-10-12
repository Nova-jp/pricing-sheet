"""
取引条件の型(engine/common/terms.py)のテスト。規約の値・tenor/endの指定・PAY/RECを、
型を作る時点で確かめること(計算の深いところでエラーにしない)。

実行方法:
    python -m unittest tests.test_terms -v
"""

import sys
import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from engine.common.terms import STANDARD, SwapConvention, SwapParams, SwapTerms  # noqa: E402

START = date(2026, 9, 29)


class SwapConventionTest(unittest.TestCase):
    def test_standard(self):
        self.assertEqual(STANDARD, SwapConvention("PA", "act/365fixed", "PA", "STD"))

    def test_unsupported_values_are_rejected(self):
        for kwargs, word in [
            (dict(fix_freq="XX"), "fix_freq"),
            (dict(float_freq="XX"), "float_freq"),
            (dict(fix_dcf="act/360"), "dcf"),
            (dict(roll_conv="XXX"), "roll"),
        ]:
            with self.subTest(**kwargs), self.assertRaisesRegex(ValueError, word):
                replace(STANDARD, **kwargs)

    def test_is_immutable(self):
        with self.assertRaises(FrozenInstanceError):
            STANDARD.roll_conv = "IMM"


class SwapTermsTest(unittest.TestCase):
    def test_tenor_or_end_exactly_one(self):
        SwapTerms(START, STANDARD, tenor="5y")
        SwapTerms(START, STANDARD, end=date(2031, 9, 29))
        for kwargs in (dict(), dict(tenor="5y", end=date(2031, 9, 29))):
            with self.subTest(**kwargs), self.assertRaisesRegex(ValueError, "tenor と end"):
                SwapTerms(START, STANDARD, **kwargs)


class SwapParamsTest(unittest.TestCase):
    def test_pay_rec_is_checked(self):
        terms = SwapTerms(START, STANDARD, tenor="5y")
        SwapParams(terms, 1e10, "REC")
        with self.assertRaisesRegex(ValueError, "pay_rec"):
            SwapParams(terms, 1e10, "XXX")


if __name__ == "__main__":
    unittest.main()
