"""
ASWPricerシートのUDF(AswPricingTable、engine/swap_pricing/asw/table.py・pricer.py)のリグレッションテスト。

- 銘柄からDBの利率・償還日・引け値を引き、単利=引け値+スライド(yield overrideが優先)にすること
- 受渡日(T+1)・MMSの起算日(空欄=スポット)・満期日(償還日、休日調整)の決め方
- MMSは行の規約で組んだOISのパーレート(PricingSheetと同じ経路)、roll conv=BWDは償還日から遡ること
- par-par ASW・adj_notionalの列と、行ごとのエラーをmessageに閉じ込めること
値そのもの(複利・パーレート・スプレッド)はQuantLibを正とし、手計算の照合はしない。

実行方法:
    python -m unittest tests.test_asw_pricer -v
"""

import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import QuantLib as ql

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from engine.common import bond, market, udf_common  # noqa: E402
from engine.common.conventions import CALENDAR, ROLL_BWD, rule_and_eom  # noqa: E402
from engine.common.historical_store import HistoricalStore  # noqa: E402
from engine.common.jgb_source import JgbClose, JgbRecord  # noqa: E402
from engine.common.swap_pricer import _build_swap  # noqa: E402
from engine.common.terms import PAY, SwapConvention, SwapTerms  # noqa: E402
from engine.swap_pricing import udf  # noqa: E402
from engine.swap_pricing.asw import table  # noqa: E402
from engine.swap_pricing.pricing import measures  # noqa: E402
from tests.test_market_risk import _rt_range  # noqa: E402

FRIDAY = date(2026, 9, 25)  # 受渡日(T+1)が週末をまたぐ評価日
MATURITY = date(2035, 9, 20)
SUNDAY_MATURITY = date(2054, 12, 20)  # 日曜日の償還日(MMSの満期日は翌営業日)
SA = SwapConvention("SA", "act/365fixed", "SA", ROLL_BWD)
PA = SwapConvention("PA", "act/365fixed", "PA", ROLL_BWD)
_HEADER = list(table.INPUT_COLUMNS)


def _row(bond_code="JB999", slide=None, override=None, delta=None, start=None, conv=SA, pay_rec=None, flag=1):
    return [flag, bond_code, slide, override, delta, start, conv.fix_freq, conv.fix_dcf, conv.float_freq,
            conv.roll_conv, pay_rec]


class BackwardRollTest(unittest.TestCase):
    def test_bwd_is_backward_and_other_rolls_unchanged(self):
        self.assertEqual(rule_and_eom(ROLL_BWD), (ql.DateGeneration.Backward, False))
        self.assertEqual(rule_and_eom("STD"), (ql.DateGeneration.Forward, False))

    def test_bwd_schedule_rolls_back_from_maturity(self):
        """支払日が償還日から半年ごと(休日調整後)にそろい、端数は先頭(短いフロントスタブ)。"""
        start = date(2026, 9, 29)
        swap = _build_swap(ql.Tonar(), SwapTerms(start, SA, end=MATURITY), 1.0, PAY, 0.0)
        dates = [d.to_date() for d in swap.fixedSchedule().dates()]
        expected = [
            CALENDAR.adjust(ql.Date.from_date(MATURITY) - ql.Period(6 * k, ql.Months), ql.ModifiedFollowing).to_date()
            for k in range(len(dates) - 2, -1, -1)
        ]
        self.assertEqual(dates[0], start)
        self.assertEqual(dates[1:], expected)
        self.assertEqual(dates[1], date(2027, 3, 23))  # 3/20(土)・3/21(日、春分の日)・3/22(振替休日)の翌営業日


