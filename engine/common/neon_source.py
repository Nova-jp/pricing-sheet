"""
Neon(Postgres)上のASWスプレッドを取得する専用モジュール。

■ 読み取り専用・1テーブルだけ
  このNeonインスタンスは他プロジェクト(BOJ会合インプライド分析ツール等)のテーブルも
  同居している共有DB。ここでは public."ASW_data" への SELECT 以外は一切行わない
  (書き込み・他テーブルの参照はしない)。

■ 接続情報
  環境変数 NEON_CONNECTION_STRING から読む(.env、gitignore対象)。値はコード・docsに
  書かない。読み込み元は呼び出し側(morning_batch.py)が load_dotenv() で用意する
  (このモジュール自身は .env を読まない。os.environ だけに依存する)。

■ 差分取得
  since を渡すと、Neon側の updated_at がそれより新しい行だけを取る(全件を毎回
  取り直さない)。基準値はローカルDB(historical_store.HistoricalStore.asw_quotes_max_updated_at)
  から呼び出し側が渡す。
"""

import os
from datetime import datetime
from typing import List, Optional

import psycopg2

from engine.common.historical_store import AswQuoteRow

_ASW_TABLE = 'public."ASW_data"'


def fetch_asw_quotes(
    since: Optional[datetime] = None, connection_string: Optional[str] = None,
) -> List[AswQuoteRow]:
    """ASW_dataの行を取得する(trade_date, bond_code, asw_act365_pa, asw_act365_sa, updated_at)。

    since を渡すと updated_at > since の行だけ(差分取得)。省略時は全件。
    """
    conn_str = connection_string or os.environ.get("NEON_CONNECTION_STRING")
    if not conn_str:
        raise RuntimeError(
            "NEON_CONNECTION_STRING が設定されていません(.env を確認してください)"
        )
    query = (
        f"SELECT trade_date, bond_code, asw_act365_pa, asw_act365_sa, updated_at "
        f"FROM {_ASW_TABLE}"
    )
    params: tuple = ()
    if since is not None:
        query += " WHERE updated_at > %s"
        params = (since,)

    conn = psycopg2.connect(conn_str, connect_timeout=10)
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
    finally:
        conn.close()
    return [
        AswQuoteRow(trade_date=d, bond_code=code, asw_pa=pa, asw_sa=sa, updated_at=updated_at)
        for d, code, pa, sa, updated_at in rows
    ]
