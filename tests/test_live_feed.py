"""
リアルタイムのフィード(engine/common/live_feed.py)のリグレッションテスト。LSEGには接続しない
(接続部分を差し替え、LSEGのコールバックと同じ形の値を渡す)。

実行方法:
    python -m unittest tests.test_live_feed -v
"""

import logging
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from engine.common import live_feed  # noqa: E402
from engine.common.live_feed import DatasetFeed, LiveFeed, load_config, parse_config  # noqa: E402
from engine.common.live_store import FEED_OK, FEED_STOPPED, LiveStore  # noqa: E402

T0 = datetime(2026, 10, 19, 10, 0, 0)

CONFIG = {
    "timing": {"write_interval_sec": 1, "heartbeat_sec": 2, "stale_after_sec": 10, "resubscribe_after_sec": 30},
    "datasets": [
        {"name": "swap", "kind": "ois", "rows": [["O/N", "JP1DONI=TRDT"], ["M1", "JPBOJ1ONI=TRDT"]]},
        {"name": "vol", "kind": "vol", "curve": "swap", "expiries": ["1M"], "tenors": ["1Y", "2Y"]},
    ],
}
R1, R2 = "JPY1M1YTOATM=TRDT", "JPY1M2YTOATM=TRDT"


class _FakeVolCalculator:
    """プレミアムの10分の1をvolとして返す(配線だけを確かめる)。fail に文字列を入れるとその内容で失敗する。"""

    def __init__(self):
        self.fail = None
        self.calls = []

    def rows(self, premium_rows, swap_rows, trade_date):
        self.calls.append((premium_rows, swap_rows, trade_date))
        if self.fail:
            raise ValueError(self.fail)
        return [[label, ric, None if p is None else p / 10] for label, ric, p in premium_rows]


def at(seconds):
    return T0 + timedelta(seconds=seconds)


class ConfigTest(unittest.TestCase):
    def test_repository_config_matches_the_workbooks(self):
        # ブックが引き当てる表記(MainシートA列のTicker、VolPCA/BondOptionのRIC)と同じ並び
        config = load_config(live_feed.DEFAULT_CONFIG)
        swap = {d.name: d for d in config.datasets}["swap"]
        vol = {d.name: d for d in config.datasets}["vol"]
        self.assertEqual(len(swap.rows), 44)
        self.assertEqual(swap.rows[0], ("O/N", "JP1DONI=TRDT"))
        self.assertEqual(swap.rows[33], ("40Y", "JP40YONI=TRDT"))
        self.assertEqual(swap.rows[34:36], (("RD", "JPBOJRDONI=TRDT"), ("M1", "JPBOJ1ONI=TRDT")))
        self.assertEqual(len(vol.rows), 14 * 12)  # Traditionに無い6Y・9Y満期は除いた
        self.assertEqual(vol.rows[1], ("1Mx2Y", "JPY1M2YTOATM=TRDT"))
        self.assertEqual(vol.rows[-1], ("20Yx20Y", "JPY20Y20YTOATM=TRDT"))
        self.assertEqual(vol.curve, "swap")
        self.assertEqual(config.timing.stale_after_sec, 10)

    def test_defaults_when_timing_is_omitted(self):
        config = parse_config({"datasets": CONFIG["datasets"]})
        self.assertEqual(config.timing, live_feed.Timing())

    def test_invalid_settings_are_rejected(self):
        def with_timing(**kw):
            return {"timing": dict(CONFIG["timing"], **kw), "datasets": CONFIG["datasets"]}

        for raw, pattern in (
            (with_timing(stale_after_sec=3), "2倍以上"),
            (with_timing(heartbeat_sec=0), "正の秒数"),
            (with_timing(write_interval_sec=10), "write_interval_sec"),
            (with_timing(stale_after="10"), "知らない項目"),
            (with_timing(heartbeat_sec="x"), "秒数"),
            ({"datasets": []}, "datasets"),
            ({"datasets": [CONFIG["datasets"][0], CONFIG["datasets"][0]]}, "重複"),
            ({"datasets": [{"name": "swap", "kind": "fx", "rows": [["a", "b"]]}]}, "kind"),
            ({"datasets": [{"name": "swap", "kind": "ois", "rows": [["O/N"]]}]}, "rows"),
            ({"datasets": [{"name": "swap", "kind": "ois", "rows": [["a", "X="], ["b", "X="]]}]}, "RIC"),
            ({"datasets": [{"name": "vol", "kind": "vol", "expiries": ["1M"]}]}, "tenors"),
            ({"datasets": [dict(CONFIG["datasets"][1], curve="")]}, "curve"),
            ({"datasets": [CONFIG["datasets"][0], dict(CONFIG["datasets"][1], curve="vol")]}, "curve"),
        ):
            with self.subTest(pattern=pattern), self.assertRaisesRegex(ValueError, pattern):
                parse_config(raw)


