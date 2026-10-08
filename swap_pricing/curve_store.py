"""
ヒストリカル日次カーブのローカルDB(SQLite、data/historical.db)。

朝のバッチ(swap_pricing/curve_batch.py、手動実行)が historical_data.xlsx(LSEG)から
作成し、日中のヒストリカル系UDFはここを読むだけにする(ブートストラップをしない)。
LSEG由来のデータを含むためコミットしない(*.db は .gitignore 済み)。

■ テーブル
  - quotes  : 日付×テナーの生レート(Midシートの全テナー)。構築方法やテナー選択を
              変えたとき、xlsxの取得期間から外れた過去日もここから作り直せる
  - fixings : TONA実績(Fixingシート)。O/Nのピラーと、過去起算の計算に使う
  - vol_quotes : スワップションATMノーマルvol(Volシート、bp/年)。銘柄は「満期x原資産」
              (例 1Yx10Y)。分析ブック(swap_analysis)のPCAが読む。カーブには使わない
  - curves  : 日付ごとの構築済みカーブ。評価日から暦日1日刻みのDF(GRID_DAYS点)を
              float64の配列でそのまま持つ。構築方法名・入力のハッシュ・使用/不使用テナーも記録

■ なぜ暦日ごとのDFを保存するか(カーブの引き方に依存しないため)
  どんな引き方(Convex Monotone、将来のステップアップ等)で作ったカーブも、
  「日付→DF」に落とせば同じ形式で保存・復元できる。OISの評価はフィキシング日・
  利払日など日付上のDFしか使わないため、全暦日のDFを持てば元のカーブと
  同じパーレートになる(tests/test_curve_store.py で確認)。ピラーと補間方法を
  保存する方式だと、読む側で同じ補間を再現する必要があり引き方に依存してしまう。
  営業日だけでなく暦日で持つのは、祝日カレンダーの違い(QuantLibのバージョン差等)で
  ノードが欠けないようにするため。GRID_DAYSを超える日付は外挿せずエラーにする。

■ 復元
  ql.DiscountCurve(日付, DF) で組み直す(ノード上はDFそのもの)。構築時の評価日には
  依存しないカーブになる。ql.Dateオブジェクトの生成が復元時間の大半を占めるため、
  日付リストはプロセス内で1本だけ作って使い回し、必要な期間(until)だけ読み出す。
"""

import json
import sqlite3
from array import array
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Iterable, List, NamedTuple, Optional

import QuantLib as ql

from swap_pricing.paths import HISTORICAL_DB_PATH

GRID_YEARS = 50  # 最長テナー40Y+フォワード分の余裕
GRID_DAYS = GRID_YEARS * 366
_DF_BYTES = array("d").itemsize

_DDL = """
CREATE TABLE IF NOT EXISTS quotes (
    as_of_date TEXT NOT NULL,
    tenor TEXT NOT NULL,
    rate REAL NOT NULL,
    PRIMARY KEY (as_of_date, tenor)
);
CREATE TABLE IF NOT EXISTS fixings (
    fixing_date TEXT PRIMARY KEY,
    rate REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS vol_quotes (
    as_of_date TEXT NOT NULL,
    label TEXT NOT NULL,
    vol REAL NOT NULL,
    PRIMARY KEY (as_of_date, label)
);
CREATE TABLE IF NOT EXISTS curves (
    as_of_date TEXT PRIMARY KEY,
    builder TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    used_tenors TEXT NOT NULL,
    skipped_tenors TEXT NOT NULL,
    dfs BLOB NOT NULL,
    built_at TEXT NOT NULL
);
"""


class StoredCurve(NamedTuple):
    curve: ql.YieldTermStructureHandle
    index: "ql.OvernightIndex"


def discount_grid(curve: ql.YieldTermStructureHandle, as_of_date: date) -> array:
    """カーブ → 評価日から暦日1日刻みのDF(GRID_DAYS点)。どの引き方のカーブでもよい。"""
    # ブートストラップ系のカーブは評価日に連動して再計算されるため、構築時の評価日に戻す
    ql.Settings.instance().evaluationDate = ql.Date.from_date(as_of_date)
    return array("d", (curve.discount(d) for d in _grid_dates(as_of_date, GRID_DAYS)))


_date_cache: List[ql.Date] = []  # 連続した暦日の ql.Date
_DATE_CACHE_MARGIN = 10 * 366  # 作り直しの頻度を下げるため前後に余分に確保する


def _grid_dates(start: date, n: int) -> List[ql.Date]:
    global _date_cache
    first = ql.Date.from_date(start).serialNumber()
    base = _date_cache[0].serialNumber() if _date_cache else first
    if first < base or first + n > base + len(_date_cache):
        base = first - _DATE_CACHE_MARGIN
        _date_cache = [ql.Date(s) for s in range(base, first + GRID_DAYS + _DATE_CACHE_MARGIN)]
    offset = first - base
    return _date_cache[offset:offset + n]


def _iso(d: date) -> str:
    return d.isoformat()


