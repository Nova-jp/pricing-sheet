"""
任意コンベンションのヒストリカル計算(FIXED / ROLLING)のリグレッションテスト。

実行方法:
    python -m unittest tests.test_historical -v
    (カーブは全日付で同じ合成スポットレートからその場で組み、日付の決め方・
     フィキシングの扱い・満期後の除外といった自前ロジックだけを確認する。
     DBへの保存・復元は test_curve_store.py)
"""

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import QuantLib as ql  # noqa: E402

from swap_pricing.conventions import CALENDAR, forward_start_date, imm_code_to_date  # noqa: E402
from swap_pricing.curve import bootstrap_curve, spot_date_for  # noqa: E402
from swap_pricing.historical_pricer import (  # noqa: E402
    FIXED,
    ROLLING,
    SwapSpec,
    historical_multi_rate_series,
    historical_swap_rate_series,
)
from swap_pricing.swap_pricer import SwapExpiredError, price_swap  # noqa: E402
from tests.test_curve_repricing import SPOT_RATES  # noqa: E402
from tests.test_past_start import CONV, _synthetic_fixings  # noqa: E402

BASE = date(2026, 9, 25)  # 金曜(営業日)。ROLLINGの基準日
AS_OF_DATES = [date(2026, 3, 25), date(2026, 6, 25), BASE]  # いずれも営業日


class _BootstrapSource:
    """テスト用のカーブ供給元(合成レートでその場でブートストラップする)。"""

    def __init__(self, dates):
        self._dates = list(dates)

    def dates(self):
        return self._dates

    def load(self, as_of_date, until=None):
        return bootstrap_curve(as_of_date, SPOT_RATES)


def _rates(dates=AS_OF_DATES):
    return _BootstrapSource(dates)


def _direct(as_of, start, tenor=None, end=None, fixings=None, conv=CONV):
    boot = bootstrap_curve(as_of, SPOT_RATES)
    return price_swap(
        boot.curve, as_of, start, notional=1.0, pay_rec="PAY", tenor=tenor, end=end,
        index=boot.index, fixings=fixings, **conv,
    )


IMM_CONV = {**CONV, "roll_conv": "IMM"}


def _business_days(a, b):
    return CALENDAR.businessDaysBetween(ql.Date.from_date(a), ql.Date.from_date(b))


class RollingTest(unittest.TestCase):
    """ROLLINGは基準日の取引の「スポット日→起算日」「起算日→満期日」の営業日数を保って移動する。"""

    def _assert_rolled(self, series, base_start, base_end, conv=CONV):
        base_spot = spot_date_for(BASE)
        self.assertEqual([p.as_of_date for p in series], AS_OF_DATES)
        for p in series:
            with self.subTest(as_of=p.as_of_date):
                spot = spot_date_for(p.as_of_date)
                self.assertEqual(_business_days(spot, p.start_date), _business_days(base_spot, base_start))
                self.assertEqual(_business_days(p.start_date, p.maturity_date), _business_days(base_start, base_end))
                expected = _direct(p.as_of_date, p.start_date, end=p.maturity_date, conv=conv)
                self.assertEqual(p.par_rate, expected.target_fixrate)

    def test_blank_start_keeps_business_day_length(self):
        """start空欄・tenor指定: 基準日のスポット起点+tenorと同じ営業日数の、各日のスポット起点の取引。"""
        series = historical_swap_rate_series(
            ROLLING, tenor="5Y", base_date=BASE, curves=_rates(), **CONV,
        )
        base = _direct(BASE, spot_date_for(BASE), tenor="5Y")
        self._assert_rolled(series, spot_date_for(BASE), base.maturity_date)
        for p in series:
            self.assertEqual(p.start_date, spot_date_for(p.as_of_date))
        # 営業日数を保つので、満期日は「スポット日+5Y」とは一致しない日がある(了承済みの仕様)
        self.assertNotEqual(
            {p.maturity_date for p in series},
            {_direct(p.as_of_date, spot_date_for(p.as_of_date), tenor="5Y").maturity_date for p in series},
        )

    def test_forward_start_keeps_business_day_offset(self):
        base_spot = spot_date_for(BASE)
        start = CALENDAR.advance(ql.Date.from_date(base_spot), 1, ql.Years).to_date()  # 1年先スタート
        series = historical_swap_rate_series(
            ROLLING, start=start, tenor="5Y", base_date=BASE, curves=_rates(), **CONV,
        )
        self._assert_rolled(series, start, _direct(BASE, start, tenor="5Y").maturity_date)

    def test_base_date_reproduces_input_trade(self):
        """基準日当日の値は、入力どおりの日付の取引(=FairRate)と一致する。"""
        start, end = date(2027, 3, 31), date(2032, 3, 31)
        series = historical_swap_rate_series(
            ROLLING, start=start, end=end, base_date=BASE, curves=_rates([BASE]), **CONV,
        )
        self.assertEqual(series[0].start_date, start)
        self.assertEqual(series[0].par_rate, _direct(BASE, start, end=end).target_fixrate)

    def test_imm_codes_are_rolled(self):
        """IMMコードも日付として読み、他の日付と同じく営業日数を保って移動する。"""
        series = historical_swap_rate_series(
            ROLLING, start="M27", end="M38", base_date=BASE, curves=_rates(), **CONV,
        )
        start, end = imm_code_to_date("M27"), imm_code_to_date("M38")
        self._assert_rolled(series, start, _direct(BASE, start, end=end).maturity_date)
        self.assertEqual(len({p.start_date for p in series}), len(AS_OF_DATES))

    def test_tenor_start_equals_forward_date(self):
        """startのテナー(1Y)は基準日のスポット日+1Yの日付を入れたのと同じ。"""
        start = forward_start_date(spot_date_for(BASE), "1Y")
        by_tenor = historical_swap_rate_series(
            ROLLING, start="1Y", tenor="5Y", base_date=BASE, curves=_rates(), **CONV,
        )
        by_date = historical_swap_rate_series(
            ROLLING, start=start, tenor="5Y", base_date=BASE, curves=_rates(), **CONV,
        )
        self.assertEqual(by_tenor, by_date)

    def test_imm_roll_conv_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "IMM"):
            historical_swap_rate_series(
                ROLLING, start="M27", end="M38", base_date=BASE, curves=_rates(), **IMM_CONV,
            )


