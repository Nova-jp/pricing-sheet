"""
カーブ一式の共有(engine/common/market.py)、デルタ計算(risk.py)、pricingシート用UDFの
評価日の扱い・RiskGrid のリグレッションテスト。

- 共有カーブで計算したデルタが、以前の「毎回ブートストラップし直す」計算と一致すること
- バンプするのはカーブに使っているテナーだけで、ブートストラップは1回だけであること
- 評価日が今日ならRealTime、過去日ならヒストリカルDB(同じフラグでテナーを選ぶ)を使うこと

実行方法:
    python -m unittest tests.test_market_risk -v
"""

import datetime
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from engine.common import morning_batch, market, udf_common  # noqa: E402
from engine.swap_pricing import udf  # noqa: E402
from engine.common.conventions import forward_start_date  # noqa: E402
from engine.common.curve import bootstrap_curve, normalize_tenor, spot_date_for  # noqa: E402
from engine.common.historical_store import HistoricalStore  # noqa: E402
from engine.common.market import curve_set, realtime_snapshot  # noqa: E402
from engine.common.risk import bucketed_delta, bucketed_deltas, parallel_delta, price, resolve_params  # noqa: E402
from engine.common.market_data import Market  # noqa: E402
from engine.common.swap_pricer import price_swap  # noqa: E402
from engine.common.terms import SwapParams, SwapTerms  # noqa: E402
from tests.test_morning_batch import DAYS, FIXINGS, _write_config, _write_workbook  # noqa: E402
from tests.test_curve_repricing import SPOT_RATES  # noqa: E402
from tests.test_past_start import CONV, CONV_ARGS  # noqa: E402

VAL = date(2026, 9, 25)
# バケットデルタはクォートを一時的に動かして基準カーブを引き直す。QuantLibの反復ブートストラップは
# 前回の解を初期値にするので、新しく組んだカーブとはソルバーの収束精度の範囲(想定元本100億円で
# 0.01円程度、相対1e-12)でずれる。比較はこの許容誤差で行う(円)
BOOTSTRAP_TOL_YEN = 0.1
NOTIONAL = 10_000_000_000
QUOTES = {k.lower(): v for k, v in SPOT_RATES.items()} | {"m1": 0.75}  # m1はカーブに使われない


def _pv(rates, params):
    """以前の実装と同じ手順(その場でブートストラップして評価)。比較の基準。"""
    boot = bootstrap_curve(VAL, rates)
    return price_swap(
        Market(VAL, boot.curve, boot.index),
        params.terms, notional=params.notional, pay_rec=params.pay_rec, fix_rate=params.fix_rate,
    ).pv


