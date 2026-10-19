# 会社PC(Windows)でのセットアップ手順

必要なのは (1) このリポジトリのコード一式、(2) xlwingsアドイン、(3) LSEG Workspace
(`historical_data.xlsx`のデータ取得と、Python APIのリアルタイムのフィード・JGBの取得用)だけ。

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
     `engine/swap_pricing/udf.py`を再エクスポートしているだけ)からUDF
     (`PricingTable`, `FairRate`, `SwapPV`, `SwapDelta`, `SwapAnnuityDelta`,
     `NotionalForDelta`, `RiskGrid`, `MarketInfo`,
     `HistoricalRate`, `HistoricalRates`, `HedgeHistoricalRates`,
     `AswHistoricalRate`, `AswHistoricalRates`, `BojImpliedRates`)が
     Excelに登録される。
   - `_xlwings.conf`シートの「UDF Modules」に`engine.swap_pricing.udf`が
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
5. 過去起算スワップのTONA実績は、ヒストリカルDB(下の「毎朝の手順」のバッチで作る)から読む。
   pricingシートを使うときに`historical_data.xlsx`を開いておく必要はない。
   `historical_data.xlsx`(リポジトリ直下。雛形からコピーしたもの)はバッチの入力で、LSEG Workspace
   (Excelアドイン)にログインした状態で開くと`RDP.HistoricalPricing`でデータを自動取得する
   (Workspace未接続だと`#NAME?`、取得されないときはLSEGリボンの「更新」)。直下のブックは
   `.gitignore`済みなので、データ入りで保存してもコミットされない。

## 4.1 リアルタイムのレート(フィード)

PricingSheet・SwapAnalysis・BondOptionはLSEGから直接取らず、各フロントシートが、常駐のフィード
(`engine/common/live_feed.py`)が`data/live.db`に書いた値を読む(フィードは常に受信し続け、ブックは
手動計算のまま。理由は[decisions.md](decisions.md))。Excelは1つでよい(以前の自動計算のExcelは不要)。

1. LSEG Workspaceを起動・ログインしておく(フィードはWorkspaceのデスクトップセッションで受信する。
   パスワード等の設定は要らない)。
2. リポジトリ直下で、x64のvenvのPythonでフィードを起動する(コマンドプロンプトの窓は開いたままにする):

   ```
   python -m engine.common.live_feed
   ```

   約20秒で「swap: 44銘柄を購読しました」「vol: 192銘柄を購読しました」「接続の状態: ok」と出れば動いている。
   止めるときはその窓でCtrl+C(ブックはすぐに「フィードは停止しています」になる)。ログは`data/live_feed.log`。
   二重に起動すると「別のフィードが動いています」で止まる。
3. ExcelでPricingSheet.xlsm・SwapAnalysis.xlsm・BondOption.xlsmを開く。計算方法は「手動」。
4. 各フロントシート(Main・pricing・HistChart・BOJ・PCA・VolPCA・Option)の1行目が取得モードの欄。B1のプルダウンが
   `Live`なら、そのシートでShift+F9を押すたびに最新のレートを読んでそのシートだけ再計算する(F9はブック全体)。
   `Fixed`にすると、そのシートが最後にLiveで読んだレートのまま止まり、Shift+F9では入力を変えたセル
   (追加したコンベンションの行など)だけがそのレートで計算される(HistChartの重い再計算も止まる)。
   Ctrl+Alt+F9・UDFサーバの再起動でも固定の値は変わらない。D1(Updated)は、表示中のレートの値が
   フィードで最後に変わった時刻(相場が静かなら古いままになる。フィードの停止はエラーで分かる)。
   ブックを開いた直後は前回保存した値なので、最初に1回Ctrl+Alt+F9を押す。
   リアルタイムの列は折りたたんである(列の上の「+」で開く)。
