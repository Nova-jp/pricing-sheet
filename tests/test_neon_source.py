"""
swap_pricing.neon_source のリグレッションテスト。

実際のNeonには繋がず、psycopg2.connect をモックして「どのSQL・パラメータで
呼んでいるか」「結果をどう組み立てるか」だけを確認する(public."ASW_data" 以外に
触れていないことがこのテストの主眼)。

実行方法:
    python -m unittest tests.test_neon_source -v
"""

import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from swap_pricing import neon_source  # noqa: E402
from swap_pricing.historical_store import AswQuoteRow  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