class CurveSetTest(unittest.TestCase):
    def setUp(self):
        market._cache.clear()
        self.curves = curve_set(realtime_snapshot(VAL, QUOTES))
        spot = self.curves.spot_date
        self.params = resolve_params(self.curves, SwapParams(SwapTerms(spot, CONV, tenor="7Y"), NOTIONAL, "PAY"))

    def test_only_used_tenors_are_bumped(self):
        self.assertNotIn("m1", self.curves.bump_labels)
        self.assertEqual(set(self.curves.bump_labels), set(QUOTES) - {"m1"})

    def test_parallel_delta_equals_rebootstrap(self):
        bumped = {k: v + (0.01 if k in self.curves.bump_labels else 0.0) for k, v in QUOTES.items()}
        expected = _pv(bumped, self.params) - _pv(QUOTES, self.params)
        self.assertAlmostEqual(parallel_delta(self.curves, self.params), expected, delta=1e-6)

    def test_bucketed_delta_equals_rebootstrap(self):
        deltas = bucketed_delta(self.curves, self.params)
        self.assertEqual(set(deltas), set(self.curves.bump_labels))
        base = _pv(QUOTES, self.params)
        for label in ("5y", "7y", "8y", "12m"):
            with self.subTest(label=label):
                bumped = dict(QUOTES, **{label: QUOTES[label] + 0.01})
                self.assertAlmostEqual(deltas[label], _pv(bumped, self.params) - base, delta=BOOTSTRAP_TOL_YEN)

    def test_bumped_curves_are_built_once_and_shared(self):
        """パラレルのカーブは1回だけ作って共有し、バケットデルタはカーブを作らない(クォートを動かす)。"""
        with mock.patch.object(market, "bootstrap_curve", wraps=market.bootstrap_curve) as spy:
            again = curve_set(realtime_snapshot(VAL, dict(QUOTES)))
            self.assertIs(again, self.curves)
            parallel_delta(again, self.params)
            parallel_delta(again, replace(self.params, terms=replace(self.params.terms, tenor="10Y")))
            bucketed_delta(again, self.params)
            bucketed_delta(again, replace(self.params, terms=replace(self.params.terms, tenor="10Y")))
        self.assertEqual(spy.call_count, 1)  # parallel だけ

    def test_bucketed_delta_restores_base_curve(self):
        """バケットデルタのあと、基準カーブのクォートは元の値に戻り、パーレートも変わらない。"""
        before = price(self.curves.base, self.params).target_fixrate
        bucketed_delta(self.curves, self.params)
        for label in self.curves.bump_labels:
            quote = self.curves.base_curve.quotes[normalize_tenor(label)]
            self.assertEqual(quote.value(), QUOTES[label] / 100.0)
        self.assertAlmostEqual(price(self.curves.base, self.params).target_fixrate, before, places=10)  # %(1e-8bp)

    def test_bumped_quote_is_restored_on_error(self):
        quote = self.curves.base_curve.quotes["5y"]
        with self.assertRaises(RuntimeError):
            with self.curves.bumped("5y"):
                self.assertAlmostEqual(quote.value(), (QUOTES["5y"] + 0.01) / 100.0, places=15)
                raise RuntimeError("途中のエラー")
        self.assertEqual(quote.value(), QUOTES["5y"] / 100.0)

    def test_unused_tenor_cannot_be_bumped(self):
        with self.assertRaises(ValueError):
            with self.curves.bumped("m1"):
                pass

    def test_bucketed_deltas_equal_one_by_one(self):
        """複数ポジションをまとめて計算しても、1本ずつ計算したものと同じ。"""
        other = resolve_params(self.curves, replace(self.params, terms=replace(self.params.terms, tenor="10Y")))
        together = bucketed_deltas(self.curves, [self.params, other])
        for params, deltas in zip([self.params, other], together):
            one = bucketed_delta(self.curves, params)
            for label in self.curves.bump_labels:
                self.assertAlmostEqual(deltas[label], one[label], delta=BOOTSTRAP_TOL_YEN)

    def test_only_latest_snapshot_is_cached(self):
        """生きているカーブを増やさないよう、キャッシュは最新のスナップショット1組だけ。"""
        other = curve_set(realtime_snapshot(VAL, dict(QUOTES, **{"5y": QUOTES["5y"] + 0.1})))
        self.assertEqual(list(market._cache.values()), [other])
        self.assertIsNot(curve_set(realtime_snapshot(VAL, dict(QUOTES))), self.curves)


def _rt_range(flags=None):
    """RealTimeシート相当の範囲 [フラグ, Ticker, Value]。O/N行・M1行・フラグ0も含める。"""
    flags = flags or {}
    rows = [[None, "O/N", 0.48]]
    rows += [[flags.get(k, 1), k, v] for k, v in SPOT_RATES.items()]
    rows += [[None, "M1", 0.75]]
    return rows


_HEADER = ["flag", "risk_flag", "remarks", "notional", "delta", "adj_notional", "start", "tenor",
           "end", "fix rate", "fix freq", "fix dcf", "index", "float freq", "roll conv",
           "target fixrate", "spred", "fly", "pay/rec"]


def _pricing_row(risk_flag, start, tenor, pay_rec="PAY", notional=NOTIONAL, end=None):
    return [1, risk_flag, "", notional, "", "", start, tenor, end, "", "PA", "act/365fixed", "JSCC",
            "PA", "STD", 0.0, "", "", pay_rec]


