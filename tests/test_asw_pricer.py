"""
ASWPricerシートのUDF(AswPricingTable、engine/swap_pricing/asw/table.py・pricer.py)のリグレッションテスト。

- 銘柄からDBの利率・償還日・引け値を引き、単利=引け値+スライド(yield overrideが優先)にすること
- 受渡日(T+1)・MMSの起算日(空欄=スポット)・満期日(償還日、休日調整)の決め方
- MMSは行の規約で組んだOISのパーレート(PricingSheetと同じ経路)、roll conv=BWDは償還日から遡ること
- par-par ASW・adj_notional・bond bpv・hedge ratioの列と、行ごとのエラーをmessageに閉じ込めること
- アンワインド(AswUnwindTable、asw/unwind.py): 約定当初の起算日〜償還日のMMSを約定レートで組んだPV
- 引け(AswCloseTable、asw/close.py): 引け日の受渡・スポットで組んだ複利とMMS(DBの引け日のカーブ)。
  スライド等を使わず、ライブのカーブのキャッシュに触れないこと
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

from engine.common import asw, bond, market, udf_common  # noqa: E402
from engine.common.conventions import CALENDAR, ROLL_BWD, rule_and_eom  # noqa: E402
from engine.common.historical_store import AswRate, HistoricalStore, discount_grid  # noqa: E402
from engine.common.jgb_source import JgbClose, JgbRecord  # noqa: E402
from engine.common.risk import annuity_delta  # noqa: E402
from engine.common.swap_pricer import _build_swap  # noqa: E402
from engine.common.terms import PAY, REC, SwapConvention, SwapParams, SwapTerms  # noqa: E402
from engine.swap_pricing import udf  # noqa: E402
from engine.swap_pricing.asw import close, table, unwind  # noqa: E402
from engine.swap_pricing.pricing import measures  # noqa: E402
from tests.test_market_risk import _rt_range  # noqa: E402

FRIDAY = date(2026, 9, 25)  # 受渡日(T+1)が週末をまたぐ評価日
MATURITY = date(2035, 9, 20)
SUNDAY_MATURITY = date(2054, 12, 20)  # 日曜日の償還日(MMSの満期日は翌営業日)
SA = SwapConvention("SA", "act/365fixed", "SA", ROLL_BWD)
PA = SwapConvention("PA", "act/365fixed", "PA", ROLL_BWD)
_HEADER = list(table.INPUT_COLUMNS)


def _row(bond_code="JB999", slide=None, override=None, delta=None, start=None, conv=SA, pay_rec=None):
    return [bond_code, slide, override, delta, start, conv.fix_freq, conv.fix_dcf, conv.float_freq,
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


class _AswDbCase(unittest.TestCase):
    """銘柄・引け値のDBとFRIDAYのライブのカーブ(AswPricingTable・AswCloseTableのテストで共通)。"""

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


class AswPricingTableTest(_AswDbCase):
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

    def test_bond_bpv_is_repricing_for_one_bp_of_compound_yield(self):
        (r,) = self._price(_row(slide=2.5))
        jgb = bond.ql_bond(bond.Jgb(1.7, MATURITY), "no_leap")
        settle = ql.Date.from_date(r["settle"])
        dirty = lambda y: ql.BondFunctions.cleanPrice(  # noqa: E731
            jgb, ql.InterestRate(y / 100, bond.NO_LEAP, ql.Compounded, ql.Semiannual), settle,
        ) + ql.BondFunctions.accruedAmount(jgb, settle)
        y = r["compound yield"]
        repriced = (dirty(y - 0.01) - dirty(y + 0.01)) / 2 * 1e6  # 額面100円→1億円
        self.assertAlmostEqual(r["bond bpv"] / repriced, 1.0, places=6)

    def test_hedge_ratio_makes_swap_delta_match_bond_bpv(self):
        (r,) = self._price(_row(slide=2.5))
        terms = SwapTerms(self.curves.spot_date, SA, end=MATURITY)
        swap_delta = annuity_delta(measures.par(self.curves, terms).annuity, r["hedge ratio"] * 1e8, PAY)
        self.assertAlmostEqual(swap_delta, r["bond bpv"], places=6)
        self.assertEqual(r["message"], "")

    def test_hedge_ratio_ignores_pay_rec_and_delta_inputs(self):
        base, other = self._price(_row(), _row(delta=-10, pay_rec="REC"))
        self.assertEqual(base["hedge ratio"], other["hedge ratio"])
        self.assertGreater(base["hedge ratio"], 0)

    def test_hedge_ratio_uses_the_row_slide(self):
        rich, cheap = self._price(_row(override=1.0), _row(override=3.0))
        self.assertGreater(rich["bond bpv"], cheap["bond bpv"])  # 利回りが高いほどBPVは小さい

    def test_row_errors_go_to_message(self):
        no_close, unknown, off, past = self._price(
            _row("JN999"), _row("JZ1"), _row(bond_code=None), _row(start=date(2026, 9, 1)),
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


# シートの見出しと同じ表記(末尾の単位は無視して探す)
_UNWIND_HEADER = [
    "maturity", "fix freq", "fix dcf", "float freq", "roll conv", "pay/rec",
    "trade start", "traded MMS rate (%)", "swap notional override (100M)", "swap notional used (100M)",
]
TRADE_START = date(2026, 8, 4)


def _unwind_row(start=TRADE_START, rate=1.2, notional=10.0, pay_rec="PAY", conv=SA, maturity=MATURITY):
    return [maturity, conv.fix_freq, conv.fix_dcf, conv.float_freq, conv.roll_conv, pay_rec,
            start, rate, None, notional]


class AswUnwindTableTest(unittest.TestCase):
    def setUp(self):
        market._cache.clear()
        self.rt = _rt_range()
        # 約定当初(TRADE_START)から評価日前日までのTONA実績
        self.fixings = {date(2026, 8, 1) + timedelta(days=i): 0.48 for i in range((FRIDAY - date(2026, 8, 1)).days)}
        self.patches = [
            mock.patch.object(udf_common, "_db_fixings", lambda: self.fixings),
            mock.patch.object(udf_common, "_today", lambda: FRIDAY),
        ]
        for p in self.patches:
            p.start()
        self.curves = udf_common._market(self.rt, None)

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def _price(self, *rows):
        out = unwind.price_table([_UNWIND_HEADER, *rows], self.curves)
        self.assertEqual(out[0], list(unwind.OUTPUT_HEADER))
        return [dict(zip(unwind.OUTPUT_HEADER, r)) for r in out[1:]]

    def test_pv_is_the_seasoned_mms_at_the_traded_rate(self):
        (r,) = self._price(_unwind_row())
        self.assertEqual(r["unwind message"], "")
        terms = SwapTerms(TRADE_START, SA, end=MATURITY)
        expected = measures.pv(self.curves, SwapParams(terms, 10.0 * 1e8, PAY, 1.2))
        self.assertEqual(r["unwind pv"], expected)
        self.assertEqual((r["unwind start"], r["unwind end"]), (TRADE_START, MATURITY))

    def test_pay_and_rec_are_opposite(self):
        pay, rec = self._price(_unwind_row(), _unwind_row(pay_rec="rec"))
        self.assertAlmostEqual(pay["unwind pv"], -rec["unwind pv"], places=4)

    def test_paying_a_lower_rate_is_worth_more(self):
        low, high = self._price(_unwind_row(rate=1.0), _unwind_row(rate=1.5))
        self.assertGreater(low["unwind pv"], high["unwind pv"])

    def test_spot_start_at_the_mms_rate_is_zero(self):
        terms = SwapTerms(self.curves.spot_date, SA, end=MATURITY)
        par = measures.fair_rate(self.curves, terms)
        (r,) = self._price(_unwind_row(start=self.curves.spot_date, rate=par))
        self.assertAlmostEqual(r["unwind pv"], 0.0, delta=1e-3)

    def test_end_is_adjusted_maturity(self):
        (r,) = self._price(_unwind_row(maturity=SUNDAY_MATURITY))
        self.assertEqual(r["unwind end"], date(2054, 12, 21))

    def test_rows_without_a_trade_are_blank(self):
        rows = self._price(
            _unwind_row(start=None), _unwind_row(rate=None), _unwind_row(notional=None),
        )
        for r in rows:
            self.assertTrue(all(v == "" for v in r.values()))

    def test_row_errors_go_to_message(self):
        no_side, no_fixing, bad_rate = self._price(
            _unwind_row(pay_rec=None), _unwind_row(start=date(2026, 7, 1), conv=PA, maturity=date(2027, 3, 1)), _unwind_row(rate="x"),
        )
        self.assertIn("pay/rec", no_side["unwind message"])
        self.assertEqual(no_side["unwind pv"], "")
        self.assertIn("2026-07", no_fixing["unwind message"])  # 欠けているTONA実績の日付
        self.assertIn("traded MMS rate", bad_rate["unwind message"])

    def test_missing_header_is_an_error(self):
        with self.assertRaises(ValueError):
            unwind.price_table([_UNWIND_HEADER[:-1], _unwind_row()[:-1]], self.curves)

    def test_udf(self):
        out = udf.AswUnwindTable([_UNWIND_HEADER, _unwind_row()], self.rt)
        self.assertEqual(out[1], [*self._price(_unwind_row())[0].values()])


if __name__ == "__main__":
    unittest.main()


SATURDAY = date(2026, 9, 26)
_CLOSE_HEADER = list(close.INPUT_COLUMNS)


def _close_row(bond_code="JB999", conv=SA):
    return [bond_code, conv.fix_freq, conv.fix_dcf, conv.float_freq, conv.roll_conv]


class AswCloseTableTest(_AswDbCase):
    """AswPricingTableTestのDB(銘柄・引け値)に、FRIDAYの引けのカーブ(ライブのカーブと同じもの)を足す。"""

    def setUp(self):
        super().setUp()
        boot = self.curves.base_curve
        self.store.save_curve(FRIDAY, "test", "hash", boot.used_tenors, [], discount_grid(boot.curve, FRIDAY))

    def _close(self, rows, valuation_date=FRIDAY, header=_CLOSE_HEADER):
        with self.store:
            out = close.price_table([header, *rows], valuation_date, self.store)
        self.assertEqual(out[0], list(close.OUTPUT_HEADER))
        return [dict(zip(close.OUTPUT_HEADER, r)) for r in out[1:]]

    def test_close_matches_the_pricer_without_slide(self):
        """引けの日の評価日・スライドなしのAswPricingTableと同じ複利・MMS(カーブはDBの日次のグリッド)。"""
        for conv in (SA, PA):
            (live,) = self._price(_row(conv=conv))
            (r,) = self._close([_close_row(conv=conv)])
            self.assertEqual(r["close message"], "")
            self.assertEqual(r["close compound yield"], live["compound yield"])
            self.assertAlmostEqual(r["close swap rate"], live["swap rate"], places=8)

    def test_slide_and_override_are_ignored(self):
        """AswPricingTableと同じ範囲を渡しても、引けの値はスライド・yield override・startを使わない。"""
        (plain,) = self._close([_close_row()])
        (slid,) = self._close([_row(slide=5, override=2.0, start=date(2026, 10, 1))], header=_HEADER)
        self.assertEqual(slid, plain)

    def test_later_valuation_date_uses_the_close_date(self):
        """評価日が引け日より後なら、引け日の受渡・スポット(週末をまたがない)で計算する。"""
        (friday,) = self._close([_close_row()])
        (saturday,) = self._close([_close_row()], SATURDAY)
        self.assertEqual(saturday, friday)

    def test_close_differs_from_live_when_the_slide_moves_the_bond(self):
        (live,) = self._price(_row(slide=2.0))
        (r,) = self._close([_close_row()])
        self.assertGreater(live["compound yield"], r["close compound yield"])

    def test_live_curve_cache_is_untouched(self):
        cached = dict(market._cache)
        self._close([_close_row()])
        self.assertEqual(market._cache, cached)

    def test_row_errors_go_to_message(self):
        no_close, unknown, off = self._close([_close_row("JN999"), _close_row("JZ1"), _close_row(bond_code="")])
        self.assertIn("引け値", no_close["close message"])
        self.assertIn("JZ1", unknown["close message"])
        self.assertTrue(all(v == "" for v in off.values()))
        # 引け日(9/24)のカーブがDBに無い: 複利は出し、MMSはエラー
        (no_curve,) = self._close([_close_row()], date(2026, 9, 24))
        self.assertNotEqual(no_curve["close compound yield"], "")
        self.assertEqual(no_curve["close swap rate"], "")
        self.assertIn("2026-09-24", no_curve["close message"])

    def test_missing_header_is_an_error(self):
        with self.assertRaises(ValueError):
            self._close([_close_row()[:-1]], header=_CLOSE_HEADER[:-1])

    def test_udf(self):
        with mock.patch.object(udf_common, "_historical_store", lambda: self.store):
            out = udf.AswCloseTable([_CLOSE_HEADER, _close_row()], FRIDAY)
            blank_is_today = udf.AswCloseTable([_CLOSE_HEADER, _close_row()], None)
        self.assertEqual(out[1], [*self._close([_close_row()])[0].values()])
        self.assertEqual(blank_is_today, out)


class AswHistoryBuildTest(_AswDbCase):
    """朝バッチの日次ASW(engine/common/asw.build)。AswCloseTableTestと同じDB(FRIDAYの引けのカーブ)を使う。"""

    def setUp(self):
        super().setUp()
        boot = self.curves.base_curve
        self.store.save_curve(FRIDAY, "test", "hash", boot.used_tenors, [], discount_grid(boot.curve, FRIDAY))

    _close = AswCloseTableTest._close

    def _build(self, **kw):
        logs = []
        failed = asw.build(self.store, log=logs.append, **kw)
        self.assertEqual(failed, {})
        return logs

    def _saved(self, day=FRIDAY):
        with self.store:
            return {code: self.store.asw_quotes_for(code, day, day).get(day) for code in ("009990067", "009990068")}

    def test_matches_close_compound_minus_mms(self):
        """AswCloseTable(引けの複利とMMS)と同じ値。銘柄は bond_code(回号5桁+種別4桁)で保存する。"""
        self._build()
        saved = self._saved()
        for code, bond_code in (("JB999", "009990067"), ("JX999", "009990068")):
            (sa,) = self._close([_close_row(code, SA)])
            (pa,) = self._close([_close_row(code, PA)])
            self.assertAlmostEqual(saved[bond_code].sa, sa["close compound yield"] - sa["close swap rate"], places=10)
            self.assertAlmostEqual(saved[bond_code].pa, pa["close compound yield"] - pa["close swap rate"], places=10)
        self.assertNotAlmostEqual(saved["009990067"].pa, saved["009990067"].sa, places=4)

    def test_only_dates_with_a_curve_and_closes(self):
        """9/24は引け値はあるがカーブが無い、9/28はカーブ・引け値とも無い(DBの外の日付)→ 計算しない。"""
        self._build()
        with self.store:
            self.assertEqual(self.store.asw_input_hashes().keys(), {FRIDAY})
            self.assertEqual(self.store.asw_quotes_for("009990067", date(2026, 9, 24), date(2026, 9, 24)), {})

    def test_unchanged_inputs_are_not_recomputed(self):
        self._build()
        with mock.patch.object(asw, "compute_day", side_effect=AssertionError("計算し直した")):
            logs = self._build()
        self.assertIn("変更なし 1日", logs[0])
        # 引け値が変われば計算し直す
        self.store.save_jgb_closes([JgbClose(FRIDAY, "JB999", 1.710)])
        before = self._saved()["009990067"]
        self._build()
        self.assertGreater(self._saved()["009990067"].pa, before.pa)  # 単利が上がれば複利も上がり、ASWも上がる

    def test_rebuild_recomputes(self):
        self._build()
        with mock.patch.object(asw, "compute_day", wraps=asw.compute_day) as spy:
            self._build(rebuild=True)
        self.assertEqual(spy.call_count, 1)

    def test_old_rows_are_removed(self):
        """以前Neonから取得した値(計算していない日・先頭の0が落ちた銘柄)は残さない。"""
        self.store.save_asw_day(date(2024, 1, 4), {"003800067": AswRate(0.1, 0.2)}, "neon")
        self.store.save_asw_day(FRIDAY, {"3800067": AswRate(0.1, 0.2), "009990067": AswRate(9.9, 9.9)}, "neon")
        logs = self._build()
        self.assertIn("古い行を削除 1日", logs[0])
        saved = self._saved()
        self.assertEqual(self.store.asw_quotes(), {FRIDAY: saved})
        self.assertNotEqual(saved["009990067"], AswRate(9.9, 9.9))

    def test_bonds_near_maturity_and_unknown_bonds(self):
        """MMSの起算日(T+2)が償還日以降の銘柄は計算しない。銘柄情報の無い銘柄はログに出す。"""
        self.store.save_jgb_bonds([JgbRecord("JN998", "JP09980042=", "WW", 0.1, date(2026, 9, 29))])
        self.store.save_jgb_closes([JgbClose(FRIDAY, "JN998", 0.5), JgbClose(FRIDAY, "JZ001", 0.5)])
        logs = self._build()
        with self.store:
            self.assertEqual(self.store.asw_quotes_for("009980042"), {})
        self.assertTrue(any("JZ001" in line for line in logs))
        self.assertFalse(any("JN998" in line for line in logs))


class AswBondCodeTest(unittest.TestCase):
    def test_bond_code_from_ric(self):
        self.assertEqual(asw.bond_code("JP03800067="), "003800067")
        self.assertEqual(asw.bond_code("JP01900069="), "001900069")
        with self.assertRaises(ValueError):
            asw.bond_code("JP0380006=")


if __name__ == "__main__":
    unittest.main()
