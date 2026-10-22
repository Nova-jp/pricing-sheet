"""
Excelのセル関数(UDF)として直接呼び出せる計算関数群。xlwingsのExcelアドイン経由で
`=FairRate(...)`のように、ネイティブのExcel関数と全く同じ感覚でどのセルにも置ける。

■ このファイルの役割
  `PricingSheet.py`(ワークブックと同名)が`from engine.swap_pricing.udf import *`で読み込む
  単一の窓口。xlwingsのExcelアドイン(Windows)は「Import Functions」実行時に
  **ワークブックと同名の.pyファイル**からUDFを探すため、この1段の間接参照が必要
  (詳細はdocs/architecture.md)。実体はシート(機能)ごとに分かれている:
    - pricing/udf.py   : Main/pricingシート(PricingTable, FairRate, SwapPV, SwapDelta,
                          SwapAnnuityDelta, NotionalForDelta, RiskGrid, MarketInfo)
    - historical/udf.py: HistChartシート(HistoricalRate, HistoricalRates, HedgeHistoricalRates)
    - asw/udf.py       : ASWシート(AswHistoricalRate, AswHistoricalRates)・ASWPricerシート(AswPricingTable)
    - common/live_udf.py: 各フロントシート(LiveQuotes, LiveSnapshot, LiveInfo。フィード(live_feed.py)が書いた
                          リアルタイムのレートを読む。SwapAnalysis.xlsmと共通)
  Excel境界の型変換・評価日解決・カーブ一式(`_market`)はこれらが共有する
  `engine/common/udf_common.py`に集約している。

■ 設計方針(どの機能のUDFにも共通)
  - QuantLibが必要な計算(パーレート・PV・delta・notional逆算・リスクの合算・ヒストリカル)
    だけをUDFにする。カーブ(スプレッド)やフライ、合計といった単純な四則演算はExcel自身の
    数式に任せる。
  - 計算方法はブックの計算方法を「手動」にし、F9(再計算)で都度実行する想定。
    UDFは非volatileのままでよい。VBAコードは一切不要。
  - 評価日は Mainシートの評価日セル(名前「評価日」)を各UDFに渡す。空欄なら今日。
    TODAY()は使わない(volatileなのでF9のたびに全UDFが再計算されてしまうため)。
  - 評価日が今日なら ライブのレート、過去日ならヒストリカルDBのその日の
    レートでカーブを組む(判定は udf_common._market だけ。詳細はengine/common/market.py)。
    どちらも フラグ/Ticker/Value の範囲(各シートのカーブ用の範囲の TAKE(…#,,3)。旧 RealTime!B2:D45)
    でテナーを選ぶ。採否フラグ(MainシートのB列): 1(または空欄)=カーブ構築に使う、0=使わない。
  - カーブ(基準カーブとバンプしたカーブ)は market.curve_set で全行・全UDFが共有する。
    同じレート・評価日なら、ブートストラップはその最初の1回だけ。
  - 過去起算スワップ用のTONA実績フィキシングは、ヒストリカルDB(朝のバッチがhistorical_data.xlsxの
    Fixingシートから取り込んだもの)から読む。評価日より前の日だけを使う(market_data.Market)。
  - ヒストリカル系UDF(HistoricalRate, HistoricalRates)はブートストラップをせず、
    朝バッチ(python -m engine.common.morning_batch)が作った日次カーブDB
    (data/historical.db)を読むだけ。TONA実績もDBに取り込み済みのものを使う。
  - 配列を返すUDF(PricingTable, RiskGrid, HistoricalRate, HistoricalRates)は、
    Excelの動的配列のスピルにそのまま任せる。xlwingsの @xw.ret(expand="table") は使わない
    (計算後に非同期でセル範囲を旧式の配列数式に書き換えるため、動的配列の
    スピル参照(B1#等)や数式の@付与と衝突する。実際に旧Historicalシートが
    旧式配列化して先頭1セルしか表示されなくなっていた)。

■ 使い方(Windows実機でのセットアップ)
  1. pip install xlwings
  2. xlwings addin install (Excelアドインを1度だけインストール)
  3. ブックの _xlwings.conf シートの "UDF Modules" に "engine.swap_pricing.udf" を設定
     (または xlwings のRibbonから対象ブックにこのモジュールを紐付ける)
  4. ブックの計算方法を手動に設定(このモジュールでは行わない、Excel側の設定)
"""

import xlwings as xw

from engine.common.live_udf import LiveInfo, LiveQuotes, LiveSnapshot  # noqa: F401
from engine.swap_pricing.asw.udf import *  # noqa: F401,F403
from engine.swap_pricing.historical.udf import *  # noqa: F401,F403
from engine.swap_pricing.pricing.udf import *  # noqa: F401,F403


@xw.func
def Ping(value=1) -> float:
    """xlwings配線の疎通確認用(QuantLib計算を伴わない)。

    Import Functions後にこの関数だけが失敗する場合、原因はUDFの配線
    (xlwingsアドイン/ブックのVBAプロジェクト)側にある。
    """
    return float(value) * 2
