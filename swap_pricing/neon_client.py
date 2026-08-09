"""
Neon(Postgres)への接続・ヒストリカルOISレート取得モジュール。

接続文字列は .env の NEON_CONNECTION_STRING から読む(コミット対象外)。
テーブル: irs_data (product_type='OIS' がJSCC TONA相当のスポットカーブ)。
"1D"は評価日(T)→スポット日(T+2)に使うO/N相当のレートとして扱う
(curve.py側で 'o/n'/'on'/'1d' のいずれのキーも認識する)。
"""

import os
from datetime import date
from pathlib import Path
from typing import Dict, List

import psycopg2

from swap_pricing.paths import REPO_ROOT

ENV_PATH = REPO_ROOT / ".env"


def _load_connection_string() -> str:
    env_value = os.environ.get("NEON_CONNECTION_STRING")
    if env_value:
        return env_value

    if not ENV_PATH.exists():
        raise FileNotFoundError(
            f".envが見つかりません: {ENV_PATH}\n"
            "NEON_CONNECTION_STRING=... を記載してください(.env.exampleを参照)"
        )
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if line.startswith("NEON_CONNECTION_STRING="):
            return line.split("=", 1)[1].strip()
    raise ValueError(".envにNEON_CONNECTION_STRINGが見つかりません")


def get_connection():
    return psycopg2.connect(_load_connection_string())


def get_available_trade_dates(start_date: date, end_date: date) -> List[date]:
    """指定期間内で、OISレートが存在するtrade_dateの一覧を返す(昇順)。"""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT trade_date FROM irs_data
                WHERE product_type = 'OIS' AND trade_date BETWEEN %s AND %s
                ORDER BY trade_date
                """,
                (start_date, end_date),
            )
            return [row[0] for row in cur.fetchall()]


def get_ois_rates_for_date(trade_date: date) -> Dict[str, float]:
    """指定日のOISスポットレート一式を {tenor(小文字): rate(%)} で返す。"""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT tenor, rate FROM irs_data
                WHERE product_type = 'OIS' AND trade_date = %s
                """,
                (trade_date,),
            )
            rows = cur.fetchall()
    return {tenor.strip().lower(): float(rate) for tenor, rate in rows}


def get_ois_rates_for_range(
    start_date: date, end_date: date
) -> Dict[date, Dict[str, float]]:
    """
    期間内の全OISレートを1回のクエリでまとめて取得し、
    {trade_date: {tenor(小文字): rate(%)}} で返す。
    朝バッチのように多数の日付をまとめて処理する場合はこちらを使う
    (日付ごとに接続し直すget_ois_rates_for_dateより大幅に高速)。
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT trade_date, tenor, rate FROM irs_data
                WHERE product_type = 'OIS' AND trade_date BETWEEN %s AND %s
                """,
                (start_date, end_date),
            )
            rows = cur.fetchall()

    result: Dict[date, Dict[str, float]] = {}
    for trade_date, tenor, rate in rows:
        result.setdefault(trade_date, {})[tenor.strip().lower()] = float(rate)
    return result