class RiskGridTest(unittest.TestCase):
    def setUp(self):
        market._cache.clear()
        self.today = datetime.date.today()
        self.rt = _rt_range(flags={"40Y": 0})
        self.table = [
            _HEADER,
            _pricing_row(1, self.today, "5y"),
            _pricing_row(0, self.today, "10y"),  # risk_flag=0 は含めない
            _pricing_row(1, self.today, "10y", pay_rec="REC", notional=5_000_000_000),
        ]

    def test_sum_of_flagged_rows_aligned_to_realtime_rows(self):
        grid = udf.RiskGrid(self.table, self.rt)
        self.assertEqual(grid[0], ["Ticker", "Delta"])
        self.assertEqual([r[0] for r in grid[1:]], [r[1] for r in self.rt])
        by_ticker = {r[0]: r[1] for r in grid[1:]}
        self.assertEqual(by_ticker["40Y"], "")  # フラグ0
        self.assertEqual(by_ticker["M1"], "")  # カーブに使っていない

        curves = udf_common._market(self.rt, None)
        expected = {}
        for row in (self.table[1], self.table[3]):
            params = resolve_params(curves, SwapParams(SwapTerms(row[6], CONV, tenor=row[7]), row[3], row[18]))
            for label, value in bucketed_delta(curves, params).items():
                expected[label] = expected.get(label, 0.0) + value / 1_000_000.0
        for ticker, value in by_ticker.items():
            if value != "":
                self.assertAlmostEqual(value, expected[ticker.lower()], delta=1e-9)

    def test_row_error_reports_row_number(self):
        table = self.table + [_pricing_row(1, self.today, "5y", pay_rec="XXX")]
        with self.assertRaisesRegex(ValueError, "5行目"):
            udf.RiskGrid(table, self.rt)

    def test_missing_header_is_reported(self):
        with self.assertRaisesRegex(ValueError, "pay/rec"):
            udf.RiskGrid([_HEADER[:-1]], self.rt)

    def test_swap_delta_matches_sum_of_parallel(self):
        """SwapDelta(行ごとの全テナー+1bp)も共有カーブを使い、再ブートストラップと一致する。"""
        row = self.table[1]
        delta = udf.SwapDelta(row[6], row[7], None, *[row[i] for i in (10, 11, 13, 14)],
                              row[3], "PAY", "", self.rt)
        curves = udf_common._market(self.rt, None)
        params = resolve_params(curves, SwapParams(SwapTerms(row[6], CONV, tenor="5y"), NOTIONAL, "PAY"))
        self.assertAlmostEqual(delta, parallel_delta(curves, params) / 1_000_000.0, delta=1e-12)


