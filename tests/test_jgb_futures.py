"""
長期国債先物(engine/common/jgb_futures.py・futures_source.py)と、引け値の過去分(jgb_source.fetch_close_history)、
朝バッチの先物の取り込みのテスト。

交換係数・受渡日・取引最終日はJPXの告示(公開情報)の値と照らし合わせる。ベーシスは架空の銘柄・価格で、
式の性質(ネット=先渡単価−F×CF、ネット0ならIRR=レポ、CTD=ネットベーシス最小)を確かめる。
LSEGには接続しない(取得部分はモック)。

実行方法:
    python -m unittest tests.test_jgb_futures -v
"""

import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from engine.common import bond, futures_source, jgb_source, morning_batch  # noqa: E402
from engine.common import jgb_futures as jf  # noqa: E402
from engine.common.historical_store import HistoricalStore  # noqa: E402
from engine.common.jgb_source import JgbClose, JgbRecord  # noqa: E402


class FakeColumn:
    def __init__(self, values):
        self.values = values

    def items(self):
        return self.values.items()


class FakeHistory:
    """ld.get_history の結果の代わり(列 → {日付: 値})。"""

    def __init__(self, columns):
        self._columns = columns

    @property
    def columns(self):
        return list(self._columns)

    def __getitem__(self, key):
        return FakeColumn(self._columns[key])


class FakeFrame:
    def __init__(self, records):
        self.records = records

    def to_dict(self, orient):
        return self.records


class ContractTest(unittest.TestCase):
    def test_delivery_and_last_trade_dates_match_jpx(self):
        for contract, delivery, last_trade in (
            (jf.Contract(2020, 12), date(2020, 12, 21), date(2020, 12, 14)),
            (jf.Contract(2021, 9), date(2021, 9, 21), date(2021, 9, 13)),  # 9/20 敬老の日
            (jf.Contract(2026, 9), date(2026, 9, 24), date(2026, 9, 14)),  # 9/20〜23 休日
            (jf.Contract(2026, 12), date(2026, 12, 21), date(2026, 12, 14)),
        ):
            self.assertEqual(jf.delivery_date(contract), delivery)
            self.assertEqual(jf.last_trade_date(contract), last_trade)

    def test_ric_and_parse(self):
        self.assertEqual(jf.Contract(2026, 12).ric(listed=True), "JGBZ6")
        self.assertEqual(jf.Contract(2026, 9).ric(listed=False), "JGBU6^2")
        self.assertEqual(jf.Contract(2020, 3).ric(listed=False), "JGBH0^2")
        self.assertEqual(jf.Contract.parse("JGBZ6"), jf.Contract(2026, 12))
        self.assertEqual(jf.Contract.parse("JGBU6^2"), jf.Contract(2026, 9))
        self.assertEqual(jf.Contract.parse("2027-03"), jf.Contract(2027, 3))
        self.assertEqual(jf.Contract(2027, 3).label, "2027-03")
        with self.assertRaises(ValueError):
            jf.Contract.parse("2026-11")

    def test_front_contract_rolls_after_last_trade_date(self):
        self.assertEqual(jf.front_contract(date(2026, 12, 14)), jf.Contract(2026, 12))
        self.assertEqual(jf.front_contract(date(2026, 12, 15)), jf.Contract(2027, 3))
        self.assertEqual(jf.front_contract(date(2026, 10, 23)), jf.Contract(2026, 12))
        self.assertEqual(jf.front_contract(date(2026, 9, 15)), jf.Contract(2026, 12))

    def test_contracts_between(self):
        self.assertEqual(
            jf.contracts_between(jf.Contract(2026, 9), jf.Contract(2027, 3)),
            [jf.Contract(2026, 9), jf.Contract(2026, 12), jf.Contract(2027, 3)],
        )


