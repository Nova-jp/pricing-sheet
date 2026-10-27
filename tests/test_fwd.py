"""
FwdMonitor(engine.fwd)のリグレッションテスト。

実行方法:
    python -m unittest tests.test_fwd -v
    (LSEGのデータには依存しない。日ごとに平行シフトした合成スポットレートでカーブを組み、
     CMTの水準・日次変化が swap_pricer.par_rate と一致すること、FIXED・ROLLINGがHistChartの
     FIXED・ROLLING(engine.swap_pricing.historical.pricer)と一致すること、営業日の移動が
     ql.Calendar と一致すること、UDFの見出しと列の並びを確認する)
"""

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import QuantLib as ql  # noqa: E402

from engine.common import udf_common  # noqa: E402
from engine.common.conventions import CALENDAR, forward_start_date  # noqa: E402
from engine.common.curve import bootstrap_curve, spot_date_for  # noqa: E402
from engine.common.historical_store import HistoricalStore, discount_grid  # noqa: E402
from engine.common.market_data import Market  # noqa: E402
from engine.common.swap_pricer import par_rate, swap_dates  # noqa: E402
from engine.common.terms import STANDARD, SwapTerms  # noqa: E402
from engine.fwd import history, udf  # noqa: E402
from engine.swap_pricing.historical.pricer import SwapSpec, historical_multi_rate_series  # noqa: E402
from tests.test_curve_repricing import SPOT_RATES  # noqa: E402
from tests.test_swaption_forward import BootstrapSource as _BootstrapSource  # noqa: E402

DATES = [date(2026, 9, 24), date(2026, 9, 25), date(2026, 9, 28), date(2026, 9, 29)]  # 営業日(土日をまたぐ)
SHIFT_BP = {d: s for d, s in zip(DATES, [0.0, 3.0, -2.0, 5.0])}
LABELS = ["1y1y", "5y5y"]
BASE = date(2026, 9, 30)  # 基準日(DBの最終日の翌営業日=ライブの日に相当)


def _market(source, as_of_date):
    boot = source.load(as_of_date)
    return Market(as_of_date, boot.curve, boot.index, {})


def _cmt(as_of_date, label):
    start, tail = history.parse_label(label)
    return SwapTerms(forward_start_date(spot_date_for(as_of_date), start), STANDARD, tenor=tail)


class ParseTest(unittest.TestCase):
    def test_label(self):
        self.assertEqual(history.parse_label("5y5y"), ("5Y", "5Y"))
        self.assertEqual(history.parse_label(" 15Y1Y "), ("15Y", "1Y"))
        self.assertEqual(history.parse_label("1Yx2Y"), ("1Y", "2Y"))
        self.assertEqual(history.label_name("10Yx5Y"), "10y5y")

    def test_invalid_label(self):
        for bad in ("5y", "5y5", "y5y", "5d5y", ""):
            with self.assertRaises(ValueError):
                history.parse_label(bad)

    def test_mode(self):
        self.assertEqual(history.parse_mode(" rolling "), history.ROLLING)
        with self.assertRaises(ValueError):
            history.parse_mode("SPOT")


class BusinessDaysTest(unittest.TestCase):
    def test_matches_calendar(self):
        bdays = history._BusinessDays(date(2026, 9, 1), date(2068, 1, 31))
        for start in (date(2026, 9, 18), date(2026, 9, 24), date(2026, 12, 28)):
            ql_start = ql.Date.from_date(start)
            for n in (0, 1, 3, 245, 2450, 9000):
                self.assertEqual(bdays.advance(start, n), CALENDAR.advance(ql_start, n, ql.Days).to_date())
            for end in (date(2027, 1, 4), date(2031, 5, 6), date(2046, 9, 29)):
                self.assertEqual(
                    bdays.between(start, end), CALENDAR.businessDaysBetween(ql_start, ql.Date.from_date(end)),
                )


class ComputeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = _BootstrapSource(SHIFT_BP)
        cls.series = history.compute(cls.source, LABELS, BASE)

    def test_dates(self):
        self.assertEqual(self.series.labels, LABELS)
        self.assertEqual(self.series.dates, DATES[1:])

    def test_cmt_matches_par_rate(self):
        for j, d in enumerate(self.series.dates):
            prev = DATES[j]
            for k, label in enumerate(LABELS):
                today = par_rate(_market(self.source, d), _cmt(d, label)).target_fixrate
                before = par_rate(_market(self.source, prev), _cmt(d, label)).target_fixrate
                self.assertAlmostEqual(self.series.level[history.CMT][j][k], today, places=12)
                self.assertAlmostEqual(self.series.change[history.CMT][j][k], (today - before) * 100.0, places=10)

    def test_fixed_and_rolling_match_histchart(self):
        """FIXED・ROLLINGはHistChartの同名の計算(基準日の取引を固定/営業日数を保って移動)と同じ値。"""
        specs = [SwapSpec(STANDARD, start=e.lower(), tenor=t.lower()) for e, t in map(history.parse_label, LABELS)]
        for mode in (history.FIXED, history.ROLLING):
            expected = dict(historical_multi_rate_series(specs, mode, base_date=BASE, curves=self.source))
            for j, d in enumerate(self.series.dates):
                for k in range(len(LABELS)):
                    self.assertAlmostEqual(self.series.level[mode][j][k], expected[d][k].par_rate, places=12)

    def test_change_is_same_contract(self):
        """FIXEDは毎日同じ取引なので、日次変化=水準の差。ROLLINGはその日の取引を前の日のカーブで評価した差。"""
        fixed = self.series.level[history.FIXED]
        for j in range(1, len(fixed)):
            for k in range(len(LABELS)):
                self.assertAlmostEqual(
                    self.series.change[history.FIXED][j][k], (fixed[j][k] - fixed[j - 1][k]) * 100.0, places=10,
                )
        # 基準日のROLLINGの取引は、FIXEDの取引の(休日調整後の)起算日・満期日そのもの
        contracts = history._Contracts([history.parse_label(x) for x in LABELS], BASE, DATES[0])
        on_base = contracts.on(BASE)
        for rolling, fixed in zip(on_base[history.ROLLING], on_base[history.FIXED]):
            self.assertEqual((rolling.start, rolling.end), swap_dates(fixed))

    def test_base_date_limits_dates(self):
        series = history.compute(self.source, LABELS, DATES[2])
        self.assertEqual(series.dates, DATES[1:3])

    def test_too_few_dates(self):
        series = history.compute(_BootstrapSource({DATES[0]: 0.0}), LABELS, BASE)
        self.assertEqual(series.dates, [])


class FwdUdfTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.store = HistoricalStore(Path(cls.tmp.name) / "historical.db")
        source = _BootstrapSource(SHIFT_BP)
        for d in DATES:
            boot = source.load(d)
            cls.store.save_curve(d, "convex_monotone", "hash", boot.used_tenors, [], discount_grid(boot.curve, d))
        cls.expected = history.compute(source, LABELS, BASE)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_history_layout(self):
        udf._compute_cached.cache_clear()
        with mock.patch.object(udf_common, "_historical_store", return_value=self.store):
            for mode in history.MODES:
                rows = udf.FwdHistory(["1Y1Y", None, "5Yx5Y", ""], mode.lower(), BASE)
                self.assertEqual(rows[0], ["Date", "1y1y", "5y5y", "1y1y dF", "5y5y dF"])
                self.assertEqual([r[0] for r in rows[1:]], DATES[1:])
                for j, row in enumerate(rows[1:]):
                    # DBに保存したカーブ(暦日のDF)から復元しても、その場で組んだカーブと同じ値になる
                    self.assertAlmostEqual(row[2], self.expected.level[mode][j][1], places=10)
                    self.assertAlmostEqual(row[3], self.expected.change[mode][j][0], places=8)
        self.assertEqual(udf._compute_cached.cache_info().misses, 1)  # 3方式で計算は1回

    def test_live(self):
        boot = bootstrap_curve(BASE, SPOT_RATES)
        market = Market(BASE, boot.curve, boot.index, {})
        with mock.patch.object(udf_common, "_market", return_value=SimpleNamespace(base=market)):
            rows = udf.FwdLive([LABELS[0], None, LABELS[1]], [["1", "1Y", 1.0]])
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1], [""])  # 区切りの空行は空欄のまま(行がずれない)
        for row, label in ((rows[0], LABELS[0]), (rows[2], LABELS[1])):
            self.assertAlmostEqual(row[0], par_rate(market, _cmt(BASE, label)).target_fixrate, places=12)

    def test_no_labels(self):
        with self.assertRaises(ValueError):
            udf.FwdHistory([None, ""], "CMT")


if __name__ == "__main__":
    unittest.main()