class ValuationDateTest(unittest.TestCase):
    """評価日が過去ならヒストリカルDBのその日のレート(RealTimeのフラグで選ぶ)でカーブを組む。"""

    def setUp(self):
        market._cache.clear()
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        xlsx, config, self.db = tmp / "h.xlsx", tmp / "c.toml", tmp / "h.db"
        _write_workbook(xlsx)
        _write_config(config, ["O/N"] + list(SPOT_RATES))
        morning_batch.run(xlsx, config, self.db, log=lambda *_: None)
        self.patch = mock.patch.object(udf_common, "_historical_store", lambda: HistoricalStore(self.db))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self._tmp.cleanup()

    def test_past_date_uses_db_quotes_with_realtime_flags(self):
        as_of = DAYS[1]
        rt = _rt_range(flags={"40Y": 0})
        quotes = {"O/N": FIXINGS[as_of], **{k: v for k, v in SPOT_RATES.items() if k != "40Y"}}
        boot = bootstrap_curve(as_of, quotes)
        par = udf.FairRate(boot.spot_date, "10y", None, *CONV_ARGS, rt, as_of)
        market = Market(as_of, boot.curve, boot.index)
        expected = price_swap(market, SwapTerms(boot.spot_date, CONV, tenor="10y"), notional=1.0, pay_rec="PAY").target_fixrate
        self.assertAlmostEqual(par, expected, delta=1e-12)
        self.assertIn("DB", udf.MarketInfo(rt, as_of))

    def test_date_not_in_db_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "ヒストリカルDBにありません"):
            udf.FairRate(None, "10y", None, *CONV_ARGS, _rt_range(), date(2026, 9, 26))

    def test_future_date_is_rejected(self):
        future = datetime.date.today() + datetime.timedelta(days=1)
        with self.assertRaisesRegex(ValueError, "未来日"):
            udf.FairRate(None, "10y", None, *CONV_ARGS, _rt_range(), future)

    def test_today_past_start_uses_db_fixings(self):
        """評価日=今日でも、過去起算のTONA実績はDBから読む(Excelの範囲は受け取らない)。"""
        today, start = date(2026, 9, 28), date(2026, 7, 1)  # DBの実績は9/25まで
        rt = _rt_range()
        with mock.patch.object(udf_common, "_today", lambda: today):
            par = udf.FairRate(start, "1y", None, *CONV_ARGS, rt)
            curves = udf_common._market(rt, None)
        market = Market(today, curves.base.curve, curves.base.index, FIXINGS)
        expected = price_swap(market, SwapTerms(start, CONV, tenor="1y"), notional=1.0, pay_rec="PAY")
        self.assertEqual(par, expected.target_fixrate)

    def test_without_db_spot_start_works_and_past_start_asks_for_batch(self):
        today = date(2026, 9, 28)
        no_db = mock.patch.object(udf_common, "_historical_store", side_effect=ValueError("DBなし"))
        with mock.patch.object(udf_common, "_today", lambda: today), no_db:
            udf.FairRate(spot_date_for(today), "10y", None, *CONV_ARGS, _rt_range())
            with self.assertRaisesRegex(ValueError, "morning_batch"):
                udf.FairRate(date(2026, 7, 1), "1y", None, *CONV_ARGS, _rt_range())

    def test_fixings_come_from_db_before_valuation_date(self):
        """TONA実績はDBから読み、評価日より前の日だけが市場データに入る(当日以降は使わない)。"""
        curves = udf_common._market(_rt_range(), DAYS[1])
        self.assertEqual(dict(curves.base.fixings), {d: v for d, v in FIXINGS.items() if d < DAYS[1]})




class TenorStartUdfTest(unittest.TestCase):
    """start列のテナー(10y等)は、評価日のスポット日+tenorの日付を入れたのと同じ。"""

    def setUp(self):
        market._cache.clear()
        self.rt = _rt_range()
        self.spot = udf_common._market(self.rt, None).spot_date
        self.fwd = forward_start_date(self.spot, "10y")

    def test_pricing_udfs(self):
        conv = list(CONV_ARGS)
        self.assertEqual(
            udf.FairRate("10y", "20y", None, *conv, self.rt),
            udf.FairRate(self.fwd, "20y", None, *conv, self.rt),
        )
        args = (NOTIONAL, "PAY", None, self.rt)
        self.assertEqual(
            udf.SwapDelta("10Y", "20y", None, *conv, *args),
            udf.SwapDelta(self.fwd, "20y", None, *conv, *args),
        )

    def test_risk_grid(self):
        by_tenor = udf.RiskGrid([_HEADER, _pricing_row(1, "10y", "20y")], self.rt)
        by_date = udf.RiskGrid([_HEADER, _pricing_row(1, self.fwd, "20y")], self.rt)
        self.assertEqual([r[0] for r in by_tenor], [r[0] for r in by_date])
        for a, b in zip(by_tenor[1:], by_date[1:]):
            if a[1] == "":
                self.assertEqual(b[1], "")
            else:
                self.assertAlmostEqual(a[1], b[1], delta=BOOTSTRAP_TOL_YEN / 1_000_000)  # 百万円

