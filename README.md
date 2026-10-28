# pricing_sheet

JPY OIS(TONA)スワップのプライシングシートと、その周辺の分析ブック。計算はPython/QuantLibを
xlwingsのUDFとしてExcelから呼び出す(VBAなし、ブックは手動計算)。

| ブック | 内容 |
|---|---|
| `PricingSheet.xlsm` | 任意のコンベンションのスワップのパーレート / PV / デルタ / リスクグリッド、ヒストリカルなレート推移(アウトライト・カーブ・フライ)、ASW、日銀会合のインプライド |
| `SwapAnalysis.xlsm` | スワップ・ボラのPCA |
| `BondOption.xlsm` | JGBの店頭オプション(現物オプション)のプライサー |

データの供給元はLSEG Workspace。リアルタイムのレートは常駐のフィード(Python)が受信し、
ヒストリカルは朝のバッチがローカルのDBに保存する。ASWスプレッドだけはNeon(Postgres)から
読み取り専用で取得する(詳細は[docs/decisions.md](docs/decisions.md))。

## 構成

```
LSEG Workspace(起動・ログイン中)
  ├─ 常駐のフィード: python -m engine.common.live_feed(設定 live_feed.toml)
  │     ▼
  │  data/live.db ──▶ 各ブックのLiveQuotes(シートのB1がLiveなら、Shift+F9のたびに最新を読む)
  │
  ├─ historical_data.xlsx(ヒストリカルのミッド・TONAフィキシング)
  └─ Python API(JGBの銘柄情報・引け値)
        │  朝に手動実行: python -m engine.common.morning_batch(+NeonからASW)
        ▼
     data/historical.db ──▶ ヒストリカル系・現物オプションのUDF(読むだけ)
```

| パス | 内容 |
|---|---|
| `templates/` | Excelブックの雛形(LSEGのデータを含まない)。直下にコピーして作業用として使う |
| `PricingSheet.xlsm` ・ `SwapAnalysis.xlsm` ・ `BondOption.xlsm` | 作業用のブック(計算はUDFのみ。`.gitignore`済み) |
| `PricingSheet.py` ・ `SwapAnalysis.py` ・ `BondOption.py` | xlwingsの制約(ブックと同名の.pyが必要)のため、UDFを再エクスポートするだけのファイル |
| `engine/common/` | 共有の計算エンジン・データ層(カーブ、プライサー、DB、朝バッチ、リアルタイムのフィード) |
| `engine/swap_pricing/` | PricingSheet.xlsmのUDF(`pricing/`・`historical/`・`asw/`) |
| `engine/pca/` | SwapAnalysis.xlsmのPCA |
| `engine/vol/` | SwapAnalysis.xlsmのIV vs HV |
| `engine/boj/` | SwapAnalysis.xlsmのBOJ会合スワップとスポットのずれ |
| `engine/bond_option/` | BondOption.xlsmのプライサー・ボラ |
| `historical_data.xlsx` | LSEGのヒストリカル生データを取得するブック(作業用で`.gitignore`済み) |
| `live_feed.toml` | リアルタイムのフィードの設定(購読するRIC・秒数) |
| `historical_curve.toml` | ヒストリカル日次カーブの設定(カーブの引き方・使うテナー) |
| `data/` | `historical.db`(日次カーブ等)・`live.db`(リアルタイム)・`live_feed.log`。LSEG由来のためコミットしない |
| `tools/template.py` | 作業用のブックから雛形を作る・検査する(`python -m tools.template`) |
| `tests/` | リグレッションテスト |
| `docs/` | 設計・運用ドキュメント |

## セットアップ(初回のみ)

必要なもの: Windows版Excel(x64)、LSEG Workspace(ExcelアドインとPython APIのデスクトップセッション)、
python.org公式の**x64版Python 3.11以上**(Microsoft Store版・ARM64版は不可)。

```
git clone https://github.com/Nova-jp/pricing-sheet.git
cd pricing-sheet
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
xlwings addin install
copy templates\*.xls* .
```

Excelのブックは`templates/`の雛形を直下にコピーして使う。直下のブックは作業用で、データ入りで
保存してもgitに入らない(`.gitignore`済み)。

このあと、xlwingsが使うPython(venvの`pythonw.exe`)の指定、各ブックでの「Import Functions」、
計算方法(手動)の確認などを行う。手順の詳細とトラブルシューティングは
[docs/setup_windows.md](docs/setup_windows.md)を参照。

ASWスプレッドの取得(朝バッチの一部)にはNeonへの接続文字列が必要。リポジトリ直下に`.env`というファイルを作り、
次の1行を書く(PCごとに1回):

```
NEON_CONNECTION_STRING=postgresql://<ユーザー>:<パスワード>@<ホスト>/<DB名>?sslmode=require
```

- 値はNeonのコンソールの「Connect」で表示される接続文字列(パスワード入り)をそのまま貼る(`OPERATIONS.md`も参照)。
  パスワードを含むので、`.env`はgitignore対象でコミットされない。値をコード・docsに書かない。
