"""
リアルタイムのレートを、フィード(engine/common/live_feed.py、LSEGのストリーミングを購読し続ける
常駐プロセス)から手動計算のブック(PricingSheet.xlsm・SwapAnalysis.xlsm・BondOption.xlsm)へ渡すための
ローカルDB(SQLite、data/live.db)。

■ なぜファイルを介すか
  ブックは手動計算のまま、Shift+F9のときだけ最新を読みたい。フィードは別のプロセスで常に受信し続け、
  変化があれば最大 write_interval_sec ごとにここへ書く(LiveStore.publish)。ブックは読みたいときに
  ここを読む(LiveQuotes)。別のプロセスから同時に読み書きしても壊れないようにSQLite(WAL)にしている。
  LSEG由来のデータを含むためコミットしない(*.db は .gitignore 済み)。

■ テーブル
  - live : データセット(例 "swap"・"vol")ごとの最新の行(JSON)と、値が最後に変わった時刻。
           データセットごとに1行だけ持ち、上書きする。値が変わらないときは書かない。
  - snapshot : (ブック, シート, データセット)ごとに、そのシートが最後にライブで読んだ行と時刻
           (LiveQuotesが読むたびに保存する)。シートの取得モードがFixedのとき、LiveSnapshotは
           最新ではなくこれを返す(Fixedに切り替えた直後も、計算に使ったのと同じレートのまま。
           Ctrl+Alt+F9やUDFサーバの再起動でも変わらない)
  - feed : データセットごとのフィードの状態(ハートビート)。フィードがティックの有無に関係なく
           heartbeat_sec ごとに書く。LiveQuotes(read_live)はこれを見て、ハートビートが stale_after_sec
           より古いか、LSEGとの接続が正常(ok)でなければ、古い値を返さずにエラーにする。
           「相場が静かで値が変わらない」と「フィードが止まっている」をこれで区別する

■ 値の型
  セルの値(文字列・数値・空欄)はJSONのまま持つ。日付(会合スワップの起算日・満期日)は
  {"date": "YYYY-MM-DD"} にして保存し、読むときに datetime に戻す(Excelには日付として返る)。
"""

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, List, NamedTuple, Optional

from engine.common.paths import LIVE_DB_PATH

_DDL = """
CREATE TABLE IF NOT EXISTS live (
    dataset TEXT PRIMARY KEY,
    rows TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshot (
    book TEXT NOT NULL,
    sheet TEXT NOT NULL,
    dataset TEXT NOT NULL,
    rows TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (book, sheet, dataset)
);
CREATE TABLE IF NOT EXISTS feed (
    dataset TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    message TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    last_tick_at TEXT,
    stale_after_sec REAL NOT NULL,
    pid INTEGER NOT NULL
);
"""

FEED_OK = "ok"  # LSEGとの接続が正常(個々の銘柄の異常は行の値を空欄にして表す)
FEED_STOPPED = "stopped"  # フィードを終了した(Ctrl+C等)
START_COMMAND = "python -m engine.common.live_feed"


class LiveData(NamedTuple):
    rows: List[list]
    updated_at: datetime  # 値が最後に変わった時刻(ローカル時刻)


class FeedStatus(NamedTuple):
    """データセットごとのフィードの状態(feedテーブルの1行)。時刻はローカル時刻。"""
    dataset: str
    state: str  # FEED_OK / "connecting" / "reconnecting" / "suspect" / FEED_STOPPED
    message: str  # 状態の説明(LSEGの状態の文言など)
    heartbeat_at: datetime  # フィードが最後に生きていた時刻
    last_tick_at: Optional[datetime]  # 最後に何かを受信した時刻(相場が静かなら古くなる)
    stale_after_sec: float  # この秒数よりハートビートが古ければ停止とみなす(live_feed.toml)
    pid: int  # 書いたフィードのプロセスID(二重起動の検出)

    def problem(self, now: datetime) -> Optional[str]:
        """ライブのレートとして使えない理由(使えるなら None)。"""
        age = (now - self.heartbeat_at).total_seconds()
        if self.state == FEED_STOPPED:
            return f"フィードは停止しています({self.heartbeat_at:%H:%M:%S}に終了)。{START_COMMAND} を起動してください"
        if age > self.stale_after_sec:
            return (f"フィードが止まっています(最後のハートビート {self.heartbeat_at:%H:%M:%S}、{age:.0f}秒前)。"
                    f"フィードのウィンドウを確認するか、起動し直してください")
        if self.state != FEED_OK:
            tick = f"、最後の受信 {self.last_tick_at:%H:%M:%S}" if self.last_tick_at else ""
            return f"LSEGとの接続が正常ではありません({self.state}: {self.message}{tick})"
        return None


def _encode(value):
    if isinstance(value, datetime):
        value = value.date() if value.time() == datetime.min.time() else value
    if isinstance(value, datetime):
        return {"datetime": value.isoformat()}
    if isinstance(value, date):
        return {"date": value.isoformat()}
    if isinstance(value, float) and value != value:  # NaN はJSONにできないので空欄にする
        return None
    return value


def _decode(value):
    if isinstance(value, dict):
        if "date" in value:
            return datetime.fromisoformat(value["date"])
        if "datetime" in value:
            return datetime.fromisoformat(value["datetime"])
    return value


def _dataset_key(dataset) -> str:
    if dataset is None or str(dataset).strip() == "":
        raise ValueError('データセット名が空です(例 "swap"・"vol")')
    return str(dataset).strip().lower()


