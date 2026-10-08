# 会社PC(Windows)でのセットアップ手順

必要なのは (1) このリポジトリのコード一式、(2) xlwingsアドイン、(3) LSEG Workspace
(`historical_data.xlsx`とRealTimeシートのデータ取得用)だけ。

## 1. リポジトリの取得

```
git clone https://github.com/Nova-jp/pricing-sheet.git
```

このリポジトリはGitHub上でPublicなので、認証なしでcloneできる。

Excelのブックは、LSEGのデータを含まない雛形だけが`templates/`に入っている。作業用として
リポジトリ直下にコピーして使う(直下のブックは`.gitignore`済みで、データ入りで保存してもgitに入らない):

```
copy templates\*.xls* .
```

`git pull`で雛形が更新されたときは、同じコマンドで作業用のブックを置き換える(データは開いたときに
LSEGから取り直される)。ブックの構成を変えてコミットする側の手順はCLAUDE.md(`python -m tools.template`)。
コミットするPCでは、雛形の検査をコミット時に走らせるため一度だけ`git config core.hooksPath .githooks`を実行する。

## 2. Python環境

- **python.org公式のx64版Python**を使う(Excelのbit数=x64に揃える)。Microsoft Store版と
  ARM64版は使わない(QuantLibにWindows ARM64のwheelが無く、Store版はxlwingsから
  起動できない)。venvを作ってその中にインストールする:

  ```
  pip install -r requirements.txt
  ```

## 3. xlwings Excelアドインのインストール(初回のみ)

```
xlwings addin install
```

Excelを再起動すると、リボンに「xlwings」タブが表示される。

- **使うPythonを指定する**: `%USERPROFILE%\.xlwings\xlwings.conf`の`"INTERPRETER_WIN"`に
  venvの**`pythonw.exe`**(`python.exe`ではない)を指定する。このファイルを手で書く場合は
  **改行コードをCRLF**にし、最終行にも改行を入れる(LFだけだとxlwingsリボンで
  「実行時エラー62 ファイルにこれ以上データがありません」になる。リボンのSaveで作れば元からCRLF)。
- **VBAプロジェクトへのアクセスを許可する**: ファイル→オプション→トラストセンター→
  マクロの設定→「VBAプロジェクト オブジェクト モデルへのアクセスを信頼する」をオン
  (Import FunctionsがブックにUDFのVBAラッパーを書き込むために必要)。
- **ブックのVBAからxlwingsアドインを参照する**: `PricingSheet.xlsm`は雛形の時点で
  参照が入っているので通常は不要。UDFのセルが「オブジェクトが必要です」になるときは、
  ブックを開いてAlt+F11→ツール→参照設定→`xlwings`にチェックして上書き保存する。

## 4. UDFの読み込み

1. `PricingSheet.xlsm`をExcelで開く。
2. xlwingsリボンの **「Import Functions」** をクリック。
   - これにより、ワークブックと同名の`PricingSheet.py`(実体は
     `swap_pricing/udf.py`を再エクスポートしているだけ)からUDF
     (`FairRate`, `SwapPV`, `SwapDelta`, `SwapAnnuityDelta`,
     `NotionalForDelta`, `RiskGrid`, `MarketInfo`, `HistoricalOutright`,
     `HistoricalRate`, `HistoricalRates`)が
     Excelに登録される。
   - `_xlwings.conf`シートの「UDF Modules」に`swap_pricing.udf`が
     設定済みだが、Windowsの「Import Functions」はワークブック名と
     同名の`.py`ファイルを見に行く仕様のため、`PricingSheet.py`が
     実際にUDF定義を含んでいる(再エクスポートしている)ことが必須。
3. 計算方法が「手動」になっていることを確認(通常はブック保存時の設定が
   そのまま反映される。ずれていたら 数式タブ→計算方法の設定→手動)。
   **Excelの計算方法は最初に開いたブックの設定がアプリ全体に適用される**ため、
   `historical_data.xlsx`(自動計算)と一緒に使うときは**PricingSheet.xlsmを先に開く**。
   逆順で開くと自動計算になり、編集のたびにUDFが走る。
4. 任意のセルでF9を押して再計算されることを確認する
   (VBAマクロ・ボタンは一切不要)。
5. 過去起算スワップを評価する場合は、リポジトリ直下の`historical_data.xlsx`
   (LSEGの生データブック。雛形からコピーしたもの)も開いておく。LSEG Workspace(Excelアドイン)にログインした状態で開くと
   `RDP.HistoricalPricing`でデータを自動取得する(Workspace未接続だと`#NAME?`、
   取得されないときはLSEGリボンの「更新」)。直下のブックは`.gitignore`済みなので、
   データ入りで保存してもコミットされない。pricingシートのUDFは
   最後の引数で`[historical_data.xlsx]Fixing!$A$5#`(TONA実績)を参照している。

## 5. 毎朝の手順(ヒストリカルの日次カーブDBの更新)

1. `historical_data.xlsx`をExcelで開き、LSEGのデータ更新が終わったら**保存**する
   (バッチは保存済みの値を読む。開いたままでよい)。
