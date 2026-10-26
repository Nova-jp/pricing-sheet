"""
IV vs HV(engine.vol)のリグレッションテスト。

実行方法:
    python -m unittest tests.test_vol -v
    (LSEGのデータには依存しない。日ごとに平行シフトした合成スポットレートでカーブを組み、
     Fwdの水準・日次変化が swap_pricer.par_rate と一致すること、ロールダウンが混ざらないこと、
     IVの突き合わせ、UDFの見出しと列の並びを確認する)
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
from engine.common.conventions import forward_start_date  # noqa: E402
from engine.common.curve import bootstrap_curve, spot_date_for  # noqa: E402
from engine.common.historical_store import (  # noqa: E402
    GRID_DAYS, HistoricalStore, StoredCurve, _grid_dates, discount_grid,
)
from engine.common.market_data import Market  # noqa: E402
from engine.common.swap_pricer import par_rate  # noqa: E402
from engine.common.terms import STANDARD, SwapTerms  # noqa: E402
from engine.vol import hv, udf  # noqa: E402
from tests.test_curve_repricing import SPOT_RATES  # noqa: E402

DATES = [date(2026, 9, 24), date(2026, 9, 25), date(2026, 9, 28), date(2026, 9, 29)]  # 営業日(土日をまたぐ)
SHIFT_BP = {d: s for d, s in zip(DATES, [0.0, 3.0, -2.0, 5.0])}
LABELS = ["1Mx10Y", "1Yx2Y"]


def _rates(d, shift_bp):
    return {k: v + shift_bp / 100.0 for k, v in SPOT_RATES.items()}


class _BootstrapSource:
    """テスト用のカーブ供給元(日付ごとに平行シフトした合成レートでその場でブートストラップする)。"""

    def __init__(self, shifts):
        self.shifts = shifts
        self._curves = {}

    def dates(self):
        return sorted(self.shifts)

    def load(self, as_of_date, until=None):
        if as_of_date not in self._curves:
            self._curves[as_of_date] = bootstrap_curve(as_of_date, _rates(as_of_date, self.shifts[as_of_date]))
        return self._curves[as_of_date]


def _par(source, as_of_date, label, contract_date):
    """contract_dateの契約(スポット日+満期スタート)を、as_of_dateのカーブで評価したパーレート(%)。"""
    expiry, tail = hv.parse_label(label)
    terms = SwapTerms(start=forward_start_date(spot_date_for(contract_date), expiry), convention=STANDARD, tenor=tail)
    boot = source.load(as_of_date)
    return par_rate(Market(as_of_date, boot.curve, boot.index, {}), terms).target_fixrate


class ParseLabelTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(hv.parse_label("1Yx10Y"), ("1Y", "10Y"))
        self.assertEqual(hv.parse_label(" 3mX5y "), ("3M", "5Y"))

    def test_invalid(self):
        for bad in ("1Y10Y", "1Yx", "10Y", "1Dx10Y"):
            with self.assertRaises(ValueError):
                hv.parse_label(bad)


class ComputeTest(unittest.TestCase):
    def test_matches_par_rate(self):
        source = _BootstrapSource(SHIFT_BP)
        series = hv.compute(source, {}, LABELS)
        self.assertEqual(series.dates, DATES[1:])
        for j, d in enumerate(series.dates):
            prev = DATES[j]
            for k, label in enumerate(LABELS):
                today = _par(source, d, label, d)
                self.assertAlmostEqual(series.fwd[j][k], today, places=12)
                self.assertAlmostEqual(series.dfwd[j][k], (today - _par(source, prev, label, d)) * 100.0, places=10)

    def test_no_rolldown(self):
        """日付→DFが毎日同じなら、Fwdの水準は日がたつと動くが、日次変化は0(同じ契約どうしで比べる)。"""
        boot = bootstrap_curve(DATES[0], SPOT_RATES)
        fixed = ql.DiscountCurve(  # 最初の日のカーブを、日付→DFのまま全日付で使う
            _grid_dates(DATES[0], GRID_DAYS), list(discount_grid(boot.curve, DATES[0])), ql.Actual365Fixed(),
        )
        handle = ql.YieldTermStructureHandle(fixed)
        source = mock.Mock()
        source.dates.return_value = DATES
        source.load.return_value = StoredCurve(handle, ql.Tonar(handle))
        series = hv.compute(source, {}, ["1Yx2Y"])
        self.assertNotAlmostEqual(series.fwd[0][0], series.fwd[-1][0], places=6)
        for row in series.dfwd:
            self.assertAlmostEqual(row[0], 0.0, places=10)

    def test_iv_alignment(self):
        quotes = {DATES[1]: {"1MX10Y": 60.0, "1Yx2Y": 50.0}, DATES[3]: {"1mx10y": 62.0}}
        series = hv.compute(_BootstrapSource(SHIFT_BP), quotes, ["1mx10y", "1Yx2Y"])
        self.assertEqual(series.labels, ["1Mx10Y", "1Yx2Y"])
        self.assertEqual(series.iv, [[60.0, 50.0], [None, None], [62.0, None]])

    def test_too_few_dates(self):
        series = hv.compute(_BootstrapSource({DATES[0]: 0.0}), {}, LABELS)
        self.assertEqual(series.dates, [])


class VolHistoryUdfTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.store = HistoricalStore(Path(cls.tmp.name) / "historical.db")
        source = _BootstrapSource(SHIFT_BP)
        for d in DATES:
            boot = source.load(d)
            cls.store.save_curve(d, "convex_monotone", "hash", boot.used_tenors, [], discount_grid(boot.curve, d))
        cls.store.save_vol_quotes({DATES[2]: {"1Mx10Y": 61.5}})
        cls.expected = hv.compute(source, cls.store.vol_quotes(), LABELS)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_layout(self):
        udf._compute_cached.cache_clear()
        with mock.patch.object(udf_common, "_historical_store", return_value=self.store):
            rows = udf.VolHistory(LABELS + [None, ""])
        self.assertEqual(rows[0], [
            "Date", "1Mx10Y IV", "1Mx10Y Fwd", "1Mx10Y dF", "1Yx2Y IV", "1Yx2Y Fwd", "1Yx2Y dF",
        ])
        self.assertEqual([r[0] for r in rows[1:]], DATES[1:])
        self.assertEqual(rows[2][1], 61.5)
        self.assertEqual(rows[1][1], "")
        for j, row in enumerate(rows[1:]):
            # DBに保存したカーブ(暦日のDF)から復元しても、その場で組んだカーブと同じ値になる
            self.assertAlmostEqual(row[2], self.expected.fwd[j][0], places=10)
            self.assertAlmostEqual(row[6], self.expected.dfwd[j][1], places=8)

    def test_no_labels(self):
        with self.assertRaises(ValueError):
            udf.VolHistory([None, ""])


class VolFwdLiveUdfTest(unittest.TestCase):
    def test_matches_par_rate(self):
        base = DATES[-1]
        boot = bootstrap_curve(base, SPOT_RATES)
        market = Market(base, boot.curve, boot.index, {})
        with mock.patch.object(udf_common, "_market", return_value=SimpleNamespace(base=market)):
            rows = udf.VolFwdLive([LABELS[0], None, LABELS[1]], [["1", "1Y", 1.0]])
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1], [""])  # 空欄の行は空欄のまま(行がずれない)
        spot = spot_date_for(base)
        for row, (e, t) in ((rows[0], ("1M", "10Y")), (rows[2], ("1Y", "2Y"))):
            terms = SwapTerms(forward_start_date(spot, e), STANDARD, tenor=t)
            self.assertAlmostEqual(row[0], par_rate(market, terms).target_fixrate, places=12)

    def test_no_labels(self):
        with self.assertRaises(ValueError):
            udf.VolFwdLive([None, ""], [["1", "1Y", 1.0]])


if __name__ == "__main__":
    unittest.main()