class FixedTest(unittest.TestCase):
    def test_dates_are_kept_as_input(self):
        start = date(2027, 3, 31)
        series = historical_swap_rate_series(
            FIXED, start=start, tenor="5Y", curves=_rates(), **CONV,
        )
        self.assertEqual({p.start_date for p in series}, {start})
        self.assertEqual(len({p.maturity_date for p in series}), 1)
        for p in series:
            with self.subTest(as_of=p.as_of_date):
                self.assertEqual(p.par_rate, _direct(p.as_of_date, start, tenor="5Y").target_fixrate)

    def test_imm_codes_are_fixed_dates(self):
        series = historical_swap_rate_series(
            FIXED, start="M27", end="M38", curves=_rates(), **IMM_CONV,
        )
        self.assertEqual({p.start_date for p in series}, {imm_code_to_date("M27")})

    def test_tenor_start_is_fixed_forward_date(self):
        start = forward_start_date(spot_date_for(BASE), "1Y")
        series = historical_swap_rate_series(
            FIXED, start="1y", tenor="5Y", base_date=BASE, curves=_rates(), **CONV,
        )
        self.assertEqual({p.start_date for p in series}, {start})
        for p in series:
            with self.subTest(as_of=p.as_of_date):
                self.assertEqual(p.par_rate, _direct(p.as_of_date, start, tenor="5Y").target_fixrate)

    def test_past_start_uses_only_fixings_before_as_of_date(self):
        """過去日Dの計算にはD当日以降の実績を使わない(先読みしない)。"""
        start = date(2026, 1, 6)
        fixings = _synthetic_fixings(date(2025, 12, 1), date(2026, 9, 30))  # 未来日の値も含む
        series = historical_swap_rate_series(
            FIXED, start=start, tenor="2Y", fixings=fixings, curves=_rates(), **CONV,
        )
        for p in series:
            with self.subTest(as_of=p.as_of_date):
                known = {d: v for d, v in fixings.items() if d < p.as_of_date}
                expected = _direct(p.as_of_date, start, tenor="2Y", fixings=known)
                self.assertEqual(p.par_rate, expected.target_fixrate)

    def test_past_start_without_fixings_raises_with_date(self):
        with self.assertRaisesRegex(ValueError, "2026-03-25"):
            historical_swap_rate_series(
                FIXED, start=date(2026, 1, 6), tenor="2Y", curves=_rates(), **CONV,
            )

    def test_matured_dates_are_skipped(self):
        """満期後の日付は系列から外す(エラーにしない)。"""
        fixings = _synthetic_fixings(date(2025, 12, 1), date(2026, 9, 30))
        series = historical_swap_rate_series(
            FIXED, start=date(2026, 1, 6), end=date(2026, 5, 7), fixings=fixings,
            curves=_rates(), **CONV,
        )
        self.assertEqual([p.as_of_date for p in series], [date(2026, 3, 25)])

    def test_blank_start_is_base_spot_date(self):
        """start空欄は基準日のスポット日の取引として固定する。"""
        series = historical_swap_rate_series(
            FIXED, tenor="5Y", base_date=BASE, curves=_rates(), **CONV,
        )
        self.assertEqual({p.start_date for p in series}, {spot_date_for(BASE)})
        for p in series:
            with self.subTest(as_of=p.as_of_date):
                expected = _direct(p.as_of_date, spot_date_for(BASE), tenor="5Y")
                self.assertEqual(p.par_rate, expected.target_fixrate)


