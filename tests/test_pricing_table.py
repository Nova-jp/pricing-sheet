"""
pricing表を1回で計算するUDF(PricingTable、engine/swap_pricing/pricing/table.py)のリグレッションテスト。

- 各行の結果が、1行用のUDFの組み立て(QuantLibの評価: price・パラレルデルタ・アニュイティ)と一致すること
- flag=1の行だけ計算し、notional・deltaが空欄の行はその列だけ空欄にすること
- 1行の入力の誤りは、その行の列を空欄にしてmessage列に理由を出すこと(表全体をエラーにしない)
- 1行あたりの評価が3回(par・PV・パラレル)で済むこと

実行方法:
    python -m unittest tests.test_pricing_table -v
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from engine.common import market, udf_common  # noqa: E402
from engine.common.conventions import forward_start_date  # noqa: E402
from engine.common.risk import annuity_delta, parallel_delta, price, resolve_params, solve_notional_for_target_delta  # noqa: E402
from engine.common.terms import SwapParams, SwapTerms  # noqa: E402
from engine.swap_pricing import udf  # noqa: E402
from engine.swap_pricing.pricing import measures, table  # noqa: E402
from tests.test_market_risk import NOTIONAL, _rt_range  # noqa: E402
from tests.test_past_start import CONV  # noqa: E402

# シートから渡す入力の列(出力の列F・P・T〜Wを含めない。HSTACK(A:E, G:O, S))
_HEADER = ["flag", "risk_flag", "remarks", "notional", "delta", "start", "tenor", "end", "fix rate",
           "fix freq", "fix dcf", "index", "float freq", "roll conv", "pay/rec"]


def _row(flag=1, start="2bd", tenor="5y", notional=NOTIONAL, delta="", fix_rate="", pay_rec="PAY", end=None):
    return [flag, 1, "", notional, delta, start, tenor, end, fix_rate, "PA", "act/365fixed", "JSCC", "PA",
            "STD", pay_rec]


class PricingTableTest(unittest.TestCase):
    def setUp(self):
        market._cache.clear()
        self.rt = _rt_range()
        self.curves = udf_common._market(self.rt, None)
        self.spot = self.curves.spot_date

    def _expected(self, start, tenor, notional, pay_rec, fix_rate=None, delta_mm=None):
        """1行用のUDFと同じ組み立てをriskの関数で直接計算する(比較の基準)。"""
        terms = SwapTerms(start, CONV, tenor=tenor)
        params = SwapParams(terms, float(notional), pay_rec, fix_rate=fix_rate)
        par = price(self.curves.base, SwapParams(terms, 1.0, pay_rec))
        resolved = resolve_params(self.curves, params)
        adj = "" if delta_mm is None else solve_notional_for_target_delta(delta_mm * 1e6, par.annuity, pay_rec)
        return [
            par.target_fixrate,
            price(self.curves.base, resolved).pv,
            parallel_delta(self.curves, resolved) / 1e6,
            annuity_delta(par.annuity, float(notional), pay_rec) / 1e6,
            adj,
        ]

    def _assert_row(self, actual, expected):
        self.assertEqual(actual[5], "")
        # fixrate(%)・delta(百万円)は1e-9、PV(円)・adj_notional(円)は想定元本100億円で0.01円
        for value, want, tol in zip(actual[:5], expected, (1e-9, 0.01, 1e-9, 1e-9, 0.01)):
            if want == "":
                self.assertEqual(value, "")
            else:
                self.assertAlmostEqual(value, want, delta=tol)

    def test_rows_match_per_row_pricing(self):
        result = udf.PricingTable([
            _HEADER,
            _row(start="2bd", tenor="5y"),
            _row(start="1y", tenor="10y", pay_rec="REC", fix_rate=1.5, delta=3.0),
            _row(start="2bd", tenor="30y", notional=5_000_000_000, delta=-2.0),
        ], self.rt)
        self.assertEqual(result[0], list(table.OUTPUT_HEADER))
        self.assertEqual(len(result), 4)
        self._assert_row(result[1], self._expected(self.spot, "5y", NOTIONAL, "PAY"))
        self._assert_row(result[2], self._expected(
            forward_start_date(self.spot, "1y"), "10y", NOTIONAL, "REC", fix_rate=1.5, delta_mm=3.0))
        self._assert_row(result[3], self._expected(self.spot, "30y", 5_000_000_000, "PAY", delta_mm=-2.0))

    def test_matches_single_row_udfs(self):
        """1行用のUDF(FairRate等)とPricingTableは同じ部品を使い、同じ値になる。"""
        row = _row(start="2bd", tenor="7y", fix_rate=1.2, delta=1.0)
        result = udf.PricingTable([_HEADER, row], self.rt)[1]
        conv = ("PA", "act/365fixed", "PA", "STD")
        self.assertEqual(result[0], udf.FairRate("2bd", "7y", None, *conv, self.rt))
        self.assertEqual(result[1], udf.SwapPV("2bd", "7y", None, *conv, NOTIONAL, "PAY", 1.2, self.rt))
        self.assertEqual(result[2], udf.SwapDelta("2bd", "7y", None, *conv, NOTIONAL, "PAY", 1.2, self.rt))
        self.assertEqual(result[3], udf.SwapAnnuityDelta("2bd", "7y", None, *conv, NOTIONAL, "PAY", self.rt))
        self.assertEqual(result[4], udf.NotionalForDelta(1.0, "2bd", "7y", None, *conv, "PAY", self.rt))

    def test_only_flagged_rows_are_priced(self):
        result = udf.PricingTable([
            _HEADER, _row(flag=0), _row(flag=""), [None] * len(_HEADER), [], _row(flag=1),
        ], self.rt)
        for r in result[1:5]:
            self.assertEqual(r, [""] * 6)
        self.assertIsInstance(result[5][0], float)

    def test_blank_notional_and_delta_leave_their_columns_blank(self):
        result = udf.PricingTable([_HEADER, _row(notional="", delta="")], self.rt)[1]
        self.assertIsInstance(result[0], float)
        self.assertEqual(result[1:], ["", "", "", "", ""])

    def test_row_error_goes_to_message_and_other_rows_are_priced(self):
        result = udf.PricingTable([
            _HEADER,
            _row(start="XYZ"),  # 起算日を解釈できない → その行は全部空欄
            _row(pay_rec="XXX", delta=1.0),  # パーレートは出る、PV・adj_notionalは空欄
            _row(),
        ], self.rt)
        self.assertEqual(result[1][:5], [""] * 5)
        self.assertIn("日付として解釈できません", result[1][5])
        self.assertIsInstance(result[2][0], float)
        self.assertEqual(result[2][1:5], ["", "", "", ""])
        self.assertIn("PV/delta", result[2][5])
        self.assertIn("adj_notional", result[2][5])
        self.assertIsInstance(result[3][1], float)
        self.assertEqual(result[3][5], "")

    def test_columns_are_found_by_header(self):
        order = list(reversed(range(len(_HEADER))))
        result = udf.PricingTable(
            [[_HEADER[i] for i in order], [_row(delta=1.0)[i] for i in order]], self.rt,
        )
        self.assertEqual(result, udf.PricingTable([_HEADER, _row(delta=1.0)], self.rt))

    def test_missing_header_is_reported(self):
        with self.assertRaisesRegex(ValueError, "pay/rec"):
            udf.PricingTable([_HEADER[:-1]], self.rt)

    def test_three_evaluations_per_row(self):
        """par・PV・パラレルの3回(1行用のUDFを5つ並べると7回)。"""
        with mock.patch.object(measures, "price", wraps=measures.price) as spy:
            udf.PricingTable([_HEADER, _row(delta=1.0), _row(fix_rate=1.0)], self.rt)
        self.assertEqual(spy.call_count, 6)


if __name__ == "__main__":
    unittest.main()
