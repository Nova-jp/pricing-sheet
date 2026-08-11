"""
日次OISスポットレートのローカルキャッシュ(SQLite)。

朝バッチ(morning_batch.py)がNeonから取得した生のOISレートを日付ごとに
保存する(ブートストラップ結果ではなく生レートを保存する設計)。
イントラデイのヒストリカルチャート計算は、この生レートを
bootstrap_curve()(日中の計算と全く同じ関数)に通してカーブを組み直す。
こうすることで、日中とヒストリカルの計算経路が完全に一致することが
保証される(カーブの再構築コスト自体はミリ秒オーダーで軽いため)。
Neonへは日中一切アクセスしない。
"""

import json
import sqlite3
from datetime import date
from typing import Dict, List, Optional

from swap_pricing.paths import DATA_DIR

CURVE_CACHE_DB_PATH = DATA_DIR / "curve_cache.db"

_DDL = """
CREATE TABLE IF NOT EXISTS rate_cache (
    as_of_date TEXT PRIMARY KEY,
    rates_json TEXT NOT NULL
)
"""


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(CURVE_CACHE_DB_PATH))
    conn.execute(_DDL)
    return conn


def save_rates(as_of_date: date, rates: Dict[str, float]) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO rate_cache (as_of_date, rates_json) VALUES (?, ?)
            ON CONFLICT(as_of_date) DO UPDATE SET rates_json=excluded.rates_json
            """,
            (as_of_date.isoformat(), json.dumps(rates)),
        )


def load_rates(as_of_date: date) -> Optional[Dict[str, float]]:
    """当該日のOISスポットレート辞書を返す。キャッシュがなければNone。"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT rates_json FROM rate_cache WHERE as_of_date = ?",
            (as_of_date.isoformat(),),
        ).fetchone()
    if row is None:
        return None
    return json.loads(row[0])


def cached_dates() -> List[date]:
    with _connect() as conn:
        rows = conn.execute("SELECT as_of_date FROM rate_cache ORDER BY as_of_date").fetchall()
    return [date.fromisoformat(r[0]) for r in rows]
