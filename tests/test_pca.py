"""
PCA(engine.pca)のリグレッションテスト。

実行方法:
    python -m unittest tests.test_pca -v
    (LSEGのデータには依存しない。合成した日付×銘柄の表で、windowの選び方
     (推定基準日・欠損日の除外・日次変化の隣接判定)、標準化、比較日の実績(日次変化の基準日)、
     主成分の符号の向き、DBからの読み込み(スワップ/ボラ)を確認する)
"""

import sys
import tempfile
import unittest
from datetime import date
from unittest import mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402
import QuantLib as ql  # noqa: E402

from engine.pca import udf  # noqa: E402
from engine.pca.history import SWAP, VOL, load_history, parse_dataset  # noqa: E402
from engine.pca.pca import (  # noqa: E402
    CHANGE,
    LEVEL,
    default_until,
    estimate,
    gap_warning,
    observe,
    parse_mode,
    parse_standardize,
    principal_components,
)
from engine.common.conventions import CALENDAR  # noqa: E402
from engine.common.historical_store import HistoricalStore  # noqa: E402

LABELS = ["O/N", "1Y", "2Y", "5Y", "10Y"]


def _business_days(start: date, n: int):
    d = ql.Date.from_date(start)
    days = []
    while len(days) < n:
        if CALENDAR.isBusinessDay(d):
            days.append(d.to_date())
        d += 1
    return days


DAYS = _business_days(date(2026, 1, 5), 40)  # 2026-01-05(月)から40営業日


def _history(days=DAYS, seed=0):
    rng = np.random.default_rng(seed)
    level = np.array([0.5, 0.8, 1.0, 1.4, 1.8])
    history = {}
    for d in days:
        level = level + rng.normal(0, 0.01) + rng.normal(0, 0.005, len(LABELS))
        history[d] = dict(zip(LABELS, level.tolist()))
    return history


def _est(history, mode=LEVEL, window=10, until=DAYS[-2], standardize=False, labels=LABELS):
    return estimate(history, labels, mode, standardize, window, until, 100.0)


def _x(history, d, labels=LABELS):
    return np.array([history[d][t] for t in labels])


