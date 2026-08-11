"""
PricingSheet.xlsm 埋め込みのVBAマクロ(SampleCall)から呼ばれるエントリポイント。

VBA側は ThisWorkbook.Name(拡張子なし)からモジュール名を導出して
`import PricingSheet; PricingSheet.main()` を実行する(xlwings標準の
standaloneテンプレートの挙動)。このファイル名・関数名は変更しないこと。
"""

import xlwings as xw

from swap_pricing.pricing_pipeline import refresh_pricing_sheet


def main() -> None:
    book = xw.Book.caller()
    refresh_pricing_sheet(book)
