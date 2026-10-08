"""
PricingSheet.xlsx とファイル名が一致するUDF公開用モジュール。

xlwingsのExcelアドインは、UDFの登録先を探す際にワークブックと同名の
.pyファイルを見るため、実体(swap_pricing/udf.py)をここに再エクスポートする。
UDF専用設計のため、VBA/RunPython経由のエントリポイントは持たない。
"""

from swap_pricing.udf import *  # noqa: F401,F403