class ConversionFactorTest(unittest.TestCase):
    # JPXの告示(取引開始・取引最終日の到来等)の受渡適格銘柄及び交換比率から
    LONG_2021_06 = (  # 長期国債先物 2021年6月限、利率0.1%
        (date(2028, 6, 20), 0.666765), (date(2028, 9, 20), 0.657226), (date(2028, 12, 20), 0.647830),
        (date(2029, 3, 20), 0.638569), (date(2029, 12, 20), 0.611599), (date(2030, 12, 20), 0.577447),
    )

    def test_matches_jpx_long_term(self):
        for maturity, expected in self.LONG_2021_06:
            self.assertEqual(jf.conversion_factor(0.1, maturity, jf.Contract(2021, 6)), expected, maturity)

    def test_matches_jpx_with_other_coupons_and_standard(self):
        # 超長期国債先物(標準物3%)2021年6月限・9月限: 利率の違う銘柄で式のクーポンの項を確かめる
        self.assertEqual(jf.conversion_factor(0.4, date(2040, 9, 20), jf.Contract(2021, 6), 3.0), 0.621880)
        self.assertEqual(jf.conversion_factor(0.5, date(2040, 12, 20), jf.Contract(2021, 6), 3.0), 0.632942)
        self.assertEqual(jf.conversion_factor(0.5, date(2040, 12, 20), jf.Contract(2021, 9), 3.0), 0.636422)
        # 中期国債先物(標準物3%)2021年9月限
        self.assertEqual(jf.conversion_factor(0.005, date(2026, 3, 20), jf.Contract(2021, 9), 3.0), 0.874801)

    def test_months_count_from_nominal_20th(self):
        # 2026年12月限の受渡日は12/21だが、月数は名目の12/20から数える(7年ちょうど=84か月)
        self.assertEqual(jf.months_to_maturity(jf.Contract(2026, 12), date(2033, 12, 20)), 84)
        self.assertEqual(jf.months_to_maturity(jf.Contract(2026, 12), date(2033, 12, 19)), 83)

    def test_deliverable_basket(self):
        c = jf.Contract(2026, 12)
        self.assertTrue(jf.is_deliverable("JB373", date(2033, 12, 20), c))  # 7年ちょうどは含む
        self.assertFalse(jf.is_deliverable("JB372", date(2033, 9, 20), c))
        self.assertTrue(jf.is_deliverable("JB388", date(2037, 9, 20), c))
        self.assertFalse(jf.is_deliverable("JB389", date(2037, 12, 20), c))  # 11年ちょうどは含まない
        self.assertFalse(jf.is_deliverable("JL180", date(2034, 3, 20), c))  # 10年債だけ


class BasisTest(unittest.TestCase):
    C = jf.Contract(2027, 3)
    AS_OF = date(2026, 12, 1)
    A = jf.BondQuote("JB901", 0.5, date(2034, 3, 20), 2.0)  # 架空の銘柄
    B = jf.BondQuote("JB902", 3.0, date(2034, 6, 20), 1.651)

    def test_net_basis_is_forward_minus_invoice(self):
        row = jf.basis_row(self.C, 131.0, self.AS_OF, self.A, 1.0)
        settle = bond.settlement_date(self.AS_OF, 1)
        jgb = bond.Jgb(self.A.coupon, self.A.maturity)
        price = bond.price_from_simple_yield(jgb, self.A.simple_yield, settle)
        forward = bond.forward_price(jgb, price, settle, jf.delivery_date(self.C), 1.0)
        invoice = 131.0 * row.conversion_factor
        self.assertEqual(row.price, price)
        self.assertAlmostEqual(row.gross_basis, price - invoice, places=12)
        self.assertAlmostEqual(row.net_basis, forward.clean - invoice, places=12)
        self.assertAlmostEqual(row.carry, row.gross_basis - row.net_basis, places=12)

    def test_implied_repo_equals_repo_when_net_basis_is_zero(self):
        # 期中に利払(3/20)をまたぐ銘柄で、ネットベーシスが0になる先物価格ならIRR=レポ
        cf = jf.conversion_factor(self.A.coupon, self.A.maturity, self.C)
        row0 = jf.basis_row(self.C, 131.0, self.AS_OF, self.A, 0.8)
        futures = (row0.net_basis + 131.0 * cf) / cf  # 先渡単価 / CF
        row = jf.basis_row(self.C, futures, self.AS_OF, self.A, 0.8)
        self.assertAlmostEqual(row.net_basis, 0.0, places=10)
        self.assertAlmostEqual(row.implied_repo, 0.8, places=9)

    def test_cheapest_is_min_net_basis_even_if_irr_disagrees(self):
        rows = jf.basis_table(self.C, 131.0, self.AS_OF, [self.A, self.B], 1.0)
        by_code = {r.code: r for r in rows}
        self.assertLess(by_code["JB901"].net_basis, by_code["JB902"].net_basis)
        self.assertLess(by_code["JB901"].implied_repo, by_code["JB902"].implied_repo)  # IRRでは逆の順位
        self.assertEqual(jf.cheapest(rows).code, "JB901")

    def test_basis_table_keeps_only_deliverables(self):
        outside = jf.BondQuote("JB850", 0.1, date(2030, 3, 20), 1.0)
        rows = jf.basis_table(self.C, 131.0, self.AS_OF, [self.B, outside, self.A], 1.0)
        self.assertEqual([r.code for r in rows], ["JB901", "JB902"])
        self.assertIsNone(jf.cheapest([]))

    def test_rejects_settle_after_delivery(self):
        with self.assertRaises(ValueError):
            jf.basis_row(self.C, 131.0, date(2027, 3, 19), self.A, 1.0)