class _CountingSource(_BootstrapSource):
    def __init__(self, dates):
        super().__init__(dates)
        self.loads = 0

    def load(self, as_of_date, until=None):
        self.loads += 1
        return super().load(as_of_date, until)


def _spec(**kwargs):
    return SwapSpec(**CONV, **kwargs)


class MultiLegTest(unittest.TestCase):
    """カーブ・フライの各脚をまとめて計算する historical_multi_rate_series。"""

    def test_each_leg_equals_single_series(self):
        specs = [_spec(tenor="2Y"), _spec(tenor="5Y"), _spec(tenor="10Y")]
        rows = historical_multi_rate_series(specs, ROLLING, curves=_rates())
        self.assertEqual([d for d, _ in rows], AS_OF_DATES)
        for i, tenor in enumerate(["2Y", "5Y", "10Y"]):
            single = historical_swap_rate_series(ROLLING, tenor=tenor, curves=_rates(), **CONV)
            with self.subTest(tenor=tenor):
                self.assertEqual([legs[i].par_rate for _, legs in rows], [p.par_rate for p in single])

    def test_curve_is_loaded_once_per_date(self):
        source = _CountingSource(AS_OF_DATES)
        historical_multi_rate_series(
            [_spec(tenor="2Y"), _spec(tenor="5Y"), _spec(tenor="10Y")], ROLLING, curves=source,
        )
        self.assertEqual(source.loads, len(AS_OF_DATES))

    def test_matured_leg_is_none_and_dates_stay_aligned(self):
        """FIXEDで満期後の脚だけNoneになり、日付の並びは他の脚と揃ったまま。"""
        fixings = _synthetic_fixings(date(2025, 12, 1), date(2026, 9, 30))
        specs = [_spec(start=date(2026, 1, 6), end=date(2026, 5, 7)), _spec(start=date(2027, 3, 31), tenor="5Y")]
        rows = historical_multi_rate_series(specs, FIXED, fixings=fixings, curves=_rates())
        self.assertEqual([d for d, _ in rows], AS_OF_DATES)
        self.assertEqual([legs[0] is None for _, legs in rows], [False, True, True])
        self.assertTrue(all(legs[1] is not None for _, legs in rows))

    def test_no_legs_returns_dates_only(self):
        source = _CountingSource(AS_OF_DATES)
        rows = historical_multi_rate_series([], ROLLING, from_date=date(2026, 6, 1), curves=source)
        self.assertEqual(rows, [(date(2026, 6, 25), []), (BASE, [])])
        self.assertEqual(source.loads, 0)

    def test_invalid_leg_is_reported_with_position(self):
        with self.assertRaisesRegex(ValueError, "2本目"):
            historical_multi_rate_series(
                [_spec(tenor="5Y"), _spec(tenor="5Y", end=date(2030, 1, 1))], ROLLING, curves=_rates(),
            )


class InputTest(unittest.TestCase):
    def test_unknown_mode(self):
        with self.assertRaises(ValueError):
            historical_swap_rate_series("SPOT", tenor="5Y", curves=_rates(), **CONV)

    def test_mode_is_case_insensitive(self):
        series = historical_swap_rate_series(" rolling ", tenor="5Y", curves=_rates([BASE]), **CONV)
        self.assertEqual(len(series), 1)

    def test_price_swap_raises_expired_error(self):
        with self.assertRaises(SwapExpiredError):
            _direct(BASE, date(2025, 1, 6), end=date(2026, 1, 6), fixings={})


if __name__ == "__main__":
    unittest.main()
