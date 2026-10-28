"""
engine.common.neon_source のリグレッションテスト。

実際のNeonには繋がず、psycopg2.connect をモックして「どのSQL・パラメータで
呼んでいるか」「結果をどう組み立てるか」だけを確認する(public."ASW_data" 以外に
触れていないことがこのテストの主眼)。

実行方法:
    python -m unittest tests.test_neon_source -v
"""

import io
import json
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from engine.common import neon_source  # noqa: E402
from engine.common.historical_store import AswQuoteRow  # noqa: E402

_ROWS = [
    (datetime(2026, 10, 9).date(), "000190054", 0.448, 0.4804, datetime(2026, 10, 9, 12, 30, 6)),
]


def _mock_connect(rows=_ROWS):
    cursor = mock.MagicMock()
    cursor.fetchall.return_value = rows
    cursor.__enter__.return_value = cursor
    conn = mock.MagicMock()
    conn.cursor.return_value = cursor
    connect = mock.MagicMock(return_value=conn)
    return connect, cursor


class FetchAswQuotesTest(unittest.TestCase):
    def test_queries_only_asw_data_table(self):
        connect, cursor = _mock_connect()
        with mock.patch.object(neon_source.psycopg2, "connect", connect):
            neon_source.fetch_asw_quotes(connection_string="postgres://dummy")
        sql = cursor.execute.call_args[0][0]
        self.assertIn('public."ASW_data"', sql)
        self.assertNotIn("WHERE", sql)  # sinceを渡さない = 全件

    def test_since_adds_updated_at_filter(self):
        connect, cursor = _mock_connect()
        since = datetime(2026, 10, 9, 12, 30, 6)
        with mock.patch.object(neon_source.psycopg2, "connect", connect):
            neon_source.fetch_asw_quotes(since=since, connection_string="postgres://dummy")
        sql, params = cursor.execute.call_args[0]
        self.assertIn("updated_at > %s", sql)
        self.assertEqual(params, (since,))

    def test_rows_become_asw_quote_rows(self):
        connect, _ = _mock_connect()
        with mock.patch.object(neon_source.psycopg2, "connect", connect):
            result = neon_source.fetch_asw_quotes(connection_string="postgres://dummy")
        self.assertEqual(result, [
            AswQuoteRow(
                datetime(2026, 10, 9).date(), "000190054", 0.448, 0.4804,
                datetime(2026, 10, 9, 12, 30, 6),
            ),
        ])

    def test_missing_connection_string_raises(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(RuntimeError):
                neon_source.fetch_asw_quotes()

    def test_connection_is_always_closed(self):
        connect, _ = _mock_connect()
        conn = connect.return_value
        with mock.patch.object(neon_source.psycopg2, "connect", connect):
            neon_source.fetch_asw_quotes(connection_string="postgres://dummy")
        conn.close.assert_called_once()

    def test_connection_closed_even_on_query_failure(self):
        connect, cursor = _mock_connect()
        cursor.execute.side_effect = RuntimeError("boom")
        conn = connect.return_value
        with mock.patch.object(neon_source.psycopg2, "connect", connect):
            with self.assertRaises(RuntimeError):
                neon_source.fetch_asw_quotes(connection_string="postgres://dummy")
        conn.close.assert_called_once()


_HTTP_BODY = {
    "fields": [{"name": n} for n in
               ("trade_date", "bond_code", "asw_act365_pa", "asw_act365_sa", "updated_at")],
    "rows": [{
        "trade_date": "2026-10-09", "bond_code": "000190054", "asw_act365_pa": 0.448,
        "asw_act365_sa": 0.4804, "updated_at": "2026-10-09 12:30:06.89817",
    }],
}
_CONN_STR = "postgresql://user:pw@ep-dummy-pooler.example.neon.tech/db?sslmode=require"


def _mock_urlopen(body=_HTTP_BODY):
    res = mock.MagicMock()
    res.read.return_value = json.dumps(body).encode()
    res.__enter__.return_value = res
    return mock.MagicMock(return_value=res)


class HttpsFallbackTest(unittest.TestCase):
    """5432番に繋がらないときはHTTPS(https://<ホスト>/sql)で同じSELECTを投げる。"""

    def _fetch(self, since=None, urlopen=None, log=None):
        urlopen = urlopen or _mock_urlopen()
        refused = mock.MagicMock(side_effect=neon_source.psycopg2.OperationalError("timeout expired"))
        with mock.patch.object(neon_source.psycopg2, "connect", refused), \
                mock.patch.object(neon_source.urllib.request, "urlopen", urlopen):
            result = neon_source.fetch_asw_quotes(since=since, connection_string=_CONN_STR, log=log)
        return result, urlopen.call_args[0][0]

    def test_posts_same_select_to_sql_endpoint(self):
        _, req = self._fetch()
        self.assertEqual(req.full_url, "https://ep-dummy-pooler.example.neon.tech/sql")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Neon-connection-string"), _CONN_STR)
        payload = json.loads(req.data)
        self.assertIn('public."ASW_data"', payload["query"])
        self.assertNotIn("WHERE", payload["query"])
        self.assertEqual(payload["params"], [])

    def test_since_becomes_positional_param(self):
        _, req = self._fetch(since=datetime(2026, 10, 9, 12, 30, 6))
        payload = json.loads(req.data)
        self.assertIn("updated_at > $1", payload["query"])
        self.assertEqual(payload["params"], ["2026-10-09 12:30:06"])

    def test_rows_are_converted_to_same_types_as_postgres(self):
        result, _ = self._fetch()
        self.assertEqual(result, [
            AswQuoteRow(
                datetime(2026, 10, 9).date(), "000190054", 0.448, 0.4804,
                datetime(2026, 10, 9, 12, 30, 6, 898170),
            ),
        ])

    def test_pages_by_primary_key(self):
        """レスポンスの上限(64MB)を超えないよう、(trade_date, bond_code) の続きから取る。"""
        row = _HTTP_BODY["rows"][0]
        pages = [[row], [dict(row, bond_code="000200054")], []]
        responses = []
        for rows in pages:
            res = mock.MagicMock()
            res.read.return_value = json.dumps({"rows": rows}).encode()
            res.__enter__.return_value = res
            responses.append(res)
        urlopen = mock.MagicMock(side_effect=responses)
        with mock.patch.object(neon_source, "_HTTP_PAGE_ROWS", 1):
            result, _ = self._fetch(since=datetime(2026, 10, 9), urlopen=urlopen)
        self.assertEqual([r.bond_code for r in result], ["000190054", "000200054"])
        self.assertEqual(urlopen.call_count, 3)
        payload = json.loads(urlopen.call_args_list[2][0][0].data)
        self.assertIn("updated_at > $1 AND (trade_date, bond_code) > ($2, $3)", payload["query"])
        self.assertIn("ORDER BY trade_date, bond_code LIMIT 1", payload["query"])
        self.assertEqual(payload["params"], ["2026-10-09 00:00:00", "2026-10-09", "000200054"])

    def test_logs_fallback(self):
        log = mock.MagicMock()
        self._fetch(log=log)
        self.assertIn("HTTPS", log.call_args[0][0])

    def test_http_error_carries_neon_message(self):
        err = neon_source.urllib.error.HTTPError(
            "https://x/sql", 400, "Bad Request", {}, io.BytesIO(b'{"message":"password authentication failed"}'),
        )
        with self.assertRaisesRegex(RuntimeError, "password authentication failed"):
            self._fetch(urlopen=mock.MagicMock(side_effect=err))

    def test_postgres_used_when_reachable(self):
        connect, _ = _mock_connect()
        urlopen = _mock_urlopen()
        with mock.patch.object(neon_source.psycopg2, "connect", connect), \
                mock.patch.object(neon_source.urllib.request, "urlopen", urlopen):
            neon_source.fetch_asw_quotes(connection_string=_CONN_STR)
        urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
