"""
カーブ一式の共有(swap_pricing/market.py)、デルタ計算(risk.py)、pricingシート用UDFの
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
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from swap_pricing import curve_batch, market, udf  # noqa: E402
from swap_pricing.conventions import forward_start_date  # noqa: E402
from swap_pricing.curve import bootstrap_curve, spot_date_for  # noqa: E402
from swap_pricing.curve_store import CurveStore  # noqa: E402
from swap_pricing.market import curve_set, realtime_snapshot  # noqa: E402
from swap_pricing.risk import SwapParams, bucketed_delta, parallel_delta, resolve_params  # noqa: E402
from swap_pricing.swap_pricer import price_swap  # noqa: E402
from tests.test_curve_batch import DAYS, FIXINGS, _write_config, _write_workbook  # noqa: E402
from tests.test_curve_repricing import SPOT_RATES  # noqa: E402
from tests.test_past_start import CONV  # noqa: E402

VAL = date(2026, 9, 25)
NOTIONAL = 10_000_000_000
QUOTES = {k.lower(): v for k, v in SPOT_RATES.items()} | {"m1": 0.75}  # m1はカーブに使われない


def _pv(rates, params):
    """以前の実装と同じ手順(その場でブートストラップして評価)。比較の基準。"""
    boot = bootstrap_curve(VAL, rates)
    return price_swap(
        boot.curve, VAL, params.start, params.fix_freq, params.fix_dcf, params.float_freq,
        params.float_dcf, params.roll_conv, params.notional, params.pay_rec, tenor=params.tenor,
        end=params.end, fix_rate=params.fix_rate, index=boot.index,
    ).pv


class CurveSetTest(unittest.TestCase):
    def setUp(self):
        market._cache.clear()
        self.curves = curve_set(realtime_snapshot(VAL, QUOTES))
        spot = self.curves.base.spot_date
        self.params = resolve_params(self.curves, SwapParams(
            start=spot, tenor="7Y", notional=NOTIONAL, pay_rec="PAY", **CONV,
        ))

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
                self.assertAlmostEqual(deltas[label], _pv(bumped, self.params) - base, delta=1e-6)

    def test_bumped_curves_are_built_once_and_shared(self):
        with mock.patch.object(market, "bootstrap_curve", wraps=market.bootstrap_curve) as spy:
            again = curve_set(realtime_snapshot(VAL, dict(QUOTES)))
            self.assertIs(again, self.curves)
            parallel_delta(again, self.params)
            parallel_delta(again, self.params._replace(tenor="10Y"))
            bucketed_delta(again, self.params)
            bucketed_delta(again, self.params._replace(tenor="10Y"))
        self.assertEqual(spy.call_count, 1 + len(self.curves.bump_labels))  # parallel 1 + バケット


def _rt_range(flags=None):
    """RealTimeシート相当の範囲 [フラグ, Ticker, Value]。O/N行・M1行・フラグ0も含める。"""
    flags = flags or {}
    rows = [[None, "O/N", 0.48]]
    rows += [[flags.get(k, 1), k, v] for k, v in SPOT_RATES.items()]
    rows += [[None, "M1", 0.75]]
    return rows


_HEADER = ["flag", "risk_flag", "remarks", "notional", "delta", "adj_notional", "start", "tenor",
           "end", "fix rate", "fix freq", "fix dcf", "index", "float freq", "float dcf", "roll conv",
           "target fixrate", "spred", "fly", "pay/rec"]


def _pricing_row(risk_flag, start, tenor, pay_rec="PAY", notional=NOTIONAL, end=None):
    return [1, risk_flag, "", notional, "", "", start, tenor, end, "", "PA", "act/365fixed", "JSCC",
            "PA", "act/365fixed", "STD", 0.0, "", "", pay_rec]


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

        curves = udf._market(self.rt, None)
        expected = {}
        for row in (self.table[1], self.table[3]):
            params = resolve_params(curves, SwapParams(
                start=row[6], tenor=row[7], notional=row[3], pay_rec=row[19], **CONV,
            ))
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
        delta = udf.SwapDelta(row[6], row[7], None, *[row[i] for i in (10, 11, 13, 14, 15)],
                              row[3], "PAY", "", self.rt)
        curves = udf._market(self.rt, None)
        params = resolve_params(curves, SwapParams(start=row[6], tenor="5y", notional=NOTIONAL,
                                                   pay_rec="PAY", **CONV))
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
        curve_batch.run(xlsx, config, self.db, log=lambda *_: None)
        self.patch = mock.patch.object(udf, "_historical_store", lambda: CurveStore(self.db))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self._tmp.cleanup()

    def test_past_date_uses_db_quotes_with_realtime_flags(self):
        as_of = DAYS[1]
        rt = _rt_range(flags={"40Y": 0})
        quotes = {"O/N": FIXINGS[as_of], **{k: v for k, v in SPOT_RATES.items() if k != "40Y"}}
        boot = bootstrap_curve(as_of, quotes)
        par = udf.FairRate(boot.spot_date, "10y", None, *CONV.values(), rt, as_of)
        expected = price_swap(boot.curve, as_of, boot.spot_date, notional=1.0, pay_rec="PAY",
                              tenor="10y", index=boot.index, **CONV).target_fixrate
        self.assertAlmostEqual(par, expected, delta=1e-12)
        self.assertIn("DB", udf.MarketInfo(rt, as_of))

    def test_date_not_in_db_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "ヒストリカルDBにありません"):
            udf.FairRate(None, "10y", None, *CONV.values(), _rt_range(), date(2026, 9, 26))

    def test_future_date_is_rejected(self):
        future = datetime.date.today() + datetime.timedelta(days=1)
        with self.assertRaisesRegex(ValueError, "未来日"):
            udf.FairRate(None, "10y", None, *CONV.values(), _rt_range(), future)

    def test_fixings_on_or_after_valuation_date_are_not_used(self):
        rows = [[d, r] for d, r in FIXINGS.items()]
        fixings = udf._fixings_from_range(rows, DAYS[1])
        self.assertEqual(max(fixings), DAYS[0])




class TenorStartUdfTest(unittest.TestCase):
    """start列のテナー(10y等)は、評価日のスポット日+tenorの日付を入れたのと同じ。"""

    def setUp(self):
        market._cache.clear()
        self.rt = _rt_range()
        self.spot = udf._market(self.rt, None).base.spot_date
        self.fwd = forward_start_date(self.spot, "10y")

    def test_pricing_udfs(self):
        conv = list(CONV.values())
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
        self.assertEqual(by_tenor, by_date)

class HistoricalLatestRealTimeTest(unittest.TestCase):
    """HistoricalRates: 基準日が今日なら、最新日(今日)はDBの引け値ではなくRealTimeのレートで計算する。"""

    CONV_ROW = ["", "10y", "", *CONV.values()]

    def setUp(self):
        market._cache.clear()
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        xlsx, config, self.db = tmp / "h.xlsx", tmp / "c.toml", tmp / "h.db"
        _write_workbook(xlsx)
        _write_config(config, ["O/N"] + list(SPOT_RATES))
        curve_batch.run(xlsx, config, self.db, log=lambda *_: None)
        self.patches = [mock.patch.object(udf, "_historical_store", lambda: CurveStore(self.db))]
        for p in self.patches:
            p.start()
        # DB(SPOT_RATES)と区別できるよう、RealTimeは全テナー+10bp
        self.rt = [[f, t, v + 0.1 if isinstance(v, float) and t != "M1" else v] for f, t, v in _rt_range()]

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self._tmp.cleanup()

    def _rates(self, today, base_date=None, real_time_range="rt", conventions=None):
        rt = self.rt if real_time_range == "rt" else real_time_range
        with mock.patch.object(udf, "_today", lambda: today):
            return udf.HistoricalRates(conventions or [self.CONV_ROW], "ROLLING", base_date, None, None, rt)

    def _fair_rate(self, today):
        with mock.patch.object(udf, "_today", lambda: today):
            return udf.FairRate(spot_date_for(today), "10y", None, *CONV.values(), self.rt, None)

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

if __name__ == "__main__":
    unittest.main()