class RepoStepsTest(unittest.TestCase):
    @staticmethod
    def _days(start: date, n: int):
        out, d = [], start
        while len(out) < n:
            if d.weekday() < 5:
                out.append(d)
            d += timedelta(days=1)
        return out

    def test_steps_only_at_level_changes(self):
        days = self._days(date(2024, 1, 1), 60)
        fixings = {}
        for i, d in enumerate(days):
            if i < 30:
                fixings[d] = -0.02 + (0.03 if i in (10, 11) else 0.0) + (0.005 if i % 3 == 0 else 0.0)  # 2日の跳ね
            else:
                fixings[d] = 0.227 + (0.002 if i % 3 == 0 else 0.0)
        steps = jf.repo_steps(fixings)
        self.assertEqual([s.start for s in steps], [days[0], days[30]])
        self.assertAlmostEqual(steps[0].rate, -0.02, places=3)
        self.assertAlmostEqual(steps[1].rate, 0.227, places=3)

    def test_repo_on(self):
        steps = [jf.RepoStep(date(2025, 1, 27), 0.477), jf.RepoStep(date(2024, 10, 28), 0.227)]
        self.assertIsNone(jf.repo_on(steps, date(2024, 10, 25)))
        self.assertEqual(jf.repo_on(steps, date(2024, 10, 28)), 0.227)
        self.assertEqual(jf.repo_on(steps, date(2025, 1, 24)), 0.227)
        self.assertEqual(jf.repo_on(steps, date(2026, 1, 1)), 0.477)
        self.assertEqual(jf.repo_steps({}), [])


class FrontSeriesTest(unittest.TestCase):
    def test_change_is_within_the_same_contract_at_roll(self):
        z6, h7 = jf.Contract(2026, 12), jf.Contract(2027, 3)
        settles = {
            z6: {date(2026, 12, 11): 126.0, date(2026, 12, 14): 126.2},
            h7: {date(2026, 12, 11): 125.0, date(2026, 12, 14): 125.1, date(2026, 12, 15): 125.4},
        }
        rows = jf.front_series(settles)
        self.assertEqual([(r.as_of, r.contract) for r in rows], [
            (date(2026, 12, 11), z6), (date(2026, 12, 14), z6), (date(2026, 12, 15), h7),
        ])
        self.assertIsNone(rows[0].change)
        self.assertAlmostEqual(rows[1].change, 0.2)
        self.assertAlmostEqual(rows[2].change, 0.3)  # 125.1 → 125.4(126.2 → 125.4 の段差は入れない)
        self.assertEqual([r.rolled for r in rows], [False, False, True])


class SourceTest(unittest.TestCase):
    BONDS = [
        JgbRecord("JB373", "JP03730067=", "", 0.6, date(2033, 12, 20)),
        JgbRecord("JB374", "JP03740067=", "", 0.8, date(2034, 3, 20)),
    ]

    def test_close_history_fills_missing_days_from_other_sources(self):
        d1, d2, d3 = date(2026, 10, 21), date(2026, 10, 22), date(2026, 10, 23)
        data = {
            "B_YLD_1:DWAT": {"JP03730067=DWAT": {d1: 2.71, d3: 2.6}, "JP03740067=DWAT": {d1: 2.75}},
            "B_YLD_1:MZST": {"JP03730067=MZST": {d1: 9.9, d2: 2.695}, "JP03740067=MZST": {}},
            "MID_YLD_1:JCL": {"JP03730067=JCL": {d2: 8.8}, "JP03740067=JCL": {d2: 2.74, d3: float("nan")}},
        }

        def get_history(rics, fields, start, end):
            key = f"{fields[0]}:{rics[0].split('=')[1]}"
            return FakeHistory({r: data[key][r] for r in rics})

        ld = mock.Mock()
        ld.get_history.side_effect = get_history
        closes = jgb_source._fetch_close_history(ld, self.BONDS, d1, d3)
        self.assertEqual(closes, [
            JgbClose(d1, "JB373", 2.71), JgbClose(d1, "JB374", 2.75),  # 大和を優先
            JgbClose(d2, "JB373", 2.695), JgbClose(d2, "JB374", 2.74),  # 大和に無い日はみずほ→JCL
            JgbClose(d3, "JB373", 2.6),
        ])

    def test_history_columns_single_ric(self):
        df = FakeHistory({"B_YLD_1": {date(2026, 10, 23): 2.6}})
        cols = jgb_source.history_columns(df, ["JP03730067=DWAT"], "B_YLD_1")
        self.assertEqual(list(cols), ["JP03730067=DWAT"])

    def test_fetch_settles_uses_listed_or_expired_ric_and_checks_dates(self):
        ld = mock.Mock()
        ld.get_data.return_value = FakeFrame([
            {"Instrument": "JGBZ6", "DELIV_DATE": date(2026, 12, 21), "LSTTRDDATE": date(2026, 12, 14)},
            {"Instrument": "JGBH7", "DELIV_DATE": date(2027, 3, 23), "LSTTRDDATE": date(2027, 3, 12)},  # 最終日だけ食い違い
        ])
        calls = []

        def get_history(ric, fields, start, end):
            calls.append((ric, start))
            return FakeHistory({"SETTLE": {date(2026, 9, 14): 125.82}} if ric == "JGBU6^2" else {"SETTLE": {}})

        ld.get_history.side_effect = get_history
        result = futures_source._fetch(
            ld, {jf.Contract(2026, 9): date(2026, 9, 1), jf.Contract(2026, 12): date(2026, 10, 1)}, date(2026, 10, 23),
        )
        self.assertEqual(calls, [("JGBU6^2", "2026-09-01"), ("JGBZ6", "2026-10-01")])
        self.assertEqual(result.settles, [(date(2026, 9, 14), "2026-09", 125.82)])
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("2027-03限の取引最終日", result.warnings[0])


class BatchTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = HistoricalStore(Path(self._tmp.name) / "historical.db")

    def tearDown(self):
        self._tmp.cleanup()

    def test_sync_futures_fetches_only_unfinished_contracts(self):
        self.store.save_fut_settles([(date(2026, 9, 14), "2026-09", 125.82), (date(2026, 10, 20), "2026-12", 125.81)])
        fetched = futures_source.FuturesFetch([(date(2026, 10, 23), "2026-12", 126.63)], [], ["警告の例"])
        logs = []
        with mock.patch.object(morning_batch.futures_source, "fetch_settles", return_value=fetched) as fetch:
            morning_batch.sync_futures(self.store, logs.append, today=date(2026, 10, 25))
        plan, end = fetch.call_args.args
        self.assertEqual(end, date(2026, 10, 25))
        self.assertNotIn(jf.Contract(2026, 9), plan)  # 取引最終日まで取り終えた
        self.assertEqual(plan[jf.Contract(2026, 12)], date(2026, 10, 20))  # 保存済みの最終日から
        self.assertEqual(plan[jf.Contract(2020, 3)], morning_batch.FUTURES_HISTORY_START)
        self.assertEqual(max(plan), jf.Contract(2027, 6))  # 期近から3限月まで
        self.assertEqual(self.store.fut_settles()["2026-12"][date(2026, 10, 23)], 126.63)
        self.assertEqual(self.store.fut_last_dates()["2026-12"], date(2026, 10, 23))
        self.assertTrue(any("警告の例" in x for x in logs))

    def test_sync_futures_failure_is_skipped(self):
        logs = []
        with mock.patch.object(morning_batch.futures_source, "fetch_settles", side_effect=RuntimeError("繋がりません")):
            morning_batch.sync_futures(self.store, logs.append, today=date(2026, 10, 25))
        self.assertEqual(self.store.fut_settles(), {})
        self.assertTrue(any("スキップ" in x for x in logs))

    def test_sync_jgb_history_targets_10y_bonds(self):
        self.store.save_jgb_bonds([
            JgbRecord("JB373", "JP03730067=", "", 0.6, date(2033, 12, 20)),
            JgbRecord("JL180", "JP01800069=", "", 0.8, date(2042, 3, 20)),
        ])
        closes = [JgbClose(date(2020, 1, 6), "JB373", 0.0), JgbClose(date(2020, 1, 7), "JB373", 0.005)]
        with mock.patch.object(morning_batch.jgb_source, "fetch_close_history", return_value=closes) as fetch:
            morning_batch.sync_jgb_history(self.store, date(2020, 1, 1), lambda *_: None, end=date(2026, 10, 23))
        bonds, start, end = fetch.call_args.args
        self.assertEqual([b.code for b in bonds], ["JB373"])
        self.assertEqual((start, end), (date(2020, 1, 1), date(2026, 10, 23)))
        self.assertEqual(
            self.store.jgb_closes_between(date(2020, 1, 1), date(2020, 1, 31), "JB"),
            {date(2020, 1, 6): {"JB373": 0.0}, date(2020, 1, 7): {"JB373": 0.005}},
        )


if __name__ == "__main__":
    unittest.main()
