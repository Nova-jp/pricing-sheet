"""
リアルタイムのフィード(常駐プロセス)。LSEG Workspaceのデスクトップセッションで live_feed.toml の
RICを購読し続け、最新の値を data/live.db(engine/common/live_store.py)に書く。

    python -m engine.common.live_feed            # リポジトリ直下で起動。Ctrl+Cで止める
    python -m engine.common.live_feed --config 別の設定.toml

■ 流れ
  - 起動するとLSEGのセッションを開き、データセット("swap"・"vol")ごとに購読を開く。
    ティック(refresh/update)はメモリ上の最新値に反映するだけ
  - 値が変わったデータセットは最大 write_interval_sec ごとに、ブックが読む形の行にして書く
    (LiveStore.publish。値が同じなら書かない)
  - ティックの有無に関係なく heartbeat_sec ごとに、データセットごとの状態(ハートビート)を書く。
    ブックの LiveQuotes はハートビートが stale_after_sec より古いか状態が ok でなければエラーにする
  - 接続が切れたらまずライブラリ自身の再接続を待ち、resubscribe_after_sec たっても戻らなければ
    セッションと購読を開き直す
  - 終了時(Ctrl+C)は状態を stopped にして書く(ブックはすぐに「停止しています」と分かる)

■ 行の形(ブック側は変えない。以前の LiveFeed.xlsm の PublishLive と同じ)
  - kind="ois": [Ticker, BIDとASKのミッド, 起算日(GV1_DATE), 満期日(GV2_DATE)]
  - kind="vol": [銘柄, RIC, MID_IV_NOR]
  銘柄の状態が異常(Suspect・存在しない等)か、BID・ASKのどちらかが無ければ値を空欄にする
  (黙って古い値・片側の値を使わない)。

LSEGのライブラリ(lseg.data)は起動時にだけ読み込む(テストはLSEG無しで動くように、接続部分を差し替える)。
"""

import argparse
import logging
import os
import threading
import time
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from engine.common.live_store import FEED_OK, FEED_STOPPED, FeedStatus, LiveStore
from engine.common.paths import LIVE_DATA_DIR, REPO_ROOT

DEFAULT_CONFIG = REPO_ROOT / "live_feed.toml"
LOG_PATH = LIVE_DATA_DIR / "live_feed.log"  # live.dbと同じ場所(メインの作業ツリー)

# データセットの種類 → 購読するフィールド
KIND_FIELDS: Dict[str, Tuple[str, ...]] = {
    "ois": ("BID", "ASK", "GV1_DATE", "GV2_DATE"),
    "vol": ("MID_IV_NOR",),
}

CONNECTING = "connecting"  # 起動直後・開き直している途中
RECONNECTING = "reconnecting"  # 接続が切れた(ライブラリの再接続待ち・開き直し待ち)
SUSPECT = "suspect"  # 接続はあるが、そのデータセットの全銘柄が異常

log = logging.getLogger("live_feed")


# ── 設定(live_feed.toml) ───────────────────────────────────────────────────

@dataclass(frozen=True)
class Timing:
    write_interval_sec: float = 1.0
    heartbeat_sec: float = 2.0
    stale_after_sec: float = 10.0
    resubscribe_after_sec: float = 30.0


@dataclass(frozen=True)
class Dataset:
    name: str
    kind: str
    rows: Tuple[Tuple[str, str], ...]  # (ブックで引き当てる表記, RIC)

    @property
    def fields(self) -> Tuple[str, ...]:
        return KIND_FIELDS[self.kind]

    @property
    def rics(self) -> List[str]:
        return [ric for _, ric in self.rows]


@dataclass(frozen=True)
class FeedConfig:
    timing: Timing
    datasets: Tuple[Dataset, ...]


def load_config(path: Path = DEFAULT_CONFIG) -> FeedConfig:
    """live_feed.toml を読み、誤りがあれば ValueError(起動時に止める)。項目が無ければ既定値。"""
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    return parse_config(raw)


def parse_config(raw: dict) -> FeedConfig:
    timing_raw = dict(raw.get("timing", {}))
    unknown = set(timing_raw) - set(Timing.__dataclass_fields__)
    if unknown:
        raise ValueError(f"[timing] に知らない項目があります: {sorted(unknown)}")
    try:
        timing = Timing(**{k: float(v) for k, v in timing_raw.items()})
    except (TypeError, ValueError):
        raise ValueError(f"[timing] の値は秒数(数値)にしてください: {timing_raw}") from None
    for name, value in vars(timing).items():
        if value <= 0:
            raise ValueError(f"[timing] {name} は正の秒数にしてください: {value}")
    if timing.stale_after_sec < 2 * timing.heartbeat_sec:
        raise ValueError(
            f"[timing] stale_after_sec({timing.stale_after_sec:g})は heartbeat_sec({timing.heartbeat_sec:g})の"
            "2倍以上にしてください(ハートビートが1回遅れただけで停止と判定されるため)"
        )
    if timing.write_interval_sec >= timing.stale_after_sec:
        raise ValueError("[timing] write_interval_sec は stale_after_sec より短くしてください")

    datasets = [_parse_dataset(d) for d in raw.get("datasets", [])]
    if not datasets:
        raise ValueError("[[datasets]] がありません")
    names = [d.name for d in datasets]
    if len(set(names)) != len(names):
        raise ValueError(f"datasets の name が重複しています: {names}")
    return FeedConfig(timing, tuple(datasets))


