# pricing_sheet

JPY OIS(TONA)スワップのプライシングシート。Excel(UDF専用、VBAなし) +
Python/QuantLib + LSEG Workspace(データ供給元はLSEGのみ。クラウドは使わない)。
詳細設計・運用情報は`docs/`配下を参照(このファイルは要点のみ、肥大化させない)。

- 全体構成・データフロー・モジュール一覧: `docs/architecture.md`
- 設計判断とその理由: `docs/decisions.md`
- 保留中/次フェーズの項目: `docs/roadmap.md`
- Windowsセットアップ手順: `docs/setup_windows.md`
- 環境固有の運用値: `OPERATIONS.md`(gitignore対象、直接見るか
  ユーザーに確認する。コード中にハードコードしない)

## 触ってよい範囲

- `swap_pricing/`, `PricingSheet.py`, `PricingSheet.xlsm`, `historical_data.xlsx`,
  `historical_curve.toml`, `tests/`, `docs/`, `tools/`, `templates/` が本プロジェクト。分析ブック
  `SwapAnalysis.xlsm`・`SwapAnalysis.py`・`swap_analysis/`(PCA等、pricingとは別ブック)も含む。

## 設計方針(必ず守る)

- **UDF専用、VBAなし**。QuantLibが必要な計算(パーレート/PV/デルタ/
  リスクの合算(RiskGrid)/ヒストリカル)だけを`swap_pricing/udf.py`のUDFにする。
  カーブのスプレッド・フライ・合計のような四則演算はExcel自身の数式に
  任せる。「高度な計算が必要そうなら、勝手にUDF化・複雑化せず先に相談する」。
- `PricingSheet.py`は`swap_pricing/udf.py`の再エクスポート専用
  (`from swap_pricing.udf import *`)。ワークブックと同名の.pyファイルに
  UDF実体が必要というxlwings(Windows)の制約のため。VBAのRunPython/
  マクロボタンでUDFセルを静的値で上書きするような実装は絶対に作らない
  (過去に実際に事故が起きた設計)。
- 配列を返すUDFに`@xw.ret(expand="table")`を使わない(旧式配列数式に書き換わり
  動的配列のスピルと衝突する)。配列をそのまま返しExcelのスピルに任せる。
- 価格計算の値はQuantLibを正とし、手計算の再実装による検証ロジックは作らない。
- 評価日はMainシートの`評価日`セル(空欄=今日)に集約し、UDFの引数に`TODAY()`を使わない
  (volatileでF9のたびに全UDFが再計算される)。カーブは`swap_pricing/market.py`で全UDFが共有し、
  UDFごと・行ごとにブートストラップしない。
- データの供給元はLSEG Workspaceだけ(RealTimeシートと`historical_data.xlsx`)。
  ヒストリカルは手動実行のバッチ(`python -m swap_pricing.curve_batch`)が日次カーブを
  `data/historical.db`に保存し、日中のUDFはそれを読むだけ。カーブの引き方は
  `curve.CURVE_BUILDERS`に閉じ込め、保存・評価側は日付→DFしか扱わない(引き方に依存させない)。
- `historical_data.xlsx`(LSEG生データ、過去起算のTONAフィキシング供給元)は
  LSEG Workspaceの`RDP.HistoricalPricing`と
  Excel標準関数だけで構成する。

## リポジトリはPublic(コミット前に必ず確認)

- 秘密情報・環境固有の値(接続文字列、社内のパス等)は
  絶対にコードやdocs/に書かない。運用値は`OPERATIONS.md`(gitignore済み)にのみ書く。
- **LSEGのデータはコミットしない**(LSEGコンテンツは再配布禁止)。直下のブック(`PricingSheet.xlsm`・
  `SwapAnalysis.xlsm`・`historical_data.xlsx`)は作業用で、データが入るため`.gitignore`済み。
  コミットするのは`python -m tools.template`で作った`templates/`の雛形だけ(数式の計算結果・
  スピル結果・LSEGの受信領域・グラフ/外部リンク/RTDのキャッシュ・ローカルのパス・作成者名を消したもの)。
  ブックの構成(シート・数式)を変えたら、Excelで保存して閉じてから`python -m tools.template`を実行し、
  `templates/`をコミットする。雛形を手で編集・掃除しない。
- 検査(`python -m tools.template --check`)はpre-commitフック(`git config core.hooksPath .githooks`で
  有効化。クローンごとに1回)とGitHub Actionsで走る。検査で「知らない部品」が出たら、データが入る
  場所かを確かめて`tools/template.py`の規則か許可の一覧に加える(検査を緩めて通さない)。
  LSEGアドインが値として書き込むシートを増やしたら`RECEIVE_AREAS`に宣言する。
- 数式をCOMで書き換えるときは`.Formula2`を使う(`.Formula`だと旧式の数式になり、配列UDFが
  スピルせず1セルしか返らない。検査が配列UDFのセルの動的配列(`cm`属性)を確かめる)。

## その他

- コメント・ドキュメント・コミュニケーションは日本語。
- 市場レートの正確な逆算(ブートストラップの精度)は本プロジェクトの
  ハード要件。カーブ関連の変更をする際は、既存の検証済み許容誤差
  (機械精度〜0.1bp未満)を落とさないこと。