class EstimateTest(unittest.TestCase):
    def test_window_ends_at_base_date_inclusive(self):
        history = _history()
        m = _est(history, until=DAYS[-5])
        self.assertEqual(m.end, DAYS[-5])
        self.assertEqual(m.start, DAYS[-14])
        self.assertEqual(m.n_samples, 10)
        np.testing.assert_allclose(m.mean, np.mean([_x(history, d) for d in DAYS[-14:-4]], axis=0))
        np.testing.assert_array_equal(m.scale, np.ones(len(LABELS)))

    def test_default_base_date_excludes_target(self):
        # 推定基準日が空欄なら比較日より前まで(当日は推定に使わない)
        history = _history()
        m = _est(history, until=default_until(DAYS[-1]))
        self.assertEqual(m.end, DAYS[-2])
        self.assertEqual(gap_warning(m.end, DAYS[-1], "推定終了日"), "")

    def test_missing_label_days_are_skipped(self):
        history = _history()
        del history[DAYS[-3]]["5Y"]
        m = _est(history)
        self.assertEqual(m.n_samples, 10)
        self.assertEqual(m.start, DAYS[-12])
        self.assertEqual(m.excluded_days, 1)
        self.assertEqual(_est(history, labels=["1Y", "10Y"]).excluded_days, 0)

    def test_change_uses_adjacent_business_days_only(self):
        # 間の営業日が欠けたペア(2日分の変化)は使わない
        history = _history()
        del history[DAYS[-5]]["2Y"]
        m = _est(history, mode=CHANGE, window=5)
        pairs = [(-9, -8), (-8, -7), (-7, -6), (-4, -3), (-3, -2)]
        changes = np.array([_x(history, DAYS[b]) - _x(history, DAYS[a]) for a, b in pairs])
        np.testing.assert_allclose(m.mean, changes.mean(axis=0))
        self.assertEqual((m.start, m.end, m.excluded_days), (DAYS[-9], DAYS[-2], 1))

    def test_standardize(self):
        # 標準化ありは σ=標準偏差、固有値の和=変数の数(相関行列のトレース)
        history = _history()
        m = _est(history, standardize=True, window=30)
        samples = np.array([_x(history, d) for d in DAYS[-31:-1]])
        np.testing.assert_allclose(m.scale, samples.std(axis=0, ddof=1))
        self.assertAlmostEqual(m.eigenvalues.sum(), len(LABELS))
        # 標準化なしは固有値の和=分散(bp²)の和
        m = _est(history, window=30)
        self.assertAlmostEqual(m.eigenvalues.sum(), (samples * 100).var(axis=0, ddof=1).sum())

    def test_standardize_rejects_flat_series(self):
        history = _history()
        for d in DAYS:
            history[d]["O/N"] = 0.5  # 動かない(利上げの間のTONA)
        with self.assertRaisesRegex(ValueError, "O/N"):
            _est(history, standardize=True)
        _est(history, standardize=False)  # 標準化なしなら計算できる

    def test_full_reconstruction(self):
        # 全主成分なら、標準化の有無・モードによらず復元=実績(任意の日)
        history = _history()
        for mode in (LEVEL, CHANGE):
            for standardize in (False, True):
                m = _est(history, mode=mode, standardize=standardize, window=20, until=DAYS[-15])
                x = observe(history, LABELS, mode, DAYS[-1], history[DAYS[-1]], "DB").values
                z = (x - m.mean) / m.scale
                recon = m.mean + m.scale * (m.vectors @ (m.vectors.T @ z))
                np.testing.assert_allclose(recon, x, atol=1e-12)

    def test_insufficient_samples(self):
        with self.assertRaisesRegex(ValueError, "使えるのは"):
            _est(_history(), window=50)

    def test_gap_warning_when_db_is_stale(self):
        self.assertIn("営業日前", gap_warning(DAYS[-1], date(2026, 6, 1), "推定終了日"))
        self.assertEqual(gap_warning(date(2026, 1, 9), date(2026, 1, 10), "推定終了日"), "")  # 土曜

    def test_invalid_inputs(self):
        with self.assertRaisesRegex(ValueError, "重複"):
            _est(_history(), labels=["1Y", "1y"])
        with self.assertRaisesRegex(ValueError, "選択されていません"):
            _est(_history(), labels=[])
        with self.assertRaisesRegex(ValueError, "モード"):
            parse_mode("PCA")
        with self.assertRaisesRegex(ValueError, "標準化"):
            parse_standardize("たぶん")
        self.assertEqual(parse_mode("日次変化"), CHANGE)
        self.assertTrue(parse_standardize("あり"))
        self.assertFalse(parse_standardize(False))
        # 新しいシートのプルダウンは英語(PCAGrid)
        self.assertEqual(parse_mode("Level"), LEVEL)
        self.assertEqual(parse_mode("Change"), CHANGE)
        self.assertTrue(parse_standardize("Yes"))
        self.assertFalse(parse_standardize("No"))
        self.assertEqual(parse_dataset("vol"), VOL)
        self.assertEqual(parse_dataset(None), SWAP)


class ObserveTest(unittest.TestCase):
    def test_level_and_change(self):
        history = _history()
        live = {"O/N": 0.6, "1Y": 0.9, "2Y": 1.1, "5Y": 1.5, "10Y": 1.9}
        obs = observe(history, LABELS, LEVEL, DAYS[-1], live, "RealTime")
        np.testing.assert_allclose(obs.values, [0.6, 0.9, 1.1, 1.5, 1.9])
        obs = observe(history, LABELS, CHANGE, DAYS[-1], live, "RealTime")
        np.testing.assert_allclose(obs.values, np.array(list(live.values())) - _x(history, DAYS[-2]))
        self.assertEqual((obs.base_date, obs.warning), (DAYS[-2], ""))

    def test_change_with_stale_db_warns(self):
        # 前営業日がDBになければ直近の日を基準にして、何営業日分の変化かを警告する
        history = _history()
        del history[DAYS[-2]]
        obs = observe(history, LABELS, CHANGE, DAYS[-1], _history()[DAYS[-1]], "DB")
        self.assertEqual(obs.base_date, DAYS[-3])
        self.assertIn("2営業日分", obs.warning)

    def test_missing_labels(self):
        with self.assertRaisesRegex(ValueError, "RealTimeに値がない銘柄: 5Y, 10Y"):
            observe(_history(), LABELS, LEVEL, DAYS[-1], {"O/N": 0.5, "1Y": 1, "2Y": 1}, "RealTime")


