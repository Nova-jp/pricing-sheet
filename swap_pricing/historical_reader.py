"""
ヒストリカルDBファイル（SQLite）の読み取り専用モジュール。

■ 前提
  data/historical.db にヒストリカルデータが格納されている前提で読み取るだけを行う。
  このファイルをどう生成するか（会社PCでの毎朝バッチ実行 / Neonからの部分ダウンロード等）
  は未決定であり、本モジュールのスコープ外。

■ 想定テーブル形状（暫定）
  history(instrument TEXT, as_of_date TEXT[YYYY-MM-DD], value REAL)
"""

import sqlite3
from datetime import date
from typing import List, Optional, Tuple

from swap_pricing.paths import HISTORICAL_DB_PATH


def read_history(
    instrument: str, start_date: Optional[date] = None
) -> List[Tuple[date, float]]:
    """
    指定した instrument のヒストリカル系列を [(日付, 値), ...] （日付昇順）で返す。

    start_date を指定した場合はその日付以降のみを返す。
    """
    if not HISTORICAL_DB_PATH.exists():
        raise FileNotFoundError(
            f"ヒストリカルDBファイルが見つかりません: {HISTORICAL_DB_PATH}\n"
            "DBファイルの生成方法は別途決定予定です。"
        )

    query = "SELECT as_of_date, value FROM history WHERE instrument = ?"
    params: list = [instrument]
    if start_date is not None:
        query += " AND as_of_date >= ?"
        params.append(start_date.isoformat())
    query += " ORDER BY as_of_date"

    with sqlite3.connect(str(HISTORICAL_DB_PATH)) as conn:
        rows = conn.execute(query, params).fetchall()

    return [(date.fromisoformat(as_of_date), value) for as_of_date, value in rows]
