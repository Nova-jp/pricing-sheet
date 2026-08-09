"""
データ取得パイプラインのエントリポイント。

RealTimeシートのライブ値 + ヒストリカルDBファイルの値を Calc シートへ書き込む。
xlwingsの RunPython から呼び出される想定。

計算ロジック（QuantLib等）は次フェーズで扱うため、ここでは値を集めて
Calcシートに素通しするところまでを担当する。
"""

import xlwings as xw

from swap_pricing.feed_reader import read_realtime_values
from swap_pricing.historical_reader import read_history

CALC_SHEET_NAME = "Calc"


def refresh() -> None:
    book = xw.Book.caller()
    calc_sheet = book.sheets[CALC_SHEET_NAME]
    calc_sheet.clear_contents()

    realtime_values = read_realtime_values(book)

    realtime_rows = [["Ticker", "RealTimeValue"]]
    realtime_rows += [[ticker, value] for ticker, value in realtime_values.items()]
    calc_sheet.range("A1").value = realtime_rows

    historical_rows = [["Ticker", "Date", "HistoricalValue"]]
    for ticker in realtime_values:
        try:
            history = read_history(ticker)
        except FileNotFoundError:
            history = []
        historical_rows += [[ticker, as_of_date, value] for as_of_date, value in history]
    calc_sheet.range("D1").value = historical_rows


if __name__ == "__main__":
    xw.Book("PricingSheet.xlsm").set_mock_caller()
    refresh()
