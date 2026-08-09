"""
日次DFカーブのローカルキャッシュ(SQLite)。

朝バッチ(morning_batch.py)がNeonから取得したOISレートをブートストラップした
結果(pillarのtime/DF配列)を日付ごとに保存する。イントラデイのヒストリカル
チャート計算は、このローカルキャッシュだけを参照しNeonには接続しない。
"""

import json
import sqlite3
from datetime import date
from typing import List, Optional, Tuple

from swap_pricing.paths import DATA_DIR

CURVE_CACHE_DB_PATH = DATA_DIR / "curve_cache.db"

_DDL = """
CREATE TABLE IF NOT EXISTS curve_cache (
    as_of_date TEXT PRIMARY KEY,
    include_odd_tenors INTEGER NOT NULL,
    pillar_times_json TEXT NOT NULL,
    pillar_dfs_json TEXT NOT NULL,
    used_tenors_json TEXT NOT NULL,
    skipped_tenors_json TEXT NOT NULL
)
"""


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(CURVE_CACHE_DB_PATH))
    conn.execute(_DDL)
    return conn


def save_curve(
    as_of_date: date,
    include_odd_tenors: bool,
    pillar_times: List[float],
    pillar_dfs: List[float],
    used_tenors: List[str],
    skipped_tenors: List[str],
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO curve_cache
                (as_of_date, include_odd_tenors, pillar_times_json, pillar_dfs_json,
                 used_tenors_json, skipped_tenors_json)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(as_of_date) DO UPDATE SET
                include_odd_tenors=excluded.include_odd_tenors,
                pillar_times_json=excluded.pillar_times_json,
                pillar_dfs_json=excluded.pillar_dfs_json,
                used_tenors_json=excluded.used_tenors_json,
                skipped_tenors_json=excluded.skipped_tenors_json
            """,
            (
                as_of_date.isoformat(),
                int(include_odd_tenors),
                json.dumps(pillar_times),
                json.dumps(pillar_dfs),
                json.dumps(used_tenors),
                json.dumps(skipped_tenors),
            ),
        )


def load_curve(as_of_date: date) -> Optional[Tuple[List[float], List[float]]]:
    """(pillar_times, pillar_dfs) を返す。キャッシュがなければNone。"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT pillar_times_json, pillar_dfs_json FROM curve_cache WHERE as_of_date = ?",
            (as_of_date.isoformat(),),
        ).fetchone()
    if row is None:
        return None
    return json.loads(row[0]), json.loads(row[1])


def cached_dates() -> List[date]:
    with _connect() as conn:
        rows = conn.execute("SELECT as_of_date FROM curve_cache ORDER BY as_of_date").fetchall()
    return [date.fromisoformat(r[0]) for r in rows]
