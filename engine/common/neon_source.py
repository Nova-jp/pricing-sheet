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

■ 経路: 5432番(Postgres)→ 繋がらなければ443番(HTTPS)
  まず psycopg2 で5432番に繋ぎ、接続に失敗したら(会社のネットワーク等で5432番が塞がれている)
  NeonのSQL over HTTPの窓口(https://<ホスト>/sql、公式のserverless driverが使うもの)に
  同じSELECTを投げる。接続文字列は同じものを Neon-Connection-String ヘッダーで渡す。
  PCごとに設定を書き換えずに済むよう、切り替えは自動にした。HTTPSでは日付・時刻が文字列で
  返るので、5432番と同じ型(date/datetime/float)に戻してから返す。HTTPSは1回のレスポンスが
  64MBまでで初回の全件(約90万行)が収まらないため、主キー順に10万行ずつ分けて取る。
  プロキシは urllib の既定(環境変数・Windowsのシステム設定)に従う。

■ 差分取得
  since を渡すと、Neon側の updated_at がそれより新しい行だけを取る(全件を毎回
  取り直さない)。基準値はローカルDB(historical_store.HistoricalStore.asw_quotes_max_updated_at)
  から呼び出し側が渡す。
"""

import json
import os
import urllib.error
import urllib.request
from datetime import date, datetime
from typing import List, Optional
from urllib.parse import urlparse

import psycopg2

from engine.common.historical_store import AswQuoteRow

_ASW_TABLE = 'public."ASW_data"'
_COLUMNS = "trade_date, bond_code, asw_act365_pa, asw_act365_sa, updated_at"
_PG_CONNECT_TIMEOUT = 5  # 5432番が塞がれた環境では毎朝この秒数を待ってからHTTPSに切り替える
_HTTP_TIMEOUT = 60
_HTTP_PAGE_ROWS = 100_000  # 1ページ約14MB(HTTPSのレスポンスの上限は64MB)


def fetch_asw_quotes(
    since: Optional[datetime] = None, connection_string: Optional[str] = None, log=None,
) -> List[AswQuoteRow]:
    """ASW_dataの行を取得する(trade_date, bond_code, asw_act365_pa, asw_act365_sa, updated_at)。

    since を渡すと updated_at > since の行だけ(差分取得)。省略時は全件。
    5432番に繋がらなければHTTPS(443番)で取り直す。log を渡すとHTTPSに切り替えたことを出す。
    """
    conn_str = connection_string or os.environ.get("NEON_CONNECTION_STRING")
    if not conn_str:
        raise RuntimeError(
            "NEON_CONNECTION_STRING が設定されていません(.env を確認してください)"
        )
    query = f"SELECT {_COLUMNS} FROM {_ASW_TABLE}"
    try:
        conn = psycopg2.connect(conn_str, connect_timeout=_PG_CONNECT_TIMEOUT)
    except psycopg2.OperationalError as e:
        if log is not None:
            log(f"ASW取得: 5432番に繋がらないためHTTPSで取得 ({str(e).strip()})")
        return _fetch_via_https(conn_str, query, since)
    params: tuple = ()
    if since is not None:
        query += " WHERE updated_at > %s"
        params = (since,)
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


def _fetch_via_https(conn_str: str, query: str, since: Optional[datetime]) -> List[AswQuoteRow]:
    """NeonのSQL over HTTP(https://<ホスト>/sql)で同じSELECTを投げる。プレースホルダーは $1, $2…。

    1回のレスポンスは64MBまで(全件の約90万行は収まらない)なので、主キーの
    (trade_date, bond_code) の順に _HTTP_PAGE_ROWS 行ずつ取る(続きはその行より後から)。
    """
    conditions: list = []
    params: list = []
    if since is not None:
        params.append(since.isoformat(sep=" "))
        conditions.append(f"updated_at > ${len(params)}")
    result: List[AswQuoteRow] = []
    while True:
        page_conditions, page_params = list(conditions), list(params)
        if result:
            last = result[-1]
            page_params += [last.trade_date.isoformat(), last.bond_code]
            page_conditions.append(f"(trade_date, bond_code) > (${len(page_params) - 1}, ${len(page_params)})")
        page_query = query
        if page_conditions:
            page_query += " WHERE " + " AND ".join(page_conditions)
        page_query += f" ORDER BY trade_date, bond_code LIMIT {_HTTP_PAGE_ROWS}"
        rows = _post_sql(conn_str, page_query, page_params)
        # 日付・時刻は文字列、float8は数値で返る。5432番(psycopg2)と同じ型に戻す
        result += [
            AswQuoteRow(
                trade_date=date.fromisoformat(r["trade_date"]),
                bond_code=r["bond_code"],
                asw_pa=r["asw_act365_pa"],
                asw_sa=r["asw_act365_sa"],
                updated_at=datetime.fromisoformat(r["updated_at"]),
            )
            for r in rows
        ]
        if len(rows) < _HTTP_PAGE_ROWS:
            return result


def _post_sql(conn_str: str, query: str, params: list) -> list:
    req = urllib.request.Request(
        f"https://{urlparse(conn_str).hostname}/sql",
        data=json.dumps({"query": query, "params": params}).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "Neon-Connection-String": conn_str},
    )
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as res:
            return json.loads(res.read())["rows"]
    except urllib.error.HTTPError as e:
        # 本文にNeonのエラーメッセージ(SQLの誤り・認証失敗等)が入る
        detail = e.read().decode(errors="replace")
        raise RuntimeError(f"Neon(HTTPS)の取得に失敗: {e.code} {detail}") from e