def _parse_dataset(raw: dict) -> Dataset:
    name = str(raw.get("name", "")).strip().lower()
    kind = str(raw.get("kind", "")).strip().lower()
    if not name:
        raise ValueError(f"datasets に name がありません: {raw}")
    if kind not in KIND_FIELDS:
        raise ValueError(f"datasets {name!r} の kind は {sorted(KIND_FIELDS)} のどれかにしてください: {kind!r}")
    if kind == "vol":
        expiries, tenors = raw.get("expiries", []), raw.get("tenors", [])
        if not expiries or not tenors:
            raise ValueError(f"datasets {name!r}(vol)には expiries と tenors を並べてください")
        rows = [(f"{e}x{t}", f"JPYTO{e}X{t}ATM=R") for e in expiries for t in tenors]
    else:
        rows = raw.get("rows", [])
        if not rows or any(not isinstance(r, list) or len(r) != 2 for r in rows):
            raise ValueError(f'datasets {name!r} の rows は ["Ticker", "RIC"] の並びにしてください')
        rows = [(str(label), str(ric)) for label, ric in rows]
    for i, (label, ric) in enumerate(rows):
        if not label.strip() or not ric.strip():
            raise ValueError(f"datasets {name!r} の{i + 1}行目のTicker/RICが空です")
    for what, values in (("Ticker", [r[0] for r in rows]), ("RIC", [r[1] for r in rows])):
        dup = sorted({v for v in values if values.count(v) > 1})
        if dup:
            raise ValueError(f"datasets {name!r} で{what}が重複しています: {dup}")
    return Dataset(name, kind, tuple(rows))


# ── データセットごとの受信状態 ───────────────────────────────────────────────

class DatasetFeed:
    """1つのデータセットの最新値と銘柄ごとの状態。LSEGのコールバック(別スレッド)から更新される。"""

    def __init__(self, dataset: Dataset):
        self.dataset = dataset
        self._lock = threading.Lock()
        self._fields: Dict[str, dict] = {}
        self._bad: Dict[str, str] = {}  # RIC → 異常の内容
        self._dirty = True
        self.last_tick_at: Optional[datetime] = None

    def on_refresh(self, fields, ric, stream=None, now: Optional[datetime] = None) -> None:
        """銘柄の全フィールド(購読の開始・回復時)。"""
        with self._lock:
            self._fields[ric] = dict(fields or {})
            self._bad.pop(ric, None)
            self._tick(now)

    def on_update(self, fields, ric, stream=None, now: Optional[datetime] = None) -> None:
        """変わったフィールドだけ。"""
        with self._lock:
            self._fields.setdefault(ric, {}).update(fields or {})
            self._tick(now)

    def on_status(self, status, ric, stream=None, now: Optional[datetime] = None) -> None:
        """銘柄の状態。Stream=Open かつ Data=Ok 以外は異常として値を空欄にする。"""
        state = (status or {}).get("State", status or {})
        with self._lock:
            if state.get("Stream") == "Open" and state.get("Data") == "Ok":
                self._bad.pop(ric, None)
            else:
                text = state.get("Text") or state.get("Code") or ""
                self._bad[ric] = f"{state.get('Stream')}/{state.get('Data')} {text}".strip()
            self._dirty = True

    def _tick(self, now) -> None:
        self.last_tick_at = now or datetime.now()
        self._dirty = True

    def take_dirty(self) -> bool:
        """前回から変化があったか(呼ぶと変化なしに戻す)。"""
        with self._lock:
            dirty, self._dirty = self._dirty, False
            return dirty

    def all_bad(self) -> bool:
        with self._lock:
            return bool(self.dataset.rows) and all(ric in self._bad for ric in self.dataset.rics)

    def bad(self) -> Dict[str, str]:
        with self._lock:
            return dict(self._bad)

    def rows(self) -> List[list]:
        """ブックが読む形の行(live_feed.toml の並び)。"""
        with self._lock:
            return [self._row(label, ric) for label, ric in self.dataset.rows]

    def _row(self, label, ric) -> list:
        f = None if ric in self._bad else self._fields.get(ric)
        if self.dataset.kind == "vol":
            return [label, ric, _number(f.get("MID_IV_NOR")) if f else None]
        if not f:
            return [label, None, None, None]
        bid, ask = _number(f.get("BID")), _number(f.get("ASK"))
        mid = (bid + ask) / 2 if bid is not None and ask is not None else None
        return [label, mid, _date(f.get("GV1_DATE")), _date(f.get("GV2_DATE"))]


