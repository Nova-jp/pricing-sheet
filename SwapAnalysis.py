"""
SwapAnalysis.xlsm とファイル名が一致するUDF公開用モジュール。

xlwingsのExcelアドインは、UDFの登録先を探す際にワークブックと同名の
.pyファイルを見るため、実体(engine/pca/udf.py・engine/vol/udf.py・engine/boj/udf.py)と、リアルタイムのレートを読むUDF
(engine/common/live_udf.pyのLiveQuotes・LiveSnapshot・LiveInfo。PricingSheetと共通)をここに再エクスポートする。
PricingSheet側(engine/swap_pricing/)のUDFは使わない(ブック同士を依存させない)。
UDF専用設計のため、VBA/RunPython経由のエントリポイントは持たない。
"""

from engine.boj.udf import BojImpliedRates, BojSpotForwards  # noqa: F401
from engine.common.live_udf import LiveInfo, LiveQuotes, LiveSnapshot  # noqa: F401
from engine.pca.udf import *  # noqa: F401,F403
from engine.vol.udf import VolHistory  # noqa: F401