5. `Live`のシートでフィードが止まっている(ハートビートが`stale_after_sec`=10秒より古い)か、LSEGとの接続が
   正常でないと、ライブのレートのセルがエラー(「フィードが止まっています」「LSEGとの接続が正常では
   ありません」等)になり、古い値では計算しない。止まっている間に直前のレートで計算するときはB1を`Fixed`にする。
6. 調整する値(秒数・RIC)は`live_feed.toml`にまとめてある。変えたらフィードを再起動する(Excel側は何もしなくて
   よい)。RICを増やしたら、PricingSheetで使うテナーは
   MainシートのA11:B54(Ticker/採否)にも足し、各シートのカーブ用の範囲(`Main!$A$11:$A$54`)を広げる
   (読む側はTickerで引き当てる)。
7. (任意)LSEGのApp Keyを自分で発行して使うときは、環境変数`LSEG_APP_KEY`に入れてからフィードを起動する
   (無ければライブラリ同梱の既定のキーで接続する)。キーは`OPERATIONS.md`に控え、コードやdocsに書かない。

## 5. 毎朝の手順(ヒストリカルの日次カーブDBの更新)

1. `historical_data.xlsx`をExcelで開き、LSEGのデータ更新が終わったら**保存**する
   (バッチは保存済みの値を読む。開いたままでよい)。
2. リポジトリ直下で実行する:

   ```
   python -m engine.common.morning_batch
   ```

   初回は全日付を作るので1〜2分(465日で約80秒)、2回目以降は入力が変わった日だけ作り直す。
   欠損テナーを外して作った日・日本の非営業日として除外した日・失敗した日が表示される。
   `data/historical.db`(LSEG由来のデータ、コミットしない)が作成・更新される。
3. カーブに使うテナーや引き方は`historical_curve.toml`で変更する(変更後に再実行すると、
   影響する日だけ作り直される)。全日付を作り直すときは`--rebuild`を付ける。
4. 評価日はMainシートのB4(空欄=今日)。`TODAY()`は使っていないので、日付が変わっても
   レートや入力が変わらない限り再計算されない(LSEGの更新でレートが変われば再計算される)。

## 5.1 分析ブック(SwapAnalysis.xlsm)

- PricingSheetと同じxlwings・venvで動く(VBAのxlwings参照は保存済み)。初回とUDFの変更時は
  `SwapAnalysis.xlsm`を開いて「Restart UDF Server」→「Import Functions」(`SwapAnalysis.py`から
  `PcaModel`等を登録)。
- 過去分はDB(`data/historical.db`)を読むので、上の毎朝のバッチを先に実行しておく
  (スワップの生ミッドとVolシートのボラを取り込む)。
- PCA(スワップ)/VolPCA(ボラ)シートの左の設定欄で推定(推定基準日・window・モード・標準化)と
  比較(比較日・主成分数)を入れてF9(またはShift+F9)。結果は同じシートの右側に出る。比較日が空欄なら
  そのシートのライブの値(1行目の取得モードがLiveなら読み直す)と比べる。

## 5.2 現物オプション(BondOption.xlsm)

- 計算規則とシートの構成は[bond_option.md](bond_option.md)。PricingSheetと同じxlwings・venvで動く
  (VBAのxlwings参照は雛形に入っている)。初回とUDFの変更時は`BondOption.xlsm`を開いて「Restart UDF Server」→
  「Import Functions」(`BondOption.py`から`JgbPrice`等を登録)。
- JGBの銘柄情報・引け値(JCL)は毎朝のバッチ(上の5.)がLSEGのPython APIで取る。**バッチを実行するときに
  LSEG Workspaceを起動・ログインしておく**(`requirements.txt`の`lseg-data`が必要。繋がらないとこの取得だけ
  スキップされ、「JGB取得: 失敗のためスキップ」と表示される)。カーブだけ作り直すときは`--skip-jgb`。