class DatasetFeedTest(unittest.TestCase):
    def setUp(self):
        self.config = parse_config(CONFIG)
        self.swap = DatasetFeed(self.config.datasets[0])
        self.vol = DatasetFeed(self.config.datasets[1])

    def test_ois_rows_are_mid_and_dates(self):
        self.swap.on_refresh({"BID": 1.207, "ASK": 1.247, "GV1_DATE": None, "GV2_DATE": None}, "JP1DONI=TRDT", now=at(0))
        self.swap.on_refresh({"BID": 1.22, "ASK": 1.26, "GV1_DATE": "2026-11-02", "GV2_DATE": "2026-12-18"},
                             "JPBOJ1ONI=TRDT", now=at(1))
        self.assertEqual(self.swap.rows(), [
            ["O/N", (1.207 + 1.247) / 2, None, None],
            ["M1", (1.22 + 1.26) / 2, datetime(2026, 11, 2), datetime(2026, 12, 18)],
        ])
        self.assertEqual(self.swap.last_tick_at, at(1))

    def test_update_merges_fields_and_one_sided_quote_is_blank(self):
        self.swap.on_refresh({"BID": 1.2, "ASK": 1.3}, "JP1DONI=TRDT")
        self.swap.on_update({"BID": 1.25}, "JP1DONI=TRDT", now=at(5))
        self.assertEqual(self.swap.rows()[0][1], (1.25 + 1.3) / 2)
        self.assertEqual(self.swap.last_tick_at, at(5))
        self.swap.on_update({"ASK": None}, "JP1DONI=TRDT")
        self.assertIsNone(self.swap.rows()[0][1])
        # まだ何も届いていない銘柄は空欄
        self.assertEqual(self.swap.rows()[1], ["M1", None, None, None])

    def test_bad_status_blanks_the_row_until_refreshed(self):
        self.vol.on_refresh({"PRIMACT_1": 28.81}, R1)
        self.vol.on_refresh({"PRIMACT_1": 35.72}, R2)
        self.vol.take_dirty()
        self.vol.on_status({"State": {"Stream": "Open", "Data": "Suspect", "Text": "stale"}}, R1)
        self.assertTrue(self.vol.take_dirty())
        self.assertEqual(self.vol.rows(), [["1Mx1Y", R1, None], ["1Mx2Y", R2, 35.72]])  # プレミアムのまま
        self.assertIn("Suspect", self.vol.bad()[R1])
        self.assertFalse(self.vol.all_bad())
        # 回復(refresh)すれば値に戻る
        self.vol.on_refresh({"PRIMACT_1": 29.0}, R1)
        self.assertEqual(self.vol.rows()[0][2], 29.0)
        self.assertEqual(self.vol.bad(), {})

    def test_not_found_ric_is_bad(self):
        self.vol.on_status({"State": {"Data": "Suspect", "Stream": "Closed", "Code": "NotFound",
                                      "Text": "***The record could not be found"}}, R1)
        self.vol.on_status({"Stream": "Closed", "Data": "Suspect"}, R2)  # Stateで包まれていない形
        self.assertTrue(self.vol.all_bad())
        self.assertEqual([r[2] for r in self.vol.rows()], [None, None])


class _FakeConnection:
    def __init__(self, feed):
        self.closed = False
        swap, vol = feed.datasets["swap"], feed.datasets["vol"]
        swap.on_refresh({"BID": 1.2, "ASK": 1.3}, "JP1DONI=TRDT", now=at(0))
        vol.on_refresh({"PRIMACT_1": 28.81}, R1, now=at(0))

    def close(self):
        self.closed = True


class LiveFeedTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = LiveStore(Path(self._tmp.name) / "live.db")
        self.connections = []

        def connect(feed):
            conn = _FakeConnection(feed)
            self.connections.append(conn)
            return conn

        self.vol_calc = _FakeVolCalculator()
        self.feed = LiveFeed(parse_config(CONFIG), self.store, connect=connect, pid=111, vol_calculator=self.vol_calc)
        logging.disable(logging.CRITICAL)  # 接続の失敗・開き直しのログを出さない

    def tearDown(self):
        logging.disable(logging.NOTSET)
        self._tmp.cleanup()

    def states(self):
        return {s.dataset: s.state for s in self.store.feed_statuses()}

    def test_connecting_is_not_readable_then_ok_after_connect(self):
        self.feed.step(at(0))
        self.assertEqual(self.states(), {"swap": "connecting", "vol": "connecting"})
        with self.assertRaisesRegex(ValueError, "connecting"):
            self.store.read_live("swap", "PricingSheet.xlsm", "Main", now=at(0))

        # 接続中の「接続できた」イベントでは ok にしない(全銘柄の最初の値が揃ってから)
        self.feed._connecting = True
        self.feed.on_connection_event(FEED_OK, "")
        self.feed._connecting = False
        self.feed._reconnect()
        self.feed.step(at(2))
        self.assertEqual(self.states(), {"swap": FEED_OK, "vol": FEED_OK})
        rows = self.store.read_live("swap", "PricingSheet.xlsm", "Main", now=at(2)).rows
        self.assertEqual(rows, [["O/N", 1.25, None, None], ["M1", None, None, None]])

    def test_values_are_written_at_most_every_write_interval(self):
        self.feed._reconnect()
        self.feed.step(at(0))
        swap = self.feed.datasets["swap"]
        swap.on_update({"BID": 1.22}, "JP1DONI=TRDT", now=at(0.2))
        self.feed.step(at(0.5))  # 書く間隔(1秒)の前なので書かない
        self.assertEqual(self.store.read("swap").rows[0][1], 1.25)
        self.feed.step(at(1.0))
        self.assertEqual(self.store.read("swap").rows[0][1], (1.22 + 1.3) / 2)
        self.assertEqual(self.store.read("swap").updated_at, at(1))
        # 値が変わらなければ「最後に変わった時刻」は進まないが、ハートビートは進む
        self.feed.step(at(4))
        self.assertEqual(self.store.read("swap").updated_at, at(1))
        self.assertEqual({s.heartbeat_at for s in self.store.feed_statuses()}, {at(4)})
        self.assertEqual({s.last_tick_at for s in self.store.feed_statuses()}, {at(0.2), at(0)})

    def test_vol_is_calculated_from_premiums_and_swap_rows(self):
        self.feed._reconnect()
        self.feed.step(at(0))
        self.assertEqual(self.store.read("vol").rows, [["1Mx1Y", R1, 2.881], ["1Mx2Y", R2, None]])
        premiums, swap_rows, trade_date = self.vol_calc.calls[-1]
        self.assertEqual(swap_rows[0][:2], ["O/N", 1.25])
        self.assertEqual(trade_date, T0.date())
        # スワップだけが動いても計算し直す。どちらも動かなければ計算しない
        n = len(self.vol_calc.calls)
        self.feed.datasets["swap"].on_update({"BID": 1.22}, "JP1DONI=TRDT", now=at(0.5))
        self.feed.step(at(1))
        self.assertEqual(len(self.vol_calc.calls), n + 1)
        self.feed.step(at(2))
        self.assertEqual(len(self.vol_calc.calls), n + 1)

    def test_vol_failure_blanks_values_and_is_reported(self):
        self.feed._reconnect()
        self.vol_calc.fail = "カーブを組めません"
        self.feed.step(at(0))
        self.assertEqual([r[2] for r in self.store.read("vol").rows], [None, None])
        status = {s.dataset: s for s in self.store.feed_statuses()}["vol"]
        self.assertEqual(status.state, FEED_OK)
        self.assertIn("カーブを組めません", status.message)
        self.vol_calc.fail = None
        self.feed.datasets["vol"].on_update({"PRIMACT_1": 30.0}, R1, now=at(1))
        self.feed.step(at(2))
        self.assertEqual(self.store.read("vol").rows[0][2], 3.0)
        self.assertEqual({s.dataset: s for s in self.store.feed_statuses()}["vol"].message, "")

    def test_disconnect_then_resubscribe_after_timeout(self):
        self.feed._reconnect()
        self.feed.step(at(0))
        self.feed.on_connection_event("reconnecting", "StreamDisconnected")
        self.feed.step(at(2))
        self.assertEqual(self.states()["swap"], "reconnecting")
        with self.assertRaisesRegex(ValueError, "StreamDisconnected"):
            self.store.read_live("swap", "PricingSheet.xlsm", "Main", now=at(2))

        reconnects = []
        self.feed.connect_async = lambda: reconnects.append(True)
        self.feed.step(at(30))  # 切れてから28秒: まだライブラリの再接続を待つ
        self.assertEqual(reconnects, [])
        self.feed.step(at(32))  # 30秒たったので開き直す
        self.assertEqual(reconnects, [True])

        self.feed._reconnect()
        self.assertTrue(self.connections[0].closed)
        self.feed.step(at(34))
        self.assertEqual(self.states()["swap"], FEED_OK)

    def test_all_rics_bad_is_suspect(self):
        self.feed._reconnect()
        vol = self.feed.datasets["vol"]
        for ric in vol.dataset.rics:
            vol.on_status({"State": {"Stream": "Open", "Data": "Suspect"}}, ric)
        self.feed.step(at(0))
        self.assertEqual(self.states(), {"swap": FEED_OK, "vol": "suspect"})

    def test_connect_failure_is_retried(self):
        def fail(feed):
            raise ConnectionError("Workspaceが起動していません")

        self.feed._connect = fail
        self.feed._reconnect()
        self.feed.step(at(0))
        status = {s.dataset: s for s in self.store.feed_statuses()}["swap"]
        self.assertEqual(status.state, "reconnecting")
        self.assertIn("Workspace", status.message)
        reconnects = []
        self.feed.connect_async = lambda: reconnects.append(True)
        self.feed.step(at(30))
        self.assertEqual(reconnects, [True])

    def test_stop_marks_stopped_and_single_instance(self):
        self.feed._reconnect()
        self.feed.step(at(0))
        other = LiveFeed(parse_config(CONFIG), self.store, connect=_FakeConnection, pid=222,
                         vol_calculator=_FakeVolCalculator())
        with self.assertRaisesRegex(SystemExit, "pid 111"):
            other.check_single_instance(at(5))
        other.check_single_instance(at(11))  # ハートビートが古い(落ちた)フィードは無視する

        self.feed.stop(at(6))
        self.assertTrue(self.connections[0].closed)
        self.assertEqual(self.states(), {"swap": FEED_STOPPED, "vol": FEED_STOPPED})
        other.check_single_instance(at(7))
        with self.assertRaisesRegex(ValueError, "停止しています"):
            self.store.read_live("swap", "PricingSheet.xlsm", "Main", now=at(7))


