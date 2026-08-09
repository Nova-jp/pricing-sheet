"""
リアルタイムデータの読み取りモジュール。

■ データソース
  Excelブック内の "RealTime" シート。Ticker / Value の2列で構成され、
  LSEGのExcel Add-in（RTD関数）等でValue列が更新される想定。
  どのティッカーを置くかはユーザーが手動で管理する。
"""

from typing import Dict, Optional

import xlwings as xw

SHEET_NAME = "RealTime"


def read_realtime_values(book: xw.Book) -> Dict[str, Optional[float]]:
    """
    RealTimeシートの Ticker / Value 列を { ticker: value } の辞書で返す。

    1行目をヘッダー（Ticker, Value）として扱い、2行目以降を読み取る。
    Valueが数値に変換できない場合は None を返す。
    """
    sheet = book.sheets[SHEET_NAME]
    rows = sheet.used_range.value
    if rows is None:
        return {}
    if not isinstance(rows[0], (list, tuple)):
        rows = [rows]

    header = [str(c).strip().lower() if c is not None else "" for c in rows[0]]
    if "ticker" not in header or "value" not in header:
        raise ValueError(
            f"RealTimeシートに 'Ticker' / 'Value' 列が見つかりません。列名: {rows[0]}"
        )
    ticker_idx = header.index("ticker")
    value_idx = header.index("value")

    values: Dict[str, Optional[float]] = {}
    for row in rows[1:]:
        ticker = row[ticker_idx]
        if ticker is None or str(ticker).strip() == "":
            continue
        try:
            values[str(ticker).strip()] = float(row[value_idx])
        except (TypeError, ValueError):
            values[str(ticker).strip()] = None
    return values