- 朝バッチはまず5432番(Postgres)で繋ぎ、繋がらないPC(会社のPC等)では同じ接続文字列のまま自動的に
  HTTPS(443番、`https://<ホスト>/sql`)で取得する。PCごとの切り替えの設定は要らない。認証はどちらの経路でも
  接続文字列のユーザー・パスワードでNeon側が行う(会社のPCでは`ASW_data`のSELECTしかできないロールの
  接続文字列にしておくと安全)。
- 設定が無い・どちらの経路でも繋がらない場合もバッチは止まらず、ASWの取り込みだけスキップされる(ログに理由が出る)。

## 毎日の使い方

以下のPythonのコマンドは、リポジトリ直下でvenvを有効にしてから(`.venv\Scripts\activate`)実行する。

1. **LSEG Workspaceを起動・ログインする**(フィードと、朝バッチのJGBの取得が使う)。
2. **リアルタイムのフィードを起動する**(コマンドプロンプトの窓は開いたままにする):

   ```
   python -m engine.common.live_feed
   ```

   「swap: 44銘柄を購読しました」「vol: 192銘柄を購読しました」と出れば動いている。止めるときはその窓で
   Ctrl+C。二重に起動すると「別のフィードが動いています」で止まる。ログは`data/live_feed.log`。
3. **朝のバッチで日次カーブDBを更新する**。`historical_data.xlsx`を開き、LSEGのデータ更新が終わったら
   **保存**してから(バッチは保存済みの値を読む。開いたままでよい):

   ```
   python -m engine.common.morning_batch
   ```

   2回目以降は入力が変わった日だけ作り直す。全日付を作り直すときは`--rebuild`、JGBの取得を
   省くときは`--skip-jgb`。
4. **ブックを開く**。Excelの計算方法は最初に開いたブックの設定が全体に適用されるので、
   `historical_data.xlsx`と一緒に使うときは**PricingSheet.xlsm等(手動計算)を先に開く**。
   開いた直後は前回保存した値なので、最初に1回**Ctrl+Alt+F9**を押す。
5. **再計算する**。各シートの1行目のB1が取得モード:
   - `Live`: そのシートで**Shift+F9**を押すたびに最新のレートを読んで再計算する(F9はブック全体)。
     フィードが止まっている・LSEGとの接続が正常でないときは、古い値で計算せずエラーになる。
   - `Fixed`: そのシートが最後にLiveで読んだレートのまま止める(追加した行も同じレートで計算される)。
   - D1(Updated)は、表示中のレートの値が最後に変わった時刻(相場が静かなら古いままになる)。

評価日はMainシートの`評価日`セル(空欄=今日=ライブのレート、過去日=DBのその日のカーブ)。
各ブックの使い方は[docs/setup_windows.md](docs/setup_windows.md)、現物オプションの計算規則と
シートの構成は[docs/bond_option.md](docs/bond_option.md)を参照。

## コードを更新したとき(git pull後)

- xlwingsリボンの「**Restart UDF Server**」を押す(押さないと古いコードで計算され続ける)。
  UDFの追加・引数の変更があれば「**Import Functions**」も(新しいUDFが`#NAME?`になるのはこれ)。
- フィード(`live_feed.py`・`live_feed.toml`)が変わったら、フィードを再起動する。
- `templates/`が更新されていれば、作業用のブックを`copy templates\*.xls* .`で置き換える
  (ブックに入力した取引なども雛形の内容に戻るので、必要なら先に控える)。

## ブックの構成を変えたとき(コミットする側)

Excelで保存して閉じてから、雛形を作り直してコミットする。雛形には数式と入力だけが残り、
計算結果・LSEGの受信データ・キャッシュ・ローカルのパスは消える:

```
python -m tools.template
git add templates
```

検査(`python -m tools.template --check`)はpre-commitフックとGitHub Actionsで走る
(フックはクローンごとに一度`git config core.hooksPath .githooks`で有効化)。

## テスト

```
python -m unittest discover -s tests
```

QuantLibの入ったx64のPython環境で実行する。LSEGのデータには依存しない。

## ドキュメント

- [docs/architecture.md](docs/architecture.md) — 全体構成、データフロー、モジュール・シート構成
- [docs/decisions.md](docs/decisions.md) — 設計判断とその理由
- [docs/roadmap.md](docs/roadmap.md) — 保留中/次フェーズの項目
- [docs/setup_windows.md](docs/setup_windows.md) — Windowsのセットアップ手順・各ブックの使い方・トラブルシューティング
- [docs/bond_option.md](docs/bond_option.md) — 現物オプション・プライサーの計算規則とブックの構成

## 注意(このリポジトリはPublic)

- 秘密情報・環境固有の値はコミットしない。環境固有の運用値は`OPERATIONS.md`
  (gitignore対象)にだけ書く。
- **LSEGのデータはコミットしない**(再配布禁止)。直下のブックと`data/`のDB・ログは作業用で
  `.gitignore`済み。コミットするのは`python -m tools.template`で作った`templates/`の雛形だけ
  (上の「ブックの構成を変えたとき」)。