def _number(value) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return None
    return float(value)


def _date(value) -> Optional[datetime]:
    """LSEGの日付("YYYY-MM-DD")を datetime に(live_store が日付として保存する)。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:10])
    except ValueError:
        return None


# ── LSEGとの接続 ─────────────────────────────────────────────────────────────

class LsegConnection:
    """LSEGのデスクトップセッション(Workspaceが起動・ログイン中であること)と、データセットごとの購読。"""

    def __init__(self, feed: "LiveFeed", app_key: Optional[str] = None):
        import lseg.data as ld
        from lseg.data.session import EventCode
        from lseg.data.content import pricing

        self._ld = ld
        self._streams = []
        down = {EventCode.StreamDisconnected, EventCode.StreamReconnecting, EventCode.StreamAuthenticationFailed,
                EventCode.SessionDisconnected, EventCode.SessionReconnecting, EventCode.SessionAuthenticationFailed}
        up = {EventCode.StreamAuthenticationSuccess}

        def on_event(code, message, session):
            if code in down:
                feed.on_connection_event(RECONNECTING, f"{code.name} {_short(message)}")
            elif code in up:
                feed.on_connection_event(FEED_OK, "")

        self._session = ld.open_session(app_key=app_key) if app_key else ld.open_session()
        self._session.on_event(on_event)
        try:
            for ds in feed.datasets.values():
                stream = pricing.Definition(ds.dataset.rics, fields=list(ds.dataset.fields)).get_stream()
                stream.on_refresh(ds.on_refresh).on_update(ds.on_update).on_status(ds.on_status)
                stream.open()  # 全銘柄の最初の値(または状態)が届くまで待つ
                self._streams.append(stream)
                log.info("%s: %d銘柄を購読しました", ds.dataset.name, len(ds.dataset.rows))
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        for stream in self._streams:
            try:
                stream.close()
            except Exception:
                log.exception("購読を閉じられませんでした")
        self._streams = []
        try:
            self._ld.close_session()
        except Exception:
            log.exception("セッションを閉じられませんでした")


def _short(message, limit=200) -> str:
    text = str(message.get("State", message) if isinstance(message, dict) else message)
    return text if len(text) <= limit else text[:limit] + "…"


# ── フィード本体 ─────────────────────────────────────────────────────────────

class LiveFeed:
    """購読の管理(開く・開き直す)と、data/live.db への書き込み(値・ハートビート)。"""

    def __init__(self, config: FeedConfig, store: Optional[LiveStore] = None,
                 connect: Optional[Callable[["LiveFeed"], object]] = None, pid: Optional[int] = None):
        self.config = config
        self.store = store or LiveStore()
        self.datasets: Dict[str, DatasetFeed] = {d.name: DatasetFeed(d) for d in config.datasets}
        self._connect = connect or (lambda feed: LsegConnection(feed, os.environ.get("LSEG_APP_KEY")))
        self.pid = os.getpid() if pid is None else pid
        self._lock = threading.Lock()
        self._state, self._message = CONNECTING, "接続しています"
        self._unhealthy_since: Optional[datetime] = None
        self._connection = None
        self._connecting = False
        self._last_write: Optional[datetime] = None
        self._last_heartbeat: Optional[datetime] = None

    # 接続の状態(LSEGのイベントのコールバックからも呼ばれる)
    def set_connection(self, state: str, message: str) -> None:
        with self._lock:
            if (state, message) != (self._state, self._message):
                log.info("接続の状態: %s %s", state, message)
            self._state, self._message = state, message

    def on_connection_event(self, state: str, message: str) -> None:
        """LSEGのセッションのイベント。購読を開いている途中の「接続できた」は無視する
        (全銘柄の最初の値が揃うまでは ok にしない。揃ったら _reconnect が ok にする)。"""
        if state == FEED_OK and self._connecting:
            return
        self.set_connection(state, message)

    def statuses(self, now: datetime, state: Optional[str] = None) -> List[FeedStatus]:
        """データセットごとの状態(ハートビートとして書く)。"""
        with self._lock:
            conn_state, conn_message = self._state, self._message
        result = []
        for name, ds in self.datasets.items():
            st, msg = (state, "") if state else (conn_state, conn_message)
            if st == FEED_OK:
                bad = ds.bad()
                if ds.all_bad():
                    st, msg = SUSPECT, f"全{len(bad)}銘柄が異常(例 {next(iter(bad.items()))})"
                elif bad:
                    msg = f"{len(bad)}銘柄が異常(値は空欄): {', '.join(sorted(bad)[:5])}"
            result.append(FeedStatus(name, st, msg, now, ds.last_tick_at,
                                     self.config.timing.stale_after_sec, self.pid))
        return result

    def check_single_instance(self, now: datetime) -> None:
        """別のフィードが動いていれば起動しない(同じDBに2つが書くと、止めた方の状態が混ざる)。"""
        for s in self.store.feed_statuses():
            if s.pid != self.pid and s.state != FEED_STOPPED and \
                    (now - s.heartbeat_at).total_seconds() <= s.stale_after_sec:
                raise SystemExit(f"別のフィード(pid {s.pid})が動いています。先にそちらを止めてください")

    def step(self, now: datetime) -> None:
        """メインループの1回分。値とハートビートを書き、必要なら購読を開き直す。"""
        timing = self.config.timing
        heartbeat_due = self._last_heartbeat is None or             (now - self._last_heartbeat).total_seconds() >= timing.heartbeat_sec
        # ハートビートの前には必ず値を書く(ok と書いた時点で、DBの値がそれより古くないように)
        if heartbeat_due or self._last_write is None or                 (now - self._last_write).total_seconds() >= timing.write_interval_sec:
            self._last_write = now
            for name, ds in self.datasets.items():
                if ds.take_dirty():
                    self.store.publish(name, ds.rows(), now=now)
        if heartbeat_due:
            self._last_heartbeat = now
            statuses = self.statuses(now)
            self.store.write_feed_status(statuses)
            self._watch(now, statuses)

    def _watch(self, now: datetime, statuses: List[FeedStatus]) -> None:
        """正常でない状態が resubscribe_after_sec 続いたら、セッションと購読を開き直す。"""
        if all(s.state == FEED_OK for s in statuses):
            self._unhealthy_since = None
            return
        if self._unhealthy_since is None:
            self._unhealthy_since = now
        if self._connection is not None and not self._connecting and \
                (now - self._unhealthy_since).total_seconds() >= self.config.timing.resubscribe_after_sec:
            log.warning("接続が戻らないので、セッションと購読を開き直します")
            self._unhealthy_since = now
            self.connect_async()

    def connect_async(self) -> None:
        """(開き直すときは閉じてから)接続を別スレッドで開く。開いている間もハートビートは書き続ける。"""
        if self._connecting:
            return
        self._connecting = True
        threading.Thread(target=self._reconnect, daemon=True).start()

    def _reconnect(self) -> None:
        try:
            old, self._connection = self._connection, None
            if old is not None:
                self.set_connection(CONNECTING, "開き直しています")
                old.close()
            self.set_connection(CONNECTING, "接続しています")
            self._connection = self._connect(self)
            self.set_connection(FEED_OK, "")
        except Exception as e:
            log.exception("LSEGに接続できませんでした")
            self.set_connection(RECONNECTING, f"接続できません: {e}")
            self._connection = _Retry()  # resubscribe_after_sec 後に _watch がもう一度開く
        finally:
            self._connecting = False

    def stop(self, now: datetime) -> None:
        """購読を閉じ、状態を stopped にして書く。"""
        connection, self._connection = self._connection, None
        if connection is not None:
            connection.close()
        self.store.write_feed_status(self.statuses(now, state=FEED_STOPPED))

    def run(self, stop: Optional[threading.Event] = None, tick_sec: float = 0.2) -> None:
        stop = stop or threading.Event()
        self.check_single_instance(datetime.now())
        self.connect_async()
        last_summary = time.monotonic()
        try:
            while not stop.is_set():
                self.step(datetime.now())
                if time.monotonic() - last_summary >= 60:
                    last_summary = time.monotonic()
                    for s in self.statuses(datetime.now()):
                        tick = f"{s.last_tick_at:%H:%M:%S}" if s.last_tick_at else "なし"
                        log.info("%s: %s 最後の受信 %s %s", s.dataset, s.state, tick, s.message)
                stop.wait(tick_sec)
        finally:
            self.stop(datetime.now())
            log.info("フィードを停止しました")


class _Retry:
    """接続に失敗したときの目印(閉じるものは無い)。"""

    def close(self) -> None:
        pass


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="LSEGのリアルタイムのレートを data/live.db に書き続ける")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="設定ファイル(既定 live_feed.toml)")
    args = parser.parse_args(argv)

    LIVE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(LOG_PATH, encoding="utf-8")],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # Workspaceへの接続のたびのHTTPのログは出さない
    config = load_config(args.config)
    log.info("フィードを起動します(設定 %s、%s)", args.config,
             ", ".join(f"{d.name} {len(d.rows)}銘柄" for d in config.datasets))
    try:
        LiveFeed(config).run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
