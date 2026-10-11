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
from swap_pricing.risk import bucketed_delta  # noqa: E402
from swap_pricing.swap_pricer import _set_fixing_history, par_rate, price_swap  # noqa: E402
from swap_pricing.terms import STANDARD, SwapParams, SwapTerms  # noqa: E402
from tests.test_curve_repricing import SPOT_RATES  # noqa: E402

VALUATION = date(2026, 9, 25)  # 金曜(営業日)
CONV = STANDARD
# UDFに渡すときの規約の列(Excelの fix freq / fix dcf / float freq / roll conv の並び)
CONV_ARGS = (CONV.fix_freq, CONV.fix_dcf, CONV.float_freq, CONV.roll_conv)
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
        boot.curve, valuation, SwapTerms(start, CONV, tenor=tenor), notional=NOTIONAL, pay_rec="PAY",
        fix_rate=fix_rate, index=boot.index, fixings=fixings,
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

    def test_fixing_registry_matches_passed_fixings(self) -> None:
        """差分だけの登録でも、QuantLibの履歴は渡した内容と常に完全に一致すること
        (日付が増える・値が変わる・日付が減る・空にする、を続けて確認する)。"""
        index = self.boot.index
        dates = sorted(FIXINGS)
        changed = dict(FIXINGS)
        changed[dates[100]] += 0.25
        steps = [
            {d: FIXINGS[d] for d in dates[:200]},
            {d: FIXINGS[d] for d in dates[:201]},  # 1日増えた(差分だけ登録)
            dict(FIXINGS),  # まとめて増えた
            changed,  # 値が変わった(登録し直し)
            {d: FIXINGS[d] for d in dates[:50]},  # 減った(登録し直し)
            None,  # 空にする
        ]
        for n, fixings in enumerate(steps):
            with self.subTest(step=n):
                _set_fixing_history(index, fixings)
                ts = index.timeSeries()
                registered = {d.to_date(): v * 100.0 for d, v in zip(ts.dates(), ts.values())}
                expected = {
                    d: v for d, v in (fixings or {}).items()
                    if index.isValidFixingDate(ql.Date.from_date(d))
                }
                self.assertEqual(registered.keys(), expected.keys())
                for d, v in expected.items():
                    self.assertAlmostEqual(registered[d], v, places=12)

    def test_fixing_registry_recovers_after_failed_registration(self) -> None:
        """登録の途中で例外が起きても、次の呼び出しで履歴が渡した内容に揃い直すこと
        (記録が古いまま残ると、消えた分を入れ直さずに差分だけを足してしまう)。"""
        index = self.boot.index
        _set_fixing_history(index, FIXINGS)
        broken = dict(FIXINGS)
        broken[max(FIXINGS)] = "数値でない"  # 値が変わった扱い → 全部消した後の変換で失敗する
        with self.assertRaises(TypeError):
            _set_fixing_history(index, broken)
        _set_fixing_history(index, FIXINGS)
        ts = index.timeSeries()
        self.assertEqual(len(ts.dates()), len(FIXINGS))

    def test_par_rate_matches_price_swap(self) -> None:
        """パーレートだけの経路(ヒストリカル用)が、price_swapのパーレート・満期日と一致すること。
        ヒストリカルは以前からprice_swapを想定元本1・PAYで呼んでいたので、その条件で完全一致を見る
        (fairRateは想定元本が違うと最下位ビットが変わりうる)。"""
        for start, tenor, fixings in [(self.boot.spot_date, "10Y", None),
                                      (date(2026, 6, 24), "1Y", FIXINGS),
                                      (date(2024, 9, 24), "3Y", FIXINGS)]:
            with self.subTest(start=start, tenor=tenor):
                terms = SwapTerms(start, CONV, tenor=tenor)
                full = price_swap(self.boot.curve, VALUATION, terms, notional=1.0, pay_rec="PAY",
                                  index=self.boot.index, fixings=fixings)
                par = par_rate(self.boot.curve, VALUATION, terms, index=self.boot.index, fixings=fixings)
                self.assertEqual(par.target_fixrate, full.target_fixrate)
                self.assertEqual(par.maturity_date, full.maturity_date)

    def test_spot_start_does_not_need_fixings(self) -> None:
        _price(self.boot, self.boot.spot_date, "10Y", None)

    def test_bucketed_delta_with_past_start(self) -> None:
        params = SwapParams(SwapTerms(date(2026, 6, 24), CONV, tenor="1Y"), NOTIONAL, "PAY",
                            fix_rate=0.9, fixings=FIXINGS)
        curves = curve_set(realtime_snapshot(VALUATION, SPOT_RATES))
        deltas = bucketed_delta(curves, params)
        self.assertGreater(sum(deltas.values()), 0)  # PAYなので金利上昇で得


class EvaluationDateTest(unittest.TestCase):
    def test_cached_curve_after_other_date_bootstrap(self) -> None:
        """別日付のカーブ構築(ヒストリカル計算)を挟んでも、キャッシュ済みカーブで同じ値になる。"""
        boot = bootstrap_curve(VALUATION, SPOT_RATES)

        def par() -> float:
            return price_swap(boot.curve, VALUATION, SwapTerms(boot.spot_date, CONV, tenor="10Y"),
                              notional=1.0, pay_rec="PAY", index=boot.index).target_fixrate

        base = par()
        bootstrap_curve(date(2026, 9, 24), SPOT_RATES)  # グローバル評価日が前日に変わる
        self.assertAlmostEqual(par(), base, delta=1e-10)


if __name__ == "__main__":
    unittest.main()
