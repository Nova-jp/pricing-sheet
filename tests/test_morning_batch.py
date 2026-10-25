"""
日次カーブDBを作るバッチ(engine/common/morning_batch.py)のリグレッションテスト。

LSEGのデータはPublicリポジトリに置けないため、historical_data.xlsx と同じ構成
(Mid: A列「日付」の見出し行+RIC行+日付×テナー / Fixing: 日付・TONA)の合成ブックを
一時フォルダに作って確認する。

実行方法:
    python -m unittest tests.test_morning_batch -v
"""

import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import openpyxl  # noqa: E402

from engine.common import morning_batch  # noqa: E402
from engine.common.curve import bootstrap_curve  # noqa: E402
from engine.common.historical_store import HistoricalStore  # noqa: E402
from engine.swap_pricing.historical.pricer import FIXED, SwapSpec, historical_swap_rate_series  # noqa: E402
from engine.common.market_data import Market  # noqa: E402
from engine.common.swap_pricer import price_swap  # noqa: E402
from engine.common.terms import SwapTerms  # noqa: E402
from tests.test_curve_repricing import SPOT_RATES  # noqa: E402
from tests.test_past_start import CONV, CONV_ARGS, _synthetic_fixings  # noqa: E402

DAYS = [date(2026, 9, 24), date(2026, 9, 25), date(2026, 9, 28)]
TENORS = list(SPOT_RATES)  # Midシートの見出し(12M/24M/36M表記を含む)
FIXINGS = _synthetic_fixings(date(2026, 6, 1), date(2026, 9, 25))  # 9/28分は未公表


def _write_workbook(path: Path, missing=None, mid_days=DAYS):
    """missing: {日付: [欠損させるテナー]}(LSEGのエラー値 "#N/A" を入れる)。"""
    missing = missing or {}
    wb = openpyxl.Workbook()
    mid = wb.active
    mid.title = "Mid"
    mid.append(["ミッドレート ヒストリカル(%)"])
    mid.append(["期間: ..."])
    mid.append([])
    mid.append(["日付"] + TENORS)
    mid.append(["RIC"] + [f"JP{t}ONI=TRDT" for t in TENORS])
    for d in sorted(mid_days, reverse=True):  # LSEGと同じく新しい順
        mid.append([datetime(d.year, d.month, d.day)] + [
            "#N/A" if t in missing.get(d, []) else SPOT_RATES[t] for t in TENORS
        ])
    fixing = wb.create_sheet("Fixing")
    fixing.append(["TONA フィキシング(%)"])
    fixing.append([])
    fixing.append([])
    fixing.append(["日付", "TONA"])
    for d, r in sorted(FIXINGS.items(), reverse=True):
        fixing.append([datetime(d.year, d.month, d.day), r])
    wb.save(path)


def _write_config(path: Path, tenors):
    quoted = ", ".join(f'"{t}"' for t in tenors)
    path.write_text(f'builder = "convex_monotone"\ntenors = [{quoted}]\n', encoding="utf-8")


def _par5y(curve, index, as_of, spot):
    return price_swap(
        Market(as_of, curve, index),
        SwapTerms(spot, CONV, tenor="5Y"), notional=1.0, pay_rec="PAY",
    ).target_fixrate


class CurveBatchTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.xlsx, self.config, self.db = tmp / "historical_data.xlsx", tmp / "c.toml", tmp / "h.db"
        _write_workbook(self.xlsx)
        _write_config(self.config, ["O/N"] + TENORS)

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, **kwargs):
        kwargs.setdefault("skip_jgb", True)  # LSEGに接続しない(JGBの取得はモックで別に確かめる)
        kwargs.setdefault("skip_vol", True)  # 同じく(プレミアムの取得はモックで別に確かめる)
        return morning_batch.run(self.xlsx, self.config, self.db, log=lambda *_: None, **kwargs)

    def test_builds_curve_equal_to_direct_bootstrap_with_tona_as_on(self):
        summary = self._run()
        self.assertEqual(summary.built, DAYS)
        store = HistoricalStore(self.db)
        as_of = date(2026, 9, 25)
        quotes = {"O/N": FIXINGS[as_of], **SPOT_RATES}  # O/Nはその日のTONA実績
        boot = bootstrap_curve(as_of, quotes)
        self.assertIn("o/n", boot.used_tenors)
        stored = store.load(as_of)
        self.assertAlmostEqual(
            _par5y(stored.curve, stored.index, as_of, boot.spot_date),
            _par5y(boot.curve, boot.index, as_of, boot.spot_date),
            delta=1e-9,
        )

    def test_missing_on_is_skipped_and_recorded(self):
        """TONA実績が未公表の日(9/28)はO/N抜きで構築し、欠損として記録する。"""
        summary = self._run()
        self.assertEqual(summary.with_missing, {date(2026, 9, 28): ["O/N"]})

    def test_missing_tenor_builds_from_available(self):
        _write_workbook(self.xlsx, missing={date(2026, 9, 25): ["7Y", "15Y"]})
        summary = self._run()
        self.assertIn(date(2026, 9, 25), summary.built)
        self.assertEqual(summary.with_missing[date(2026, 9, 25)], ["7Y", "15Y"])
        as_of = date(2026, 9, 25)
        quotes = {"O/N": FIXINGS[as_of], **{t: r for t, r in SPOT_RATES.items() if t not in ("7Y", "15Y")}}
        boot = bootstrap_curve(as_of, quotes)
        stored = HistoricalStore(self.db).load(as_of)
        self.assertAlmostEqual(
            _par5y(stored.curve, stored.index, as_of, boot.spot_date),
            _par5y(boot.curve, boot.index, as_of, boot.spot_date),
            delta=1e-9,
        )

    def test_tenor_selection_and_incremental_rebuild(self):
        self._run()
        again = self._run()
        self.assertEqual((again.built, again.unchanged), ([], len(DAYS)))

        _write_config(self.config, [t for t in ["O/N"] + TENORS if t != "40Y"])
        changed = self._run()
        self.assertEqual(changed.built, DAYS)  # テナー選択を変えると作り直される
        self.assertEqual(self._run(rebuild=True).built, DAYS)

    def test_dates_dropped_from_workbook_are_kept_and_rebuilt_from_db(self):
        """xlsxの取得期間から外れた日も、DBの生レートから作り直せる。"""
        self._run()
        _write_workbook(self.xlsx, mid_days=DAYS[1:])  # 9/24がxlsxから外れる
        _write_config(self.config, TENORS)  # O/Nを外す
        summary = self._run()
        # 9/28は元々O/N抜き(TONA未公表)で作っていたので入力が変わらず、作り直さない
        self.assertEqual(summary.built, DAYS[:2])
        self.assertEqual(summary.unchanged, 1)
        self.assertEqual(HistoricalStore(self.db).dates(), DAYS)

    def test_historical_series_from_db(self):
        """DBのカーブとTONA実績で、過去起算のFIXED系列が計算できる。"""
        self._run()
        store = HistoricalStore(self.db)
        series = historical_swap_rate_series(
            SwapSpec(CONV, start=date(2026, 7, 1), tenor="2Y"), FIXED, fixings=store.fixings(), curves=store,
        )
        self.assertEqual([p.as_of_date for p in series], DAYS)

    def test_japanese_holidays_are_not_built(self):
        """祝日(2026-09-22 国民の休日)にLSEGの値があっても、その日のカーブは作らない。"""
        holiday = date(2026, 9, 22)
        _write_workbook(self.xlsx, mid_days=DAYS + [holiday])
        summary = self._run()
        self.assertEqual(summary.holidays, [holiday])
        self.assertNotIn(holiday, HistoricalStore(self.db).dates())

    def test_udf_historical_rates_aligns_legs(self):
        """UDF HistoricalRates: 空の行は空欄の列、各脚は1本ずつ計算した値と一致する。"""
        from unittest import mock

        from engine.common import udf_common
        from engine.swap_pricing import udf

        self._run()
        store = HistoricalStore(self.db)
        conv = list(CONV_ARGS)
        table = [["", "5y", ""] + conv, ["", "", ""] + ["", "", "", ""], ["", "10Y", ""] + conv]
        with mock.patch.object(udf_common, "_historical_store", lambda: store):
            rows = udf.HistoricalRates(table, "rolling")
            single = udf.HistoricalRate("", "10Y", "", *conv, "ROLLING")
        self.assertEqual(rows[0], ["Date", "A", "B", "C"])
        self.assertEqual([r[0] for r in rows[1:]], DAYS)
        self.assertTrue(all(r[2] == "" for r in rows[1:]))
        self.assertEqual([r[3] for r in rows[1:]], [r[1] for r in single[1:]])

    def test_udf_historical_rates_without_legs_returns_dates(self):
        from unittest import mock

        from engine.common import udf_common
        from engine.swap_pricing import udf

        self._run()
        with mock.patch.object(udf_common, "_historical_store", lambda: HistoricalStore(self.db)):
            rows = udf.HistoricalRates([[""] * 7] * 3, "FIXED")
        self.assertEqual(rows[1:], [[d, "", "", ""] for d in DAYS])

    def _premiums(self):
        from engine.common.swaption_source import PremiumQuote

        return [
            PremiumQuote(date(2026, 9, 24), "1Yx10Y", 450.0, 2.9),
            PremiumQuote(date(2026, 9, 25), "1Yx10Y", 455.0, 2.9),
            PremiumQuote(date(2026, 9, 25), "1Mx1Y", 7.0, 1.2),
            PremiumQuote(date(2026, 9, 26), "1Yx10Y", 460.0, 2.9),  # 土曜(カーブが無い)
        ]

    def test_vols_are_computed_from_premiums_and_the_curve_of_the_day(self):
        import QuantLib as ql
        from unittest import mock

        from engine.common.swaption import PremiumConverter
        from engine.common.swaption_source import FIRST_DATE

        with mock.patch.object(morning_batch.swaption_source, "fetch_premiums", return_value=self._premiums()) as fetch:
            self._run(skip_vol=False)
        self.assertEqual(fetch.call_args.args[0], FIRST_DATE)  # 初回は最初の日から
        store = HistoricalStore(self.db)
        vols = store.vol_quotes()
        self.assertEqual(sorted(vols), [date(2026, 9, 24), date(2026, 9, 25)])  # カーブの無い日は作らない
        as_of = date(2026, 9, 25)
        ql.Settings.instance().evaluationDate = ql.Date.from_date(as_of)
        expected = PremiumConverter(store.load(as_of).curve, as_of).convert("1Yx10Y", 455.0).vol
        self.assertAlmostEqual(vols[as_of]["1Yx10Y"], expected, places=12)
        self.assertEqual(set(vols[as_of]), {"1Yx10Y", "1Mx1Y"})

    def test_second_run_fetches_from_overlap_and_skips_unchanged_days(self):
        from datetime import timedelta
        from unittest import mock

        with mock.patch.object(morning_batch.swaption_source, "fetch_premiums", return_value=self._premiums()):
            self._run(skip_vol=False)
        logs = []
        with mock.patch.object(morning_batch.swaption_source, "fetch_premiums", return_value=[]) as fetch:
            morning_batch.run(self.xlsx, self.config, self.db, log=logs.append, skip_jgb=True, skip_vol=False)
        self.assertEqual(fetch.call_args.args[0],
                         date(2026, 9, 26) - timedelta(days=morning_batch.PREMIUM_OVERLAP_DAYS))
        self.assertIn("ボラ計算: 新規/更新 0日 / 変更なし 2日", "\n".join(logs))

    def test_old_vol_rows_without_premiums_are_removed(self):
        from unittest import mock

        HistoricalStore(self.db).save_vol_quotes({date(2026, 9, 28): {"1Yx10Y": 66.0}})  # 以前の =R の値
        with mock.patch.object(morning_batch.swaption_source, "fetch_premiums", return_value=self._premiums()):
            self._run(skip_vol=False)
        self.assertNotIn(date(2026, 9, 28), HistoricalStore(self.db).vol_quotes())

    def test_premium_fetch_failure_does_not_abort_the_batch(self):
        from unittest import mock

        with mock.patch.object(morning_batch.swaption_source, "fetch_premiums",
                               side_effect=RuntimeError("Workspaceに繋がりません")):
            summary = self._run(skip_vol=False)
        self.assertEqual(summary.built, DAYS)
        self.assertEqual(HistoricalStore(self.db).vol_quotes(), {})

    def test_empty_workbook_is_rejected(self):
        _write_workbook(self.xlsx, mid_days=[])
        with self.assertRaisesRegex(ValueError, "Midシートにデータがありません"):
            self._run()

    def test_asw_quotes_are_synced_from_neon(self):
        from unittest import mock

        from engine.common.historical_store import AswQuoteRow

        rows = [AswQuoteRow(date(2026, 10, 9), "000190054", 0.448, 0.4804, datetime(2026, 10, 9, 12, 30, 6))]
        with mock.patch.object(morning_batch.neon_source, "fetch_asw_quotes", return_value=rows) as fetch:
            summary = self._run()
        fetch.assert_called_once_with(since=None)  # 初回は差分の基準が無い=全件
        self.assertEqual(summary.built, DAYS)  # ASW取り込みはカーブ構築に影響しない
        self.assertEqual(
            HistoricalStore(self.db).asw_quotes(),
            {date(2026, 10, 9): {"000190054": (0.448, 0.4804)}},
        )

    def test_second_run_passes_watermark_as_since(self):
        from unittest import mock

        from engine.common.historical_store import AswQuoteRow

        rows = [AswQuoteRow(date(2026, 10, 9), "000190054", 0.448, 0.4804, datetime(2026, 10, 9, 12, 30, 6))]
        with mock.patch.object(morning_batch.neon_source, "fetch_asw_quotes", return_value=rows):
            self._run()
        with mock.patch.object(morning_batch.neon_source, "fetch_asw_quotes", return_value=[]) as fetch:
            self._run()
        fetch.assert_called_once_with(since=datetime(2026, 10, 9, 12, 30, 6))

    def test_neon_failure_does_not_abort_the_batch(self):
        from unittest import mock

        with mock.patch.object(
            morning_batch.neon_source, "fetch_asw_quotes", side_effect=RuntimeError("接続できません"),
        ):
            summary = self._run()  # 例外を外に投げず、カーブ構築は通常通り完了する
        self.assertEqual(summary.built, DAYS)
        self.assertEqual(HistoricalStore(self.db).asw_quotes(), {})

    def test_jgbs_are_synced_from_lseg(self):
        from unittest import mock

        from engine.common.jgb_source import JgbClose, JgbRecord

        bonds = [JgbRecord("JB380", "JP03800067=", "JP1103801RA7", 1.7, date(2035, 9, 20))]
        closes = [JgbClose(date(2026, 10, 16), "JB380", 2.94)]
        with mock.patch.object(morning_batch.jgb_source, "fetch_jgbs", return_value=(bonds, closes)):
            summary = self._run(skip_jgb=False)
        self.assertEqual(summary.built, DAYS)
        store = HistoricalStore(self.db)
        self.assertEqual(store.jgb_bond("JB380"), tuple(bonds[0]))
        self.assertEqual(store.jgb_close("JB380", date(2026, 10, 19)), (date(2026, 10, 16), 2.94))
        self.assertIsNone(store.jgb_close("JB380", date(2026, 10, 15)))  # その日以前の引け値だけ

    def test_lseg_failure_does_not_abort_the_batch(self):
        from unittest import mock

        with mock.patch.object(
            morning_batch.jgb_source, "fetch_jgbs", side_effect=RuntimeError("Workspaceに繋がりません"),
        ):
            summary = self._run(skip_jgb=False)
        self.assertEqual(summary.built, DAYS)
        self.assertIsNone(HistoricalStore(self.db).jgb_bond("JB380"))


if __name__ == "__main__":
    unittest.main()
