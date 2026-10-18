"""
LiveFeed.xlsm とファイル名が一致するUDF公開用モジュール。

xlwingsのExcelアドインは、UDFの登録先を探す際にワークブックと同名の
.pyファイルを見るため、実体(engine/common/live_udf.pyのPublishLive)をここに再エクスポートする。
LiveFeed.xlsmは自動計算のExcel(手動計算のブックとは別のExcel)で開く(docs/architecture.md)。
UDF専用設計のため、VBA/RunPython経由のエントリポイントは持たない。
"""

from engine.common.live_udf import PublishLive  # noqa: F401