class VolCalculatorTest(unittest.TestCase):
    def test_vol_equals_conversion_on_the_curve_from_the_same_quotes(self):
        import QuantLib as ql

        from engine.common.curve import bootstrap_curve
        from engine.common.swaption import PremiumConverter
        from tests.test_curve_repricing import SPOT_RATES

        trade_date = T0.date()
        swap_rows = [["O/N", 0.477, None, None]] + [[t, r, None, None] for t, r in SPOT_RATES.items()]
        swap_rows.append(["M1", 0.6, None, None])  # historical_curve.toml に無いTickerは使わない
        rows = live_feed.VolCalculator().rows([["1Yx10Y", "X", 450.0], ["2Mx6Y", "Y", None]], swap_rows, trade_date)
        quotes = {"O/N": 0.477, **SPOT_RATES}
        result = bootstrap_curve(trade_date, quotes)
        ql.Settings.instance().evaluationDate = ql.Date.from_date(trade_date)
        expected = PremiumConverter(result.curve, trade_date).convert("1Yx10Y", 450.0).vol
        self.assertAlmostEqual(rows[0][2], expected, places=10)
        self.assertEqual(rows[1], ["2Mx6Y", "Y", None])

    def test_no_swap_rates_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "スワップのレート"):
            live_feed.VolCalculator().rows([["1Yx10Y", "X", 450.0]], [["O/N", 0.477]], T0.date())


if __name__ == "__main__":
    unittest.main()