class CurveStore:
    def __init__(self, db_path: Path = HISTORICAL_DB_PATH):
        self.db_path = Path(db_path)
        self._session: Optional[sqlite3.Connection] = None

    def exists(self) -> bool:
        return self.db_path.exists()

    def __enter__(self) -> "CurveStore":
        """with ブロックの間は接続を1本だけ開いて使い回す(日付ごとに load する
        ヒストリカル計算で、毎回の接続・切断のコストを避けるため)。"""
        self._session = self._open()
        return self

    def __exit__(self, *exc) -> None:
        self._session.close()
        self._session = None

    def _open(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        conn.executescript(_DDL)
        return conn

    @contextmanager
    def _connect(self):
        """接続を必ず閉じる(sqlite3の with 文はコミットするだけで閉じないため、
        Windowsではファイルがロックされたまま残る)。with ブロック中はその接続を使う。"""
        if self._session is not None:
            with self._session:
                yield self._session
            return
        conn = self._open()
        try:
            with conn:  # 正常終了でコミット、例外でロールバック
                yield conn
        finally:
            conn.close()

    # --- バッチ(書き込み) ---

    def save_quotes(self, quotes_by_date: Dict[date, Dict[str, float]]) -> None:
        """日付ごとに丸ごと置き換える(LSEG側の値の修正も反映されるように)。"""
        with self._connect() as conn:
            for as_of_date, quotes in quotes_by_date.items():
                conn.execute("DELETE FROM quotes WHERE as_of_date = ?", (_iso(as_of_date),))
                conn.executemany(
                    "INSERT INTO quotes (as_of_date, tenor, rate) VALUES (?, ?, ?)",
                    [(_iso(as_of_date), tenor, rate) for tenor, rate in quotes.items()],
                )

    def save_fixings(self, fixings: Dict[date, float]) -> None:
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO fixings (fixing_date, rate) VALUES (?, ?) "
                "ON CONFLICT(fixing_date) DO UPDATE SET rate=excluded.rate",
                [(_iso(d), r) for d, r in fixings.items()],
            )

    def save_vol_quotes(self, vols_by_date: Dict[date, Dict[str, float]]) -> None:
        """日付ごとに丸ごと置き換える(save_quotes と同じ)。"""
        with self._connect() as conn:
            for as_of_date, vols in vols_by_date.items():
                conn.execute("DELETE FROM vol_quotes WHERE as_of_date = ?", (_iso(as_of_date),))
                conn.executemany(
                    "INSERT INTO vol_quotes (as_of_date, label, vol) VALUES (?, ?, ?)",
                    [(_iso(as_of_date), label, v) for label, v in vols.items()],
                )

    def save_curve(
        self, as_of_date: date, builder: str, input_hash: str,
        used_tenors: Iterable[str], skipped_tenors: Iterable[str], dfs: array,
    ) -> None:
        if len(dfs) != GRID_DAYS:
            raise ValueError(f"DFの点数が不正です: {len(dfs)} (期待 {GRID_DAYS})")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO curves VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(as_of_date) DO UPDATE SET "
                "builder=excluded.builder, input_hash=excluded.input_hash, "
                "used_tenors=excluded.used_tenors, skipped_tenors=excluded.skipped_tenors, "
                "dfs=excluded.dfs, built_at=excluded.built_at",
                (
                    _iso(as_of_date), builder, input_hash, json.dumps(list(used_tenors)),
                    json.dumps(list(skipped_tenors)), dfs.tobytes(),
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )

    # --- 読み込み ---

    def quotes(self) -> Dict[date, Dict[str, float]]:
        result: Dict[date, Dict[str, float]] = {}
        with self._connect() as conn:
            for d, tenor, rate in conn.execute("SELECT as_of_date, tenor, rate FROM quotes"):
                result.setdefault(date.fromisoformat(d), {})[tenor] = rate
        return result

    def quotes_on(self, as_of_date: date) -> Dict[str, float]:
        """その日の生レート {テナー: %}(Midシートの表記)。無ければ空。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT tenor, rate FROM quotes WHERE as_of_date = ?", (_iso(as_of_date),),
            ).fetchall()
        return dict(rows)

    def vol_quotes(self) -> Dict[date, Dict[str, float]]:
        result: Dict[date, Dict[str, float]] = {}
        with self._connect() as conn:
            for d, label, v in conn.execute("SELECT as_of_date, label, vol FROM vol_quotes"):
                result.setdefault(date.fromisoformat(d), {})[label] = v
        return result

    def fixings(self) -> Dict[date, float]:
        with self._connect() as conn:
            rows = conn.execute("SELECT fixing_date, rate FROM fixings").fetchall()
        return {date.fromisoformat(d): r for d, r in rows}

    def input_hashes(self) -> Dict[date, str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT as_of_date, input_hash FROM curves").fetchall()
        return {date.fromisoformat(d): h for d, h in rows}

    def dates(self) -> List[date]:
        """カーブが保存されている日付(昇順)。"""
        with self._connect() as conn:
            rows = conn.execute("SELECT as_of_date FROM curves ORDER BY as_of_date").fetchall()
        return [date.fromisoformat(r[0]) for r in rows]

    def load(self, as_of_date: date, until: Optional[date] = None) -> StoredCurve:
        """保存済みカーブを ql.DiscountCurve として復元する。

        until: この日付までのDFだけを読む(短いスワップの計算を速くするため)。
        省略時は保存範囲の全体。範囲外の日付を評価に使うとQuantLibがエラーにする。
        """
        n = GRID_DAYS if until is None else (until - as_of_date).days + 1
        if n > GRID_DAYS:
            raise ValueError(
                f"{as_of_date}のカーブの保存範囲({GRID_YEARS}年)を超える日付です: {until}"
            )
        n = max(n, 2)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT substr(dfs, 1, ?) FROM curves WHERE as_of_date = ?",
                (n * _DF_BYTES, _iso(as_of_date)),
            ).fetchone()
        if row is None:
            raise ValueError(f"{as_of_date}のカーブがDBにありません")
        dfs = array("d")
        dfs.frombytes(row[0])
        curve = ql.DiscountCurve(_grid_dates(as_of_date, n), dfs.tolist(), ql.Actual365Fixed())
        handle = ql.YieldTermStructureHandle(curve)
        return StoredCurve(curve=handle, index=ql.Tonar(handle))
