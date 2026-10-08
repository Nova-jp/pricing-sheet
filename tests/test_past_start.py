"""
過去起算スワップ(TONA実績フィキシング使用)と評価日の扱いに対するリグレッションテスト。

実行方法:
    python -m unittest tests.test_past_start -v
    (LSEGのデータはPublicリポジトリに置けないため、フィキシングは合成値を使う)
"""

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import QuantLib as ql  # noqa: E402

from swap_pricing.conventions import CALENDAR  # noqa: E402
from swap_pricing.curve import bootstrap_curve  # noqa: E402
from swap_pricing.market import curve_set, realtime_snapshot  # noqa: E402
from swap_pricing.risk import SwapParams, bucketed_delta  # noqa: E402
from swap_pricing.swap_pricer import price_swap  # noqa: E402
from tests.test_curve_repricing import SPOT_RATES  # noqa: E402

VALUATION = date(2026, 9, 25)  # 金曜(営業日)
CONV = dict(fix_freq="PA", fix_dcf="act/365fixed", float_freq="PA", float_dcf="act/365fixed", roll_conv="STD")
NOTIONAL = 10_000_000_000


def _synthetic_fixings(first: date, last: date) -> dict:
    """営業日ごとに少しずつ変わる合成TONA(%)。"""
    fixings = {}
    d = first
    while d <= last:
        if CALENDAR.isBusinessDay(ql.Date.from_date(d)):
            fixings[d] = 0.5 + 0.001 * (d - first).days
        d += timedelta(days=1)
    return fixings


FIXINGS = _synthetic_fixings(date(2025, 1, 6), VALUATION - timedelta(days=1))


def _price(boot, start, tenor, fixings, fix_rate=0.9, valuation=VALUATION):
    return price_swap(
        boot.curve, valuation, start, notional=NOTIONAL, pay_rec="PAY", tenor=tenor,
        fix_rate=fix_rate, index=boot.index, fixings=fixings, **CONV,
    )


class PastStartTest(unittest.TestCase):
    def setUp(self) -> None:
        self.boot = bootstrap_curve(VALUATION, SPOT_RATES)

    def test_past_start_without_fixings_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "フィキシング"):
            _price(self.boot, date(2026, 6, 24), "1Y", None)

    def test_missing_fixing_is_reported(self) -> None:
        fixings = dict(FIXINGS)
        del fixings[date(2026, 9, 24)]
        with self.assertRaisesRegex(ValueError, "1日分不足"):
            _price(self.boot, date(2026, 6, 24), "1Y", fixings)

    def test_only_alive_coupon_period_needs_fixings(self) -> None:
        """支払い済みクーポンの期間のフィキシングは不要(起算日まで遡らなくてよい)。"""
        start = date(2024, 9, 24)  # 3Y、第3期間(2026-09-24〜)だけが未払い
        partial = {d: v for d, v in FIXINGS.items() if d >= date(2026, 9, 24)}
        _price(self.boot, start, "3Y", partial)  # 例外にならない

    def test_fixings_after_valuation_date_are_ignored(self) -> None:
        future = dict(FIXINGS)
        future.update(_synthetic_fixings(VALUATION + timedelta(days=1), date(2026, 12, 30)))
        a = _price(self.boot, date(2026, 6, 24), "1Y", FIXINGS).pv
        b = _price(self.boot, date(2026, 6, 24), "1Y", future).pv
        self.assertEqual(a, b)

    def test_fixing_history_is_replaced_not_accumulated(self) -> None:
        """前回の呼び出しで登録した履歴が、次の呼び出しで黙って使われないこと。"""
        _price(self.boot, date(2026, 6, 24), "1Y", FIXINGS)
        with self.assertRaises(ValueError):
            _price(self.boot, date(2026, 6, 24), "1Y", {})

    def test_spot_start_does_not_need_fixings(self) -> None:
        _price(self.boot, self.boot.spot_date, "10Y", None)

    def test_bucketed_delta_with_past_start(self) -> None:
        params = SwapParams(start=date(2026, 6, 24), tenor="1Y", notional=NOTIONAL, pay_rec="PAY",
                            fix_rate=0.9, fixings=FIXINGS, **CONV)
        curves = curve_set(realtime_snapshot(VALUATION, SPOT_RATES))
        deltas = bucketed_delta(curves, params)
        self.assertGreater(sum(deltas.values()), 0)  # PAYなので金利上昇で得


class EvaluationDateTest(unittest.TestCase):
    def test_cached_curve_after_other_date_bootstrap(self) -> None:
        """別日付のカーブ構築(ヒストリカル計算)を挟んでも、キャッシュ済みカーブで同じ値になる。"""
        boot = bootstrap_curve(VALUATION, SPOT_RATES)

        def par() -> float:
            return price_swap(boot.curve, VALUATION, boot.spot_date, notional=1.0, pay_rec="PAY",
                              tenor="10Y", index=boot.index, **CONV).target_fixrate

        base = par()
        bootstrap_curve(date(2026, 9, 24), SPOT_RATES)  # グローバル評価日が前日に変わる
        self.assertAlmostEqual(par(), base, delta=1e-10)


if __name__ == "__main__":
    unittest.main()