class PrincipalComponentsTest(unittest.TestCase):
    def test_sign_convention(self):
        # PC1(全体が同方向)は和が正、PC2(傾き)は後ろ側が正
        rng = np.random.default_rng(1)
        x = np.linspace(-1, 1, 6)
        samples = np.outer(rng.normal(0, 10, 300), np.ones(6)) + np.outer(rng.normal(0, 3, 300), x)
        samples += rng.normal(0, 0.1, samples.shape)
        for flip in (1, -1):  # 入力の符号を反転しても同じ向き
            result = principal_components(flip * samples)
            self.assertGreater(result.vectors[:, 0].sum(), 0)
            self.assertGreater(result.vectors[-1, 1], result.vectors[0, 1])
            self.assertTrue(np.all(np.diff(result.eigenvalues) <= 0))


class PcaGridTest(unittest.TestCase):
    """PcaGrid: 複数ケースの推定を1回の呼び出しで返す(PcaModel/PcaWindowと同じ値)。"""

    def _grid(self, settings, today=DAYS[-1]):
        history = _history()
        udf._estimate_cached.cache_clear()
        with mock.patch.object(udf, "load_history", return_value=history),                 mock.patch.object(udf, "_db_key", return_value=1),                 mock.patch.object(udf, "_today", return_value=today):
            return history, udf.PcaGrid(LABELS, settings)

    def test_cases(self):
        # 列=ケース: [推定基準日, window, モード, 標準化]。3列目はwindowが空欄なので使わない
        settings = [
            ["", DAYS[-6], "", ""],
            [10, 8, "", 100],
            ["Level", "Change", "Level", "Level"],
            ["No", "Yes", "No", "No"],
        ]
        history, rows = self._grid(settings)
        self.assertEqual(rows[0], udf.GRID_HEADER + [f"PC{j + 1}" for j in range(len(LABELS))])
        cases = [r[0] for r in rows[1:]]
        self.assertEqual(cases, [1] * 5 + [2] * 5 + [4])

        # ケース1: 推定基準日が空欄=今日より前の直近(PcaModelと同じ推定)
        model = _est(history, LEVEL, 10, default_until(DAYS[-1]))
        case1 = rows[1:6]
        self.assertEqual(case1[0][1:6], [model.start, model.end, 10, model.excluded_days, ""])
        self.assertEqual([r[6] for r in case1], LABELS)
        np.testing.assert_allclose([r[7] for r in case1], model.mean)
        np.testing.assert_allclose([r[9:] for r in case1], model.vectors)

        # ケース2: 推定基準日あり・日次変化・標準化あり
        model = _est(history, CHANGE, 8, DAYS[-6], standardize=True)
        case2 = rows[6:11]
        self.assertEqual(case2[0][2], DAYS[-6])
        np.testing.assert_allclose([r[8] for r in case2], model.scale)
        np.testing.assert_allclose([r[9:] for r in case2], model.vectors)

        # ケース4: 推定できない(サンプル不足)→ 1行だけで警告に理由、他のケースは計算される
        self.assertEqual(len(rows[-1]), len(rows[0]))
        self.assertIn("window", rows[-1][5])
        self.assertEqual(rows[-1][6:], [""] * (len(LABELS) + 3))

    def test_gap_warning_when_base_is_blank(self):
        today = _business_days(DAYS[-1], 4)[-1]  # DBの最終日から3営業日後
        _, rows = self._grid([[""], [10], ["Level"], ["No"]], today=today)
        self.assertIn("推定終了日", rows[1][5])


class LoadHistoryTest(unittest.TestCase):
    def test_swap_and_vol_from_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = HistoricalStore(Path(tmp) / "h.db")
            holiday = date(2026, 1, 12)  # 成人の日
            store.save_quotes({date(2026, 1, 9): {"12M": 0.8, "m1": 0.5}, holiday: {"12M": 0.9}})
            store.save_fixings({date(2026, 1, 9): 0.48})
            store.save_vol_quotes({date(2026, 1, 9): {"1Yx10Y": 72.0}, holiday: {"1Yx10Y": 70.0}})
            self.assertEqual(
                load_history(SWAP, store), {date(2026, 1, 9): {"12M": 0.8, "M1": 0.5, "O/N": 0.48}},
            )
            self.assertEqual(load_history(VOL, store), {date(2026, 1, 9): {"1YX10Y": 72.0}})


if __name__ == "__main__":
    unittest.main()