2. リポジトリ直下で実行する:

   ```
   python -m swap_pricing.curve_batch
   ```

   初回は全日付を作るので1〜2分(465日で約80秒)、2回目以降は入力が変わった日だけ作り直す。
   欠損テナーを外して作った日・日本の非営業日として除外した日・失敗した日が表示される。
   `data/historical.db`(LSEG由来のデータ、コミットしない)が作成・更新される。
3. カーブに使うテナーや引き方は`historical_curve.toml`で変更する(変更後に再実行すると、
   影響する日だけ作り直される)。全日付を作り直すときは`--rebuild`を付ける。
4. 評価日はMainシートのB3(空欄=今日)。`TODAY()`は使っていないので、日付が変わっても
   レートや入力が変わらない限り再計算されない(LSEGの更新でレートが変われば再計算される)。

## 5.1 分析ブック(SwapAnalysis.xlsm)

- PricingSheetと同じxlwings・venvで動く(VBAのxlwings参照は保存済み)。初回とUDFの変更時は
  `SwapAnalysis.xlsm`を開いて「Restart UDF Server」→「Import Functions」(`SwapAnalysis.py`から
  `PcaModel`等を登録)。
- 過去分はDB(`data/historical.db`)を読むので、上の毎朝のバッチを先に実行しておく
  (スワップの生ミッドとVolシートのボラを取り込む)。
- PCA設定/VolPCA設定シートで推定(推定基準日・window・モード・標準化)と比較(比較日・主成分数)を
  入れてF9。比較日が空欄ならRealTime/RealTimeVolシートのライブの値と比べる。

## 6. コードを更新したとき(git pull後など)

- **xlwingsリボンの「Restart UDF Server」を押す**。Excelの裏で動くPython(UDFサーバ)は
  起動時のコードを読み込んだままなので、押さないと古いコードで計算され続ける。
- UDFの追加・削除や引数の変更があったときは、「Import Functions」も実行する。

## 7. 動作確認のチェックリスト

- [ ] `pricing`シートの行にコンベンションを入力し、`target fixrate`列に
      パーレートが表示される
- [ ] F9で再計算され、値が更新される(自動計算はオフのまま)
- [ ] `risk_flag`を1にした行があると、pricingシートZ列のRiskGridに、RealTimeの
      テナー行の並びでテナー別デルタの合計が入る。AD1(合計)がAD2(行ごとのdeltaの
      risk_flag=1の合計)とほぼ一致する
      (先頭の1セルしか出ない場合は、数式の先頭に`@`が付いていないか確認)
- [ ] `RealTime`シートのスポット行のB列(採否フラグ)を0にしてF9すると、
      そのテナーがカーブから外れて`target fixrate`が変化し、RiskGridのその行が
      空欄になる。1に戻すと元に戻る
- [ ] `Main`シートの評価日に過去日(ヒストリカルDBにある日)を入れてF9すると、
      B4が「DB 日付」になり、pricingシートがその日のレートで計算される。
      空欄に戻すと今日(RealTime)に戻る
- [ ] `python -m swap_pricing.curve_batch`の実行後、`Historical`シートの2Y/5Y/10Y
      アウトライトが下方向に広がり、2s10sカーブ・2s5s10sフライも表示される
- [ ] `HistChart`シートの系列表に行番号を1つ(アウトライト)/2つ(カーブ)/3つ(フライ)
      入れてF9すると、グラフに最大3本の線が重なって表示される。カーブ/フライを含むと
      単位がbpになる。表示を×にした系列は線が消える
- [ ] start・endに`Z26`・`U28`のようなIMMコードを入れると、roll conv列が
      `IMM`になり計算される。roll convを`STD`に上書きするとSTDで計算される
- [ ] 過去日付のstart(例: 3か月前)の行が計算される。`historical_data.xlsx`を
      閉じると「TONAフィキシングが…不足しています」のエラーになる
- [ ] xlwingsの「Run main」等のマクロ実行ボタンは存在しない
      (VBAは完全に削除済み)

## トラブルシューティング

- **F9で計算されない/`#NAME?`エラーになる** → 「Import Functions」を
  再実行する。`PricingSheet.py`が単なる`main()`しか持たない古いバージョンに
  戻っていないか確認する(過去に実際にこの状態でハマった)。
- **過去起算の行が「TONAフィキシングがN日分不足しています」になる** →
  `historical_data.xlsx`が開いているか、そのFixingシートの取得期間が
  起算日(未払いクーポン期間の開始日)をカバーしているか確認する。
- **ヒストリカルが「ヒストリカルカーブDBがありません」になる/値が古い** → 上記「毎朝の手順」の
  バッチを実行する。
- **UDFのセルが「オブジェクトが必要です」になる** → ブックのVBAにxlwingsの参照が無い
  (上記3.の参照設定)。
- **コードを更新したのに結果が変わらない/新しいUDFが`#NAME?`** → 「Restart UDF Server」
  →「Import Functions」(上記6.)。
- **pricingシートの再計算が遅い** → UDFのセル1つごとにExcel↔Pythonの往復(個人PCで約170ms)が
  かかる。計算自体はカーブを共有していて数ms(`docs/decisions.md`)。