- ボラはフィードの`"vol"`を読む(4.1のフィードを起動しておく)。B1が取得モード(Live/Fixed)。
- 使い方: Optionシートの黄色の列(Bond・Type・Expiry・Settle lag・Repo・ATM/Strikeのスライド等)を入れてF9。
  評価日はB3(空欄=今日)。ATM単利は引け値+ATMのスライド。ボラはbp vol・ノーマルvolのどちらでも上書きできる。

## 6. コードを更新したとき(git pull後など)

- **xlwingsリボンの「Restart UDF Server」を押す**。Excelの裏で動くPython(UDFサーバ)は
  起動時のコードを読み込んだままなので、押さないと古いコードで計算され続ける。
- UDFの追加・削除や引数の変更があったときは、「Import Functions」も実行する。

## 7. 動作確認のチェックリスト

- [ ] `pricing`シートの行にコンベンションを入力し、`target fixrate`列に
      パーレートが表示される
- [ ] F9で再計算され、値が更新される(自動計算はオフのまま)
- [ ] フィード(4.1)を起動した状態で、取得モード(B1)が`Live`のシートでShift+F9を1回押すと、
      そのシートが最新のレートで計算され、D1の時刻が新しくなる。B1を`Fixed`にするとShift+F9・F9・
      Ctrl+Alt+F9でもレートが変わらず、Fixedのまま追加した行はそのレートで計算される
- [ ] フィードをCtrl+Cで止めると、`Live`のシートのShift+F9でライブのレートのセルが「フィードは停止しています」の
      エラーになり、`Fixed`のシートはそのまま計算できる
- [ ] `risk_flag`を1にした行があると、pricingシートY列のRiskGridに、Mainの
      テナー行の並びでテナー別デルタの合計が入る。AC3(合計)がAC4(行ごとのdeltaの
      risk_flag=1の合計)とほぼ一致する
      (先頭の1セルしか出ない場合は、数式の先頭に`@`が付いていないか確認)
- [ ] MainシートのA11:B54(カーブのテナー)のB列(採否フラグ)を0にしてF9すると、
      そのテナーがカーブから外れて`target fixrate`が変化し、RiskGridのその行が
      空欄になる。1に戻すと元に戻る
- [ ] `Main`シートの評価日に過去日(ヒストリカルDBにある日)を入れてF9すると、
      B4が「DB 日付」になり、pricingシートがその日のレートで計算される。
      空欄に戻すと今日(ライブ)に戻る
- [ ] `python -m engine.common.morning_batch`の実行後、`Historical`シートの2Y/5Y/10Y
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
  かかる。行ごとの結果は`PricingTable`(AQ3)が表全体を1回で計算する(100行で約1.3秒)。残りで重いのは
  RiskGrid(risk_flag=1の行数×テナー数)。詳細は`docs/decisions.md`。
- **すべてのブックでxlwingsが動かない(セルに「Could not load xlwings64-<版>.dll from either of the
  following folders」、Import Functionsも失敗)** → Windowsの**スマート アプリ コントロール**が、venvにある
  署名なしの`xlwings64-<版>.dll`をブロックしていることがある(2026-10-19に個人PCで発生。前日まで同じ設定で
  動いていた)。Python側の問題ではない。確認方法:
  - イベント ビューアー → アプリケーションとサービス ログ → Microsoft → Windows → CodeIntegrity →
    Operational に、EXCEL.EXEが`xlwings64-…dll`を読み込もうとしてブロックされた記録(ID 3033/3077)がある。
  - またはvenvのPythonで`import ctypes; ctypes.WinDLL(r"<venv>\xlwings64-<版>.dll")`が
    WinError 4551(アプリケーション制御ポリシーによってブロック)になる。
  対処はまずPCの再起動を試す(ポリシーと評価のキャッシュが更新される)。直らなければ、Windows セキュリティ →
  アプリとブラウザー コントロール → スマート アプリ コントロールをオフにする(**一度オフにするとWindowsを
  再インストールしないとオンに戻せない**ので、PCの持ち主・管理者が判断する)。会社PCでは管理者のポリシーによる。
