"""
スワップションの原資産のFwd(engine/common/swaption_forward.py、DBの swaption_forwards)のテスト。

実行方法:
    python -m unittest tests.test_swaption_forward -v
    (LSEGのデータには依存しない。日ごとに平行シフトした合成スポットレートでカーブを組み、
     Fwd・前の日付のカーブでのFwdが swap_pricer.par_rate と、年金がvolの換算(PremiumConverter)と一致すること、
     ロールダウンが混ざらないこと、入力が変わった日だけ計算し直すことを確認する)
"""

import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import QuantLib as ql  # noqa: E402

from engine.common import swaption_forward  # noqa: E402
from engine.common.curve import bootstrap_curve  # noqa: E402
from engine.common.historical_store import (  # noqa: E402
    GRID_DAYS, HistoricalStore, StoredCurve, _grid_dates, discount_grid,
)
from engine.common.market_data import Market  # noqa: E402
from engine.common.swap_pricer import par_rate  # noqa: E402
from engine.common.swaption import PremiumConverter, underlying  # noqa: E402
from tests.test_curve_repricing import SPOT_RATES  # noqa: E402

DATES = [date(2026, 9, 24), date(2026, 9, 25), date(2026, 9, 28), date(2026, 9, 29)]  # 営業日(土日をまたぐ)
SHIFT_BP = {d: s for d, s in zip(DATES, [0.0, 3.0, -2.0, 5.0])}
LABELS = ["1Mx10Y", "1Yx2Y"]


def _rates(d, shift_bp):
    return {k: v + shift_bp / 100.0 for k, v in SPOT_RATES.items()}


class BootstrapSource:
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


def par(source, as_of_date, label, contract_date):
    """contract_date の契約(swaption.underlying)を、as_of_date のカーブで評価したパーレート(%)。"""
    boot = source.load(as_of_date)
    terms = underlying(contract_date, label).terms
    return par_rate(Market(as_of_date, boot.curve, boot.index, {}), terms).target_fixrate


def forwards_from(source, labels):
    """BootstrapSource の全日付の swaption_forwards({日付: {銘柄: 行}})。"""
    dates = source.dates()
    return {
        d: {r.label: r for r in swaption_forward.compute_day(source, d, dates[i - 1] if i else None, labels)}
        for i, d in enumerate(dates)
    }


def save_curves(store, source, input_hash="hash"):
    for d in source.dates():
        boot = source.load(d)
        store.save_curve(d, "convex_monotone", f"{input_hash}-{d}", boot.used_tenors, [], discount_grid(boot.curve, d))


