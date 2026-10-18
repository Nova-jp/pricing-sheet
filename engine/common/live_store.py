"""
リアルタイムのレートを、自動計算のフィードブック(LiveFeed.xlsm)から手動計算のブック
(PricingSheet.xlsm・SwapAnalysis.xlsm)へ渡すためのローカルDB(SQLite、data/live.db)。

■ なぜファイルを介すか
  Excelの計算方法(自動/手動)はブックごとではなくExcel(プロセス)ごとに1つで、同じExcelで
  開いたブックは全部同じ計算方法になる。別のExcelで開いたブックへのリンクは保存済みのファイル
  しか読めない。そこで、自動計算のExcelで開いたLiveFeedがティックのたびにここへ書き
  (PublishLive)、手動計算のExcelのブックは読みたいときにここを読む(LiveQuotes)。
  LSEG由来のデータを含むためコミットしない(*.db は .gitignore 済み)。

■ テーブル
  - live : データセット(例 "swap"・"vol")ごとの最新の行(LiveFeedのシートの範囲をそのまま、
           JSON)と、値が最後に変わった時刻。データセットごとに1行だけ持ち、上書きする
  値が変わらないときは書かない(ティックのたびに同じ内容を書かない)。
  - snapshot : (ブック, シート, データセット)ごとに、そのシートが最後にライブで読んだ行と時刻
           (LiveQuotesが読むたびに保存する)。シートの取得モードがFixedのとき、LiveSnapshotは
           最新ではなくこれを返す(Fixedに切り替えた直後も、計算に使ったのと同じレートのまま。
           Ctrl+Alt+F9やUDFサーバの再起動でも変わらない)

■ 値の型
  セルの値(文字列・数値・空欄)はJSONのまま持つ。日付(会合スワップの起算日・満期日)は
  {"date": "YYYY-MM-DD"} にして保存し、読むときに datetime に戻す(Excelには日付として返る)。
"""

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import List, NamedTuple, Optional

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
"""


class LiveData(NamedTuple):
    rows: List[list]
    updated_at: datetime  # 値が最後に変わった時刻(ローカル時刻)


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

    def read_live(self, dataset, book, sheet) -> LiveData:
        """最新の行を読み、(book, sheet, dataset)の固定の値として保存する(読むのと保存を同じトランザクションで)。"""
        return self._read(dataset, snapshot_for=(str(book), str(sheet)))

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

    def _read(self, dataset, snapshot_for=None) -> LiveData:
        key = _dataset_key(dataset)
        if not self.exists():
            raise ValueError(
                "リアルタイムのデータがありません。LiveFeed.xlsmを別のExcel(自動計算)で開いてください"
            )
        conn = self._connect()
        try:
            with conn:
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
            raise ValueError(f"リアルタイムのデータセット{key!r}がありません(LiveFeedのPublishLiveの名前を確認)")
        return _decode_rows(found)


def _decode_rows(found) -> LiveData:
    rows = [[_decode(v) for v in row] for row in json.loads(found[0])]
    return LiveData(rows, datetime.fromisoformat(found[1]))
