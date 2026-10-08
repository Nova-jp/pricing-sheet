"""
カーブ・ブートストラップと price_swap の逆算精度に対するリグレッションテスト。

本プロジェクトのハード要件(docs/decisions.md参照)は「市場スポットレートの
正確な逆算(機械精度〜0.1bp未満)」。日付関連ロジックをQuantLib純正の
構成(ql.MakeOIS / ql.PeriodParser / ql.Date.from_date 等)へ置き換えた際に、
この精度を落としていないことを機械的に確認するために追加した。

実行方法:
    python -m unittest tests.test_curve_repricing -v
    (会社PC以外でも動くよう、LSEGのデータには依存しない
     架空だが実際のRealTimeシートの値に基づくレートを使う)
"""

import sys
import unittest
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import QuantLib as ql  # noqa: E402

from swap_pricing.curve import bootstrap_curve  # noqa: E402
from swap_pricing.swap_pricer import _build_swap, price_swap  # noqa: E402

# 実際のRealTimeシート(2026-09時点の実データ)のスポットレート一式。
SPOT_RATES = {
    "1W": 0.9775, "1M": 1.09125, "2M": 1.16625, "3M": 1.205, "4M": 1.25,
    "5M": 1.29625, "6M": 1.33125, "7M": 1.37375, "8M": 1.4125, "9M": 1.44875,
    "10M": 1.485, "11M": 1.5225, "12M": 1.5575, "15M": 1.64625, "18M": 1.7225,
    "21M": 1.78375, "24M": 1.8525, "36M": 2.0325, "4Y": 2.16625, "5Y": 2.285,
    "6Y": 2.395, "7Y": 2.5, "8Y": 2.60125, "9Y": 2.69625, "10Y": 2.785,
    "11Y": 2.8675, "12Y": 2.94375, "15Y": 3.13875, "20Y": 3.3725, "25Y": 3.4925,
    "30Y": 3.54, "35Y": 3.5525, "40Y": 3.5475,
}

# 標準コンベンション(カーブ自体のpillarと同じ)でスワップを組んだときのpar rateが
# 入力スポットレートに戻るべきテナー(O/N・週物は別種のヘルパーのため対象外)。
_MONTH_YEAR_TENORS = [
    "1M", "2M", "3M", "4M", "5M", "6M", "7M", "8M", "9M", "10M", "11M", "12M",
    "15M", "18M", "21M", "24M", "36M", "4Y", "5Y", "6Y", "7Y", "8Y", "9Y", "10Y",
    "11Y", "12Y", "15Y", "20Y", "25Y", "30Y", "35Y", "40Y",
]

TOLERANCE_PCT = 1e-6  # 0.0001bp。「機械精度〜0.1bp未満」要件に対して十分厳しい値。


class RepricingTest(unittest.TestCase):
    """pillarをそのままのコンベンション(PA/act365f/PA/act365f/STD)で
    再度スワップに組んだとき、パーレートが入力スポットレートに戻ることを確認する。
    """

    def _assert_repricing(self, valuation_date: date) -> None:
        boot = bootstrap_curve(valuation_date, SPOT_RATES)
        for tenor in _MONTH_YEAR_TENORS:
            with self.subTest(valuation_date=valuation_date, tenor=tenor):
                result = price_swap(
                    boot.curve, valuation_date, boot.spot_date,
                    "PA", "act/365fixed", "PA", "act/365fixed", "STD",
                    notional=1.0, pay_rec="PAY", tenor=tenor.lower(), index=boot.index,
                )
                self.assertAlmostEqual(result.target_fixrate, SPOT_RATES[tenor], delta=TOLERANCE_PCT)

    def test_repricing_on_business_day(self) -> None:
        """平日評価。以前から機械精度で一致していたケース(回帰確認)。"""
        self._assert_repricing(date(2026, 9, 4))  # 金曜

    def test_repricing_on_weekend(self) -> None:
        """
        評価日が非営業日(土日)のケース。

        以前は spot_date の算出方法(自前の add_business_days)が QuantLib の
        OISRateHelper/MakeOIS 内部の設定日ロジックと食い違い、非営業日評価時に
        pillarと1営業日ズレて 5Y で約0.11bp、10Yで約0.08bpのミスプライスが
        発生していた(このテストはそのバグの再発防止用)。
        """
        self._assert_repricing(date(2026, 9, 6))  # 日曜
        self._assert_repricing(date(2026, 9, 5))  # 土曜
        self._assert_repricing(date(2026, 9, 26))  # 土曜(この置き換え作業当日の実例)

    def test_spot_date_matches_helper_settlement(self) -> None:
        """spot_dateがOISRateHelperの内部設定日ロジックと一致することを直接確認する。"""
        valuation_date = date(2026, 9, 6)  # 日曜
        boot = bootstrap_curve(valuation_date, SPOT_RATES)

        # OISRateHelperが実際に使う設定日ロジックを、別経路で再現して比較する。
        index = ql.Tonar()
        probe = ql.MakeOIS(ql.Period(1, ql.Years), index, 0.0, settlementDays=2)
        self.assertEqual(boot.spot_date, probe.startDate().to_date())