class _HistoricalDbCase(unittest.TestCase):
    """朝バッチで作ったヒストリカルDB(SPOT_RATES)と、全テナー+10bpのRealTimeを用意する。"""

    def setUp(self):
        market._cache.clear()
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        xlsx, config, self.db = tmp / "h.xlsx", tmp / "c.toml", tmp / "h.db"
        _write_workbook(xlsx)
        _write_config(config, ["O/N"] + list(SPOT_RATES))
        morning_batch.run(xlsx, config, self.db, log=lambda *_: None)
        self.patches = [mock.patch.object(udf_common, "_historical_store", lambda: HistoricalStore(self.db))]
        for p in self.patches:
            p.start()
        # DB(SPOT_RATES)と区別できるよう、RealTimeは全テナー+10bp
        self.rt = [[f, t, v + 0.1 if isinstance(v, float) and t != "M1" else v] for f, t, v in _rt_range()]

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self._tmp.cleanup()


class HistoricalLatestRealTimeTest(_HistoricalDbCase):
    """HistoricalRates: 基準日が今日なら、最新日(今日)はDBの引け値ではなくRealTimeのレートで計算する。"""

    CONV_ROW = ["", "10y", "", *CONV_ARGS]

    def _rates(self, today, base_date=None, real_time_range="rt", conventions=None):
        rt = self.rt if real_time_range == "rt" else real_time_range
        with mock.patch.object(udf_common, "_today", lambda: today):
            return udf.HistoricalRates(conventions or [self.CONV_ROW], "ROLLING", base_date, None, None, rt)

    def _fair_rate(self, today):
        with mock.patch.object(udf_common, "_today", lambda: today):
            return udf.FairRate(spot_date_for(today), "10y", None, *CONV_ARGS, self.rt, None)

    def test_today_not_in_db_is_added_from_realtime(self):
        today = date(2026, 9, 29)  # 営業日、DBにない
        rows = self._rates(today)
        self.assertEqual([r[0] for r in rows[1:]], DAYS + [today])
        self.assertEqual(rows[-1][1], self._fair_rate(today))

    def test_today_in_db_uses_realtime_instead_of_close(self):
        today = DAYS[-1]  # DBに引け値がある日
        rows = self._rates(today)
        db_rows = self._rates(today, real_time_range=None)
        self.assertEqual([r[0] for r in rows], [r[0] for r in db_rows])
        self.assertEqual(rows[-1][1], self._fair_rate(today))
        self.assertNotAlmostEqual(rows[-1][1], db_rows[-1][1], places=6)
        self.assertEqual(rows[1:-1], db_rows[1:-1])  # 今日より前はDBのまま

    def test_past_base_date_uses_db_only(self):
        rows = self._rates(date(2026, 9, 29), base_date=DAYS[1])
        self.assertEqual([r[0] for r in rows[1:]], DAYS)

    def test_holiday_today_is_not_added(self):
        rows = self._rates(date(2026, 10, 3))  # 土曜
        self.assertEqual([r[0] for r in rows[1:]], DAYS)

    def test_date_axis_includes_today(self):
        """日付軸だけの呼び出し(脚なし)にも今日が入り、カーブは作らない。"""
        with mock.patch.object(market, "bootstrap_curve") as spy:
            rows = self._rates(date(2026, 9, 29), conventions=[[""] * 8] * 3)
        self.assertEqual([r[0] for r in rows[1:]], DAYS + [date(2026, 9, 29)])
        spy.assert_not_called()