class AswPricingTableTest(unittest.TestCase):
    def setUp(self):
        market._cache.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = HistoricalStore(Path(self.tmp.name) / "historical.db")
        self.store.save_jgb_bonds([
            JgbRecord("JB999", "JP09990067=", "XX", 1.7, MATURITY),
            JgbRecord("JX999", "JP09990068=", "YY", 2.3, SUNDAY_MATURITY),
            JgbRecord("JN999", "JP09990042=", "ZZ", 0.5, date(2028, 3, 1)),  # 引け値なし
        ])
        self.store.save_jgb_closes([
            JgbClose(date(2026, 9, 24), "JB999", 1.650),
            JgbClose(FRIDAY, "JB999", 1.700),
            JgbClose(date(2026, 9, 28), "JB999", 9.999),  # 評価日より後は使わない
            JgbClose(FRIDAY, "JX999", 2.500),
        ])
        self.rt = _rt_range()
        # 過去起算のMMS用のTONA実績(ローカルのDBに依存しない)
        fixings = {date(2026, 8, 1) + timedelta(days=i): 0.48 for i in range((FRIDAY - date(2026, 8, 1)).days)}
        with mock.patch.object(udf_common, "_db_fixings", lambda: fixings),                 mock.patch.object(udf_common, "_today", lambda: FRIDAY):
            self.curves = udf_common._market(self.rt, None)
        self.today = mock.patch.object(udf_common, "_today", lambda: FRIDAY)
        self.today.start()

    def tearDown(self):
        self.today.stop()
        self.tmp.cleanup()

    def _price(self, *rows):
        with self.store:
            out = table.price_table([_HEADER, *rows], self.curves, self.store)
        self.assertEqual(out[0], list(table.OUTPUT_HEADER))
        return [dict(zip(table.OUTPUT_HEADER, r)) for r in out[1:]]

    def test_bond_dates_and_yields(self):
        (r,) = self._price(_row(slide=2.5))
        self.assertEqual(r["message"], "")
        self.assertEqual((r["coupon"], r["maturity"]), (1.7, MATURITY))
        self.assertEqual((r["close"], r["close date"]), (1.700, FRIDAY))
        self.assertAlmostEqual(r["simple yield"], 1.725, places=12)
        self.assertEqual(r["settle"], date(2026, 9, 28))  # 金曜のT+1は月曜
        jgb = bond.Jgb(1.7, MATURITY)
        self.assertEqual(r["price"], bond.price_from_simple_yield(jgb, r["simple yield"], r["settle"]))
        self.assertEqual(
            r["compound yield"],
            bond.compound_yield(jgb, r["price"] + bond.accrued_interest(jgb, r["settle"]), r["settle"]),
        )

    def test_swap_rate_is_mms_from_spot_to_maturity(self):
        sa, pa = self._price(_row(), _row(conv=PA))
        self.assertEqual(sa["start date"], self.curves.spot_date)
        self.assertEqual(sa["end date"], MATURITY)
        self.assertEqual(sa["swap rate"], measures.fair_rate(self.curves, SwapTerms(self.curves.spot_date, SA, end=MATURITY)))
        self.assertEqual(pa["swap rate"], measures.fair_rate(self.curves, SwapTerms(self.curves.spot_date, PA, end=MATURITY)))
        self.assertNotEqual(sa["swap rate"], pa["swap rate"])
        self.assertEqual(sa["compound yield"], pa["compound yield"])  # 現物の複利は規約によらない

    def test_end_date_is_adjusted_maturity(self):
        (r,) = self._price(_row("JX999"))
        self.assertEqual(r["maturity"], SUNDAY_MATURITY)
        self.assertEqual(r["end date"], date(2054, 12, 21))

    def test_start_override(self):
        (r,) = self._price(_row(start=date(2026, 10, 20)))
        self.assertEqual(r["start date"], date(2026, 10, 20))

    def test_override_wins_and_code_is_case_insensitive(self):
        (r,) = self._price(_row("jb999", slide=10, override=1.8))
        self.assertEqual(r["simple yield"], 1.8)

    def test_par_par_rises_as_bond_cheapens(self):
        rich, cheap = self._price(_row(override=1.6), _row(override=1.9))
        self.assertEqual((rich["message"], cheap["message"]), ("", ""))
        self.assertGreater(cheap["par-par asw"], rich["par-par asw"])

    def test_adj_notional(self):
        (r,) = self._price(_row(delta=-10, pay_rec="PAY"))
        terms = SwapTerms(self.curves.spot_date, SA, end=MATURITY)
        self.assertEqual(r["adj_notional"], measures.notional_for_delta(self.curves, terms, -10, "PAY"))
        (blank,) = self._price(_row())
        self.assertEqual(blank["adj_notional"], "")

    def test_row_errors_go_to_message(self):
        no_close, unknown, off, past = self._price(
            _row("JN999"), _row("JZ1"), _row(flag=0), _row(start=date(2026, 9, 1)),
        )
        self.assertIn("引け値", no_close["message"])
        self.assertEqual(no_close["coupon"], 0.5)  # 引けるところまでは出す
        self.assertIn("JZ1", unknown["message"])
        self.assertTrue(all(v == "" for v in off.values()))
        # 過去起算はpar-parだけ計算しない(MMSはDBのTONA実績で計算する)
        self.assertNotEqual(past["swap rate"], "")
        self.assertEqual(past["par-par asw"], "")
        self.assertIn("par-par asw", past["message"])

    def test_past_start_skips_only_par_par(self):
        (r,) = self._price(_row(start=FRIDAY))  # 評価日=起算日は計算する
        self.assertNotEqual(r["par-par asw"], "")

    def test_udf_reads_db_and_market(self):
        with mock.patch.object(udf_common, "_historical_store", lambda: self.store):
            out = udf.AswPricingTable([_HEADER, _row()], self.rt)
        self.assertEqual(out[1], [*self._price(_row())[0].values()])


if __name__ == "__main__":
    unittest.main()