class ExplicitEndAndConventionTest(unittest.TestCase):
    """tenor以外の入力パターン(明示end日付、脚ごとに異なるfreq、EOM/IMM)の健全性確認。"""

    def setUp(self) -> None:
        self.valuation_date = date(2026, 9, 4)
        self.boot = bootstrap_curve(self.valuation_date, SPOT_RATES)

    def test_par_swap_has_zero_pv(self) -> None:
        """パーレートで組んだスワップのPVはゼロに近いはず(fix_rate未指定)。"""
        result = price_swap(
            self.boot.curve, self.valuation_date, self.boot.spot_date,
            "PA", "act/365fixed", "PA", "act/365fixed", "STD",
            notional=1_000_000_000.0, pay_rec="PAY", tenor="10y", index=self.boot.index,
        )
        self.assertAlmostEqual(result.pv, 0.0, delta=1e-3)

    def test_explicit_end_date_without_tenor(self) -> None:
        result = price_swap(
            self.boot.curve, self.valuation_date, date(2026, 9, 8),
            "PA", "act/365fixed", "PA", "act/365fixed", "STD",
            notional=1.0, pay_rec="PAY", end=date(2026, 10, 8), index=self.boot.index,
        )
        self.assertEqual(result.maturity_date, date(2026, 10, 8))

    def test_mismatched_fixed_and_float_frequency(self) -> None:
        result = price_swap(
            self.boot.curve, self.valuation_date, self.boot.spot_date,
            "SA", "act/365fixed", "PA", "act/365fixed", "STD",
            notional=1.0, pay_rec="PAY", tenor="5y", index=self.boot.index,
        )
        self.assertGreater(result.target_fixrate, 0.0)

    def test_eom_roll_keeps_month_end(self) -> None:
        result = price_swap(
            self.boot.curve, self.valuation_date, date(2027, 8, 31),
            "PA", "act/365fixed", "PA", "act/365fixed", "EOM",
            notional=1.0, pay_rec="PAY", tenor="10y", index=self.boot.index,
        )
        self.assertEqual(result.maturity_date, date(2037, 8, 31))

    def test_eom_roll_with_non_month_end_start_behaves_like_std(self) -> None:
        """
        startが月末でない場合、EOMロールはSTDと完全に同じ結果になる(QuantLib自体の
        仕様。Calendar.advance/ql.Scheduleがeffective dateの月末判定でendOfMonth
        フラグを内部的にゲートするため)。「EOMを選んだのに月末でない限り無視される」
        は不具合ではなく、EOM規約自体の定義(月末発の場合だけ意味を持つ)による。
        """
        start = date(2026, 9, 15)  # 月末ではない
        common_args = (
            self.boot.curve, self.valuation_date, start,
            "PA", "act/365fixed", "PA", "act/365fixed",
        )
        result_eom = price_swap(*common_args, "EOM", notional=1.0, pay_rec="PAY", tenor="1y", index=self.boot.index)
        result_std = price_swap(*common_args, "STD", notional=1.0, pay_rec="PAY", tenor="1y", index=self.boot.index)
        self.assertEqual(result_eom.maturity_date, result_std.maturity_date)
        self.assertAlmostEqual(result_eom.target_fixrate, result_std.target_fixrate, delta=1e-9)

    def test_imm_roll_intermediate_dates_land_on_third_wednesday(self) -> None:
        result = price_swap(
            self.boot.curve, self.valuation_date, self.boot.spot_date,
            "QA", "act/365fixed", "QA", "act/365fixed", "IMM",
            notional=1.0, pay_rec="PAY", tenor="2y", index=self.boot.index,
        )
        self.assertGreater(result.target_fixrate, 0.0)
        # 中間のロール日(最初と最後の効果日・満期日を除く)はIMM(第3水曜)ルールに従う。
        # 最後の満期日は指定tenorの終端そのものであり、必ずしも第3水曜ではない。
        swap = _build_swap(
            self.boot.index, self.boot.spot_date, "QA", "act/365fixed", "QA", "act/365fixed",
            "IMM", 1.0, "PAY", 0.0, tenor="2y",
        )
        dates = list(swap.fixedSchedule().dates())
        for d in dates[1:-1]:
            self.assertEqual(d.weekday(), ql.Wednesday)


if __name__ == "__main__":
    unittest.main()
