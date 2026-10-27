"""
IV vs HV・VolLevel(engine.vol)のリグレッションテスト。

実行方法:
    python -m unittest tests.test_vol -v
    (LSEGのデータには依存しない。日ごとに平行シフトした合成スポットレートでカーブを組み、
     DBの原資産のFwd(swaption_forwards)から作る系列のFwdの水準・日次変化が swap_pricer.par_rate と
     一致すること、IVの突き合わせ、格子の外の銘柄のエラー、UDFの見出しと列の並び、ライブのFwdを確認する。
     原資産のFwdの計算そのものは tests/test_swaption_forward.py)
"""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from engine.common import swaption_forward, udf_common  # noqa: E402
from engine.common.curve import bootstrap_curve  # noqa: E402
from engine.common.historical_store import HistoricalStore  # noqa: E402
from engine.common.market_data import Market  # noqa: E402
from engine.common.swap_pricer import par_rate  # noqa: E402
from engine.common.swaption import underlying  # noqa: E402
from engine.vol import hv, udf  # noqa: E402
from tests.test_curve_repricing import SPOT_RATES  # noqa: E402
from tests.test_swaption_forward import (  # noqa: E402
    DATES, LABELS, SHIFT_BP, BootstrapSource, forwards_from, par, save_curves,
)


class ParseLabelTest(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(hv.normalize("1Yx10Y"), "1Yx10Y")
        self.assertEqual(hv.normalize(" 3mX5y "), "3Mx5Y")

    def test_invalid(self):
        for bad in ("1Y10Y", "1Yx", "10Y", "1Dx10Y"):
            with self.assertRaises(ValueError):
                hv.normalize(bad)


class ComputeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = BootstrapSource(SHIFT_BP)
        cls.forwards = forwards_from(cls.source, LABELS)

    def test_matches_par_rate(self):
        series = hv.compute(self.forwards, {}, LABELS)
        self.assertEqual(series.dates, DATES[1:])  # 最初の日付は前の日付が無いので除く
        for j, d in enumerate(series.dates):
            prev = DATES[j]
            for k, label in enumerate(LABELS):
                today = par(self.source, d, label, d)
                self.assertAlmostEqual(series.fwd[j][k], today, places=12)
                self.assertAlmostEqual(
                    series.dfwd[j][k], (today - par(self.source, prev, label, d)) * 100.0, places=10,
                )

    def test_iv_alignment(self):
        quotes = {DATES[1]: {"1MX10Y": 60.0, "1Yx2Y": 50.0}, DATES[3]: {"1mx10y": 62.0}}
        series = hv.compute(self.forwards, quotes, ["1mx10y", "1Yx2Y"])
        self.assertEqual(series.labels, ["1Mx10Y", "1Yx2Y"])
        self.assertEqual(series.iv, [[60.0, 50.0], [None, None], [62.0, None]])

    def test_day_with_missing_label_is_skipped(self):
        forwards = {d: dict(rows) for d, rows in self.forwards.items()}
        del forwards[DATES[2]]["1Yx2Y"]  # その日の計算に失敗した銘柄がある
        self.assertEqual(hv.compute(forwards, {}, LABELS).dates, [DATES[1], DATES[3]])
        self.assertEqual(hv.compute(forwards, {}, ["1Mx10Y"]).dates, DATES[1:])

    def test_label_outside_grid(self):
        with self.assertRaisesRegex(ValueError, "2Mx10Y"):
            hv.compute(self.forwards, {}, ["1Mx10Y", "2Mx10Y"])

    def test_no_forwards(self):
        with self.assertRaisesRegex(ValueError, "朝バッチ"):
            hv.compute({}, {}, LABELS)

    def test_too_few_dates(self):
        series = hv.compute({DATES[0]: self.forwards[DATES[0]]}, {}, LABELS)
        self.assertEqual(series.dates, [])


class VolHistoryUdfTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.store = HistoricalStore(Path(cls.tmp.name) / "historical.db")
        source = BootstrapSource(SHIFT_BP)
        save_curves(cls.store, source)
        swaption_forward.build(cls.store, LABELS, log=lambda *_: None)
        cls.store.save_vol_quotes({DATES[2]: {"1Mx10Y": 61.5}})
        cls.expected = hv.compute(forwards_from(source, LABELS), cls.store.vol_quotes(), LABELS)

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
            # DBに保存したカーブ(暦日のDF)から計算しても、その場で組んだカーブと同じ値になる
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
        for row, label in ((rows[0], LABELS[0]), (rows[2], LABELS[1])):
            terms = underlying(base, label).terms  # DBの各日と同じ契約(volの換算と同じ)
            self.assertAlmostEqual(row[0], par_rate(market, terms).target_fixrate, places=12)

    def test_no_labels(self):
        with self.assertRaises(ValueError):
            udf.VolFwdLive([None, ""], [["1", "1Y", 1.0]])


if __name__ == "__main__":
    unittest.main()