class LiveStore:
    def __init__(self, db_path: Path = LIVE_DB_PATH):
        self.db_path = Path(db_path)

    def exists(self) -> bool:
        return self.db_path.exists()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        # WAL: 書き込み中も別のExcel(別プロセス)から前の版を読める
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_DDL)
        return conn

    def publish(self, dataset, rows, now: Optional[datetime] = None) -> Optional[datetime]:
        """rows(2次元)を保存する。値が前回と同じなら書かずに None を返し、書いたらその時刻を返す。"""
        key = _dataset_key(dataset)
        payload = json.dumps(
            [[_encode(v) for v in (row if isinstance(row, (list, tuple)) else [row])]
             for row in (rows or [])],
            ensure_ascii=False,
        )
        conn = self._connect()
        try:
            with conn:
                current = conn.execute("SELECT rows FROM live WHERE dataset = ?", (key,)).fetchone()
                if current is not None and current[0] == payload:
                    return None
                stamp = (now or datetime.now()).replace(microsecond=0)
                conn.execute(
                    "INSERT OR REPLACE INTO live (dataset, rows, updated_at) VALUES (?, ?, ?)",
                    (key, payload, stamp.isoformat()),
                )
            return stamp
        finally:
            conn.close()

    def read(self, dataset) -> LiveData:
        """データセットの最新の行。"""
        return self._read(dataset)

    def read_live(self, dataset, book, sheet, now: Optional[datetime] = None) -> LiveData:
        """最新の行を読み、(book, sheet, dataset)の固定の値として保存する(読むのと保存を同じトランザクションで)。
        フィードが止まっている・LSEGとの接続が正常でないときは、古い値を返さずにエラーにする
        (保存してある固定の値も変えない)。"""
        return self._read(dataset, snapshot_for=(str(book), str(sheet)), now=now)

    def read_snapshot(self, dataset, book, sheet) -> LiveData:
        """(book, sheet, dataset)が最後にライブで読んだ行(read_liveで保存したもの)。"""
        key = _dataset_key(dataset)
        found = None
        if self.exists():
            conn = self._connect()
            try:
                found = conn.execute(
                    "SELECT rows, updated_at FROM snapshot WHERE book = ? AND sheet = ? AND dataset = ?",
                    (str(book), str(sheet), key),
                ).fetchone()
            finally:
                conn.close()
        if found is None:
            raise ValueError(
                f"{book}の{sheet}シートに固定の値({key})がありません。B1をLiveにして一度再計算してください"
            )
        return _decode_rows(found)

    def write_feed_status(self, statuses: Iterable[FeedStatus]) -> None:
        """フィードの状態(ハートビート)をデータセットごとに上書きする(まとめて1トランザクション)。"""
        conn = self._connect()
        try:
            with conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO feed"
                    " (dataset, state, message, heartbeat_at, last_tick_at, stale_after_sec, pid)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [(_dataset_key(s.dataset), s.state, s.message, s.heartbeat_at.isoformat(),
                      s.last_tick_at.isoformat() if s.last_tick_at else None, float(s.stale_after_sec), int(s.pid))
                     for s in statuses],
                )
        finally:
            conn.close()

    def feed_statuses(self) -> List[FeedStatus]:
        """全データセットのフィードの状態(DBが無ければ空)。"""
        if not self.exists():
            return []
        conn = self._connect()
        try:
            return [_decode_feed(r) for r in conn.execute(_FEED_SELECT).fetchall()]
        finally:
            conn.close()

    def _read(self, dataset, snapshot_for=None, now=None) -> LiveData:
        key = _dataset_key(dataset)
        if not self.exists():
            raise ValueError(f"リアルタイムのデータがありません。{START_COMMAND} を起動してください")
        conn = self._connect()
        try:
            with conn:
                if snapshot_for is not None:
                    _check_feed(conn, key, now or datetime.now())
                found = conn.execute("SELECT rows, updated_at FROM live WHERE dataset = ?", (key,)).fetchone()
                if found is not None and snapshot_for is not None:
                    conn.execute(
                        "INSERT OR REPLACE INTO snapshot (book, sheet, dataset, rows, updated_at)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (*snapshot_for, key, *found),
                    )
        finally:
            conn.close()
        if found is None:
            raise ValueError(f"リアルタイムのデータセット{key!r}がありません(live_feed.toml の datasets の name を確認)")
        return _decode_rows(found)


_FEED_SELECT = ("SELECT dataset, state, message, heartbeat_at, last_tick_at, stale_after_sec, pid FROM feed")


def _decode_feed(r) -> FeedStatus:
    return FeedStatus(r[0], r[1], r[2], datetime.fromisoformat(r[3]),
                      datetime.fromisoformat(r[4]) if r[4] else None, r[5], r[6])


def _check_feed(conn, key, now) -> None:
    """データセット key のフィードが使える状態でなければ ValueError。"""
    found = conn.execute(_FEED_SELECT + " WHERE dataset = ?", (key,)).fetchone()
    if found is None:
        raise ValueError(f"リアルタイムのフィードが動いていません。{START_COMMAND} を起動してください")
    problem = _decode_feed(found).problem(now)
    if problem:
        raise ValueError(problem)


def _decode_rows(found) -> LiveData:
    rows = [[_decode(v) for v in row] for row in json.loads(found[0])]
    return LiveData(rows, datetime.fromisoformat(found[1]))