class ComputeDayTest(unittest.TestCase):
    def test_matches_par_rate(self):
        source = BootstrapSource(SHIFT_BP)
        rows = swaption_forward.compute_day(source, DATES[1], DATES[0], LABELS)
        self.assertEqual([r.label for r in rows], LABELS)
        for r in rows:
            u = underlying(DATES[1], r.label)
            self.assertEqual((r.expiry_date, r.start_date, r.days), (u.expiry, u.terms.start, u.days))
            self.assertGreater(r.end_date, r.start_date)
            self.assertAlmostEqual(r.forward, par(source, DATES[1], r.label, DATES[1]), places=12)
            self.assertAlmostEqual(r.forward_prev, par(source, DATES[0], r.label, DATES[1]), places=12)

    def test_same_contract_as_vol_conversion(self):
        """Fwd・年金は、プレミアム→volの換算(朝バッチの⑦)と同じ契約・同じ値。"""
        source = BootstrapSource(SHIFT_BP)
        rows = swaption_forward.compute_day(source, DATES[2], DATES[1], LABELS)
        ql.Settings.instance().evaluationDate = ql.Date.from_date(DATES[2])
        conv = PremiumConverter(source.load(DATES[2]).curve, DATES[2])
        for r in rows:
            v = conv.convert(r.label, 100.0)
            self.assertEqual((v.expiry, v.start, v.days), (r.expiry_date, r.start_date, r.days))
            self.assertAlmostEqual(r.forward, v.forward, places=12)
            self.assertAlmostEqual(r.annuity, v.annuity, places=12)

    def test_first_date_has_no_prev(self):
        rows = swaption_forward.compute_day(BootstrapSource(SHIFT_BP), DATES[0], None, LABELS)
        self.assertTrue(all(r.forward_prev is None for r in rows))

    def test_no_rolldown(self):
        """日付→DFが毎日同じなら、同じ契約を前の日付のカーブで評価しても同じ値(差にロールダウンが入らない)。"""
        boot = bootstrap_curve(DATES[0], SPOT_RATES)
        fixed = ql.DiscountCurve(
            _grid_dates(DATES[0], GRID_DAYS), list(discount_grid(boot.curve, DATES[0])), ql.Actual365Fixed(),
        )
        handle = ql.YieldTermStructureHandle(fixed)
        source = mock.Mock()
        source.load.return_value = StoredCurve(handle, ql.Tonar(handle))
        first = swaption_forward.compute_day(source, DATES[1], DATES[0], ["1Yx2Y"])[0]
        last = swaption_forward.compute_day(source, DATES[3], DATES[2], ["1Yx2Y"])[0]
        self.assertNotAlmostEqual(first.forward, last.forward, places=6)  # 契約が変わればFwdは動く
        self.assertAlmostEqual(last.forward, last.forward_prev, places=12)


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "historical.db"
        self.store = HistoricalStore(self.db)
        self.source = BootstrapSource(SHIFT_BP)
        save_curves(self.store, self.source)

    def tearDown(self):
        self.tmp.cleanup()

    def _build(self, **kwargs):
        built = []
        real = swaption_forward.compute_day

        def spy(curves, d, prev, labels):
            built.append(d)
            return real(curves, d, prev, labels)

        with mock.patch.object(swaption_forward, "compute_day", side_effect=spy):
            failed = swaption_forward.build(self.store, LABELS, log=lambda *_: None, **kwargs)
        self.assertEqual(failed, {})
        return built

    def test_saves_all_dates(self):
        self.assertEqual(self._build(), DATES)
        saved = self.store.swaption_forwards(LABELS)
        expected = forwards_from(self.source, LABELS)
        self.assertEqual(sorted(saved), DATES)
        for d in DATES:
            for label in LABELS:
                a, b = saved[d][label], expected[d][label]
                self.assertEqual(a[:5], b[:5])
                self.assertAlmostEqual(a.forward, b.forward, places=10)  # DBの暦日のDFから復元しても同じ値
                self.assertAlmostEqual(a.annuity, b.annuity, places=10)
                if b.forward_prev is None:
                    self.assertIsNone(a.forward_prev)
                else:
                    self.assertAlmostEqual(a.forward_prev, b.forward_prev, places=10)
        self.assertEqual(self.store.swaption_forwards(["1Yx2Y"])[DATES[0]].keys(), {"1Yx2Y"})

    def test_only_changed_days_are_rebuilt(self):
        self._build()
        self.assertEqual(self._build(), [])
        # 3日目のカーブを作り直すと、その日と次の日(forward_prev が変わる)だけ計算し直す
        boot = self.source.load(DATES[2])
        self.store.save_curve(DATES[2], "convex_monotone", "changed", boot.used_tenors, [],
                              discount_grid(boot.curve, DATES[2]))
        self.assertEqual(self._build(), DATES[2:])
        self.assertEqual(self._build(rebuild=True), DATES)

    def test_rows_of_dates_without_curves_are_removed(self):
        self._build()
        conn = sqlite3.connect(self.db)
        with conn:
            conn.execute("DELETE FROM curves WHERE as_of_date = ?", (DATES[-1].isoformat(),))
        conn.close()
        self._build()
        self.assertEqual(sorted(self.store.swaption_forwards(LABELS)), DATES[:-1])
        self.assertEqual(sorted(self.store.swaption_forward_hashes()), DATES[:-1])


if __name__ == "__main__":
    unittest.main()