class ReleaseCurvesTest(_HistoricalDbCase):
    """ヒストリカル系UDFは、評価日を日付ごとに切り替える前にpricingのカーブを手放す(生きているカーブの
    ヘルパーが評価日の変更のたびに日付を作り直し、ヒストリカルが遅くなるため)。"""

    TODAY = date(2026, 9, 29)  # 営業日、DBにない
    CONV_ROW = ["", "5y", "", *CONV_ARGS]

    def _call(self, fn):
        with mock.patch.object(udf_common, "_today", lambda: self.TODAY):
            udf.FairRate(spot_date_for(self.TODAY), "5y", None, *CONV_ARGS, self.rt, None)
            pricing = list(market._cache.values())
            self.assertEqual(len(pricing), 1)
            fn()
            return pricing[0]

    def test_historical_releases_pricing_curves(self):
        self._call(lambda: udf.HistoricalRates([self.CONV_ROW], "ROLLING", DAYS[-1], None, None, None))
        self.assertEqual(len(market._cache), 0)
        self._call(lambda: udf.HistoricalRate(None, "5y", None, *CONV_ARGS, "ROLLING", DAYS[-1]))
        self.assertEqual(len(market._cache), 0)
        self._call(lambda: udf.HedgeHistoricalRates([["5Y"]], "ROLLING", [[DAYS[0]]], DAYS[-1], None))
        self.assertEqual(len(market._cache), 0)

    def test_date_axis_only_keeps_pricing_curves(self):
        pricing = self._call(lambda: udf.HistoricalRates([[""] * 7] * 3, "ROLLING", None, None, None, self.rt))
        self.assertEqual(list(market._cache.values()), [pricing])
        pricing = self._call(lambda: udf.HedgeHistoricalRates([[""]], "ROLLING", [[DAYS[0]]], None, self.rt))
        self.assertEqual(list(market._cache.values()), [pricing])

    def test_today_row_leaves_the_same_snapshot_as_pricing(self):
        """今日の行(RealTime)で組んだカーブは、pricingと同じスナップショットとして残る(値も同じ)。"""
        pricing = self._call(lambda: udf.HistoricalRates([self.CONV_ROW], "ROLLING", None, None, None, self.rt))
        (after,) = market._cache.values()
        self.assertEqual(after.snapshot, pricing.snapshot)


class HedgeHistoricalRatesUdfTest(_HistoricalDbCase):
    """HedgeHistoricalRates: HistChartの日付軸(降順)どおりに、テナーごとのスポットのパーレートを返す。
    値はHistoricalRates(標準の規約のスポットのスワップ)と同じで、今日の行はRealTimeのレート。"""

    TODAY = date(2026, 9, 29)  # 営業日、DBにない

    def _hedge(self, tickers, dates, rt="rt", mode="ROLLING"):
        rt = self.rt if rt == "rt" else rt
        with mock.patch.object(udf_common, "_today", lambda: self.TODAY):
            return udf.HedgeHistoricalRates([[t] for t in tickers], mode, [[d] for d in dates], None, rt)

    def _outright(self, tenor, mode="ROLLING"):
        with mock.patch.object(udf_common, "_today", lambda: self.TODAY):
            rows = udf.HistoricalRates([["", tenor, "", *CONV_ARGS]], mode, None, None, None, self.rt)
        return {r[0]: r[1] for r in rows[1:]}

    def test_matches_outright_series_in_given_order(self):
        dates = [self.TODAY] + DAYS[::-1]  # HistChart_Dateと同じ降順
        rows = self._hedge(["2Y", "10Y"], dates)
        for col, tenor in enumerate(["2Y", "10Y"]):
            expected = self._outright(tenor)
            self.assertEqual([r[col] for r in rows], [expected[d] for d in dates])

    def test_fixed_mode(self):
        rows = self._hedge(["5Y"], DAYS, mode="FIXED")
        expected = self._outright("5Y", mode="FIXED")
        self.assertEqual([r[0] for r in rows], [expected[d] for d in DAYS])

    def test_tenor_excluded_from_live_curve_is_blank_today(self):
        """RealTimeのフラグ0(今日のカーブに使っていない)テナーは、今日の行だけ空欄。"""
        rt = [[0 if t == "5Y" else f, t, v] for f, t, v in self.rt]
        rows = self._hedge(["5Y"], [self.TODAY, DAYS[-1]], rt=rt)
        self.assertEqual(rows[0], [""])
        self.assertIsInstance(rows[1][0], float)

    def test_blank_tickers_and_unknown_dates(self):
        rows = self._hedge(["", "5Y", None], [DAYS[0], "", date(2026, 1, 3)])
        self.assertIsInstance(rows[0][0], float)
        self.assertEqual(rows[1:], [[""], [""]])
        self.assertEqual(self._hedge([""], DAYS[:2]), [[""], [""]])

if __name__ == "__main__":
    unittest.main()
