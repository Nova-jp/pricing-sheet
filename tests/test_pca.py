"""
PCA(engine.pca)のリグレッションテスト。

実行方法:
    python -m unittest tests.test_pca -v
    (LSEGのデータには依存しない。合成した日付×銘柄の表で、windowの選び方
     (推定の最終日・欠損日の除外・日次変化の隣接判定)、標準化、復元日の実績(日次変化の基準日)、
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
    estimate,
    gap_warning,
    observe,
    parse_mode,
    parse_standardize,
    principal_components,
    residual_std,
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
        # Std(グラフの±1σ用)は標準化なしでもwindowの標準偏差
        np.testing.assert_allclose(
            m.std, np.std([_x(history, d) for d in DAYS[-14:-4]], axis=0, ddof=1),
        )

    def test_window_end_is_included_if_in_db(self):
        # 推定の最終日の値がDBにあればその日まで使う(今日の引け値がDBにあれば今日も入る)
        history = _history()
        m = _est(history, until=DAYS[-1])
        self.assertEqual(m.end, DAYS[-1])
        self.assertEqual(gap_warning(m.end, DAYS[-1]), "")

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
        np.testing.assert_array_equal(m.std, m.scale)
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
        self.assertIn("business days before", gap_warning(DAYS[-1], date(2026, 6, 1)))
        self.assertEqual(gap_warning(date(2026, 1, 9), date(2026, 1, 10)), "")  # 土曜
        self.assertEqual(gap_warning(date(2026, 1, 9), date(2026, 1, 13)), "")  # 前営業日(12日は祝日)

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


def _resid_by_day(history, m, k, days, mode=LEVEL):
    """定義どおり: 各日(LEVELは水準、CHANGEは前の日との差)を k 本で復元した差分(bp)の標準偏差。"""
    if mode == LEVEL:
        x = np.array([_x(history, d) for d in days])
    else:
        x = np.array([_x(history, b) - _x(history, a) for a, b in zip(days, days[1:])])
    v = m.vectors[:, :k]
    resid = [(xi - (m.mean + m.scale * (v @ (v.T @ ((xi - m.mean) / m.scale))))) * 100 for xi in x]
    return np.std(resid, axis=0, ddof=1)


class ResidualStdTest(unittest.TestCase):
    """差分のσ: ベクトル・μ・σを固定して、各日の差分の標準偏差をとる。"""

    def test_same_days_as_window(self):
        history = _history()
        for mode in (LEVEL, CHANGE):
            for standardize in (False, True):
                m = _est(history, mode=mode, standardize=standardize, window=20)
                days = DAYS[-21:-1] if mode == LEVEL else DAYS[-22:-1]
                for k in (1, 3):
                    np.testing.assert_allclose(
                        residual_std(history, m, k, None, 100.0), _resid_by_day(history, m, k, days, mode),
                    )
                # 同じwindowなら、k本目より後の固有値から出るσと一致する(一次変換なので)
                k = 2
                closed = m.scale * np.sqrt((m.vectors[:, k:] ** 2) @ m.eigenvalues[k:])
                closed *= 100.0 if standardize else 1.0
                np.testing.assert_allclose(residual_std(history, m, k, None, 100.0), closed)

    def test_longer_than_window_uses_earlier_days(self):
        history = _history()
        m = _est(history, window=10)
        np.testing.assert_allclose(
            residual_std(history, m, 2, 25, 100.0), _resid_by_day(history, m, 2, DAYS[-26:-1]),
        )

    def test_all_components_and_errors(self):
        history = _history()
        m = _est(history, window=10)
        np.testing.assert_allclose(residual_std(history, m, len(LABELS), None, 100.0), 0, atol=1e-10)
        with self.assertRaisesRegex(ValueError, "主成分数k"):
            residual_std(history, m, len(LABELS) + 1, None, 100.0)
        with self.assertRaisesRegex(ValueError, "σの日数"):
            residual_std(history, m, 1, 100, 100.0)


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
        self.assertIn("2 business days", obs.warning)

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
        # 列=ケース: [推定の最終日, window, モード, 標準化]。3列目はwindowが空欄なので使わない
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

        # ケース1: 推定の最終日が空欄=今日(PcaModelと同じ推定)
        model = _est(history, LEVEL, 10, DAYS[-1])
        case1 = rows[1:6]
        self.assertEqual(case1[0][1:6], [model.start, model.end, 10, model.excluded_days, ""])
        self.assertEqual([r[6] for r in case1], LABELS)
        np.testing.assert_allclose([r[7] for r in case1], model.mean)
        np.testing.assert_allclose([r[9] for r in case1], model.std)
        np.testing.assert_allclose([r[10:] for r in case1], model.vectors)

        # ケース2: 推定の最終日あり・日次変化・標準化あり
        model = _est(history, CHANGE, 8, DAYS[-6], standardize=True)
        case2 = rows[6:11]
        self.assertEqual(case2[0][2], DAYS[-6])
        np.testing.assert_allclose([r[8] for r in case2], model.scale)
        np.testing.assert_allclose([r[10:] for r in case2], model.vectors)

        # ケース4: 推定できない(サンプル不足)→ 1行だけで警告に理由、他のケースは計算される
        self.assertEqual(len(rows[-1]), len(rows[0]))
        self.assertIn("window", rows[-1][5])
        self.assertEqual(rows[-1][6:], [""] * (len(LABELS) + 4))

    def test_pca_model_has_same_columns(self):
        # PcaModelの列は PcaGridの「銘柄」以降と同じ [Label, Mean, Scale, Std, PC1, …]
        history = _history()
        udf._estimate_cached.cache_clear()
        with mock.patch.object(udf, "load_history", return_value=history),                 mock.patch.object(udf, "_db_key", return_value=1):
            table = udf.PcaModel(LABELS, "Level", "No", 10, window_end=DAYS[-6])
        self.assertEqual(table[0], udf.GRID_HEADER[6:] + [f"PC{j + 1}" for j in range(len(LABELS))])
        model = _est(history, LEVEL, 10, DAYS[-6])
        np.testing.assert_allclose([r[3] for r in table[1:]], model.std)
        np.testing.assert_allclose([r[4:] for r in table[1:]], model.vectors)

    def test_residual_std_by_case(self):
        settings = [["", ""], [10, 8], ["Level", "Change"], ["No", "Yes"]]
        history = _history()
        udf._estimate_cached.cache_clear()
        udf._resid_std_cached.cache_clear()
        with mock.patch.object(udf, "load_history", return_value=history),                 mock.patch.object(udf, "_db_key", return_value=1),                 mock.patch.object(udf, "_today", return_value=DAYS[-1]):
            rows = udf.PcaGridResidStd(LABELS, settings, [1, "", 3, 99], 20)
            grid = udf.PcaGrid(LABELS, settings)
            single = udf.PcaResidStd(LABELS, "Change", "Yes", 8, 3, sigma_days=20)
        self.assertEqual(rows[0], ["Case", "Warning", "ResidStd1", "ResidStd2", "ResidStd3", "ResidStd4"])
        self.assertEqual([r[0] for r in rows[1:]], [r[0] for r in grid[1:]])  # PcaGridと同じ並び
        case1, case2 = rows[1:6], rows[6:11]
        model = _est(history, LEVEL, 10, DAYS[-1])
        np.testing.assert_allclose([r[2] for r in case1], residual_std(history, model, 1, 20, 100.0))
        self.assertEqual({r[3] for r in case1}, {""})  # kが空欄の段
        self.assertEqual({r[5] for r in case1}, {""})  # k=99は計算できない → 警告
        self.assertIn("σ(k=99)", case1[0][1])
        np.testing.assert_allclose([r[4] for r in case2], [r[0] for r in single])

    def test_gap_warning(self):
        # 空欄(今日)でも日付の指定でも、推定に使った最後の日が最終日の前営業日より前なら警告
        today = _business_days(DAYS[-1], 4)[-1]  # DBの最終日から3営業日後
        _, rows = self._grid([["", today], [10, 10], ["Level", "Level"], ["No", "No"]], today=today)
        self.assertIn("Window ends", rows[1][5])
        self.assertIn("Window ends", rows[-1][5])


class PcaWindowTest(unittest.TestCase):
    """推定の最終日の空欄は今日。DBに今日の値がなければ直近の営業日までで、警告は出さない。"""

    def _window(self, today, window_end=None):
        history = _history()
        udf._estimate_cached.cache_clear()
        with mock.patch.object(udf, "load_history", return_value=history), \
                mock.patch.object(udf, "_db_key", return_value=1), \
                mock.patch.object(udf, "_today", return_value=today):
            return dict(udf.PcaWindow(LABELS, "Level", "No", 10, window_end))

    def test_blank_is_today(self):
        info = self._window(DAYS[-1])  # 今日の引け値がDBにある
        self.assertEqual((info["End"], info["Warning"]), (DAYS[-1], ""))
        next_day = _business_days(DAYS[-1], 2)[-1]  # DBにまだ今日の値がない
        info = self._window(next_day)
        self.assertEqual((info["End"], info["Warning"]), (DAYS[-1], ""))

    def test_window_end_date(self):
        info = self._window(DAYS[-1], DAYS[-6])
        self.assertEqual((info["Start"], info["End"], info["Warning"]), (DAYS[-15], DAYS[-6], ""))


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
