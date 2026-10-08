# pricing_sheet

JPY OIS(TONA)スワップのプライシングシート。Excelブック(`PricingSheet.xlsm`)上で、
任意のコンベンションのスワップについて

- リアルタイムのパーレート / PV / デルタ / リスクグリッド
- ヒストリカルなレート推移(アウトライト・カーブ・フライ)

を計算・表示する。計算はPython/QuantLibをxlwingsのUDFとして呼び出す(VBAなし)。
データの供給元はLSEG Workspaceだけで、クラウドやDBサーバは使わない。

## 構成

```
LSEG Workspace
  ├─ PricingSheet.xlsm の RealTimeシート ──────▶ UDF(その場でカーブ構築)
  └─ historical_data.xlsx(Mid / Fixing)
        │  朝に手動実行: python -m swap_pricing.curve_batch
        ▼
     data/historical.db(ローカルのSQLite)──▶ ヒストリカル系UDF(読むだけ)
```

| パス | 内容 |
|---|---|
| `templates/` | Excelブックの雛形(LSEGのデータを含まない)。直下にコピーして作業用として使う |
| `PricingSheet.xlsm` | 本体のExcelブック(計算はUDFのみ。作業用で`.gitignore`済み) |
| `PricingSheet.py` | xlwingsの制約のため`swap_pricing/udf.py`を再エクスポートするだけのファイル |
| `swap_pricing/` | 計算エンジン(QuantLibベース)とUDF定義 |
| `historical_data.xlsx` | LSEGのヒストリカル生データを取得するブック(作業用で`.gitignore`済み) |
| `tools/template.py` | 作業用のブックから雛形を作る・検査する(`python -m tools.template`) |
| `historical_curve.toml` | ヒストリカル日次カーブの設定(カーブの引き方・使うテナー) |
| `data/historical.db` | 日次カーブDB(バッチが作成。LSEG由来のためコミットしない) |
| `tests/` | リグレッションテスト |
| `docs/` | 設計・運用ドキュメント |

## セットアップ(初回のみ)

必要なもの: Windows版Excel(x64)、LSEG Workspace(Excelアドイン)、python.org公式の
**x64版Python 3.11以上**(Microsoft Store版・ARM64版は不可)。

```
git clone https://github.com/Nova-jp/pricing-sheet.git
cd pricing-sheet
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
xlwings addin install
copy templates\*.xls* .
```

Excelのブックは`templates/`の雛形(LSEGのデータを含まない)を直下にコピーして使う。
直下のブックは作業用で、データ入りで保存してもgitに入らない(`.gitignore`済み)。

このあと、xlwingsが使うPython(venvの`pythonw.exe`)の指定、ブックでの
「Import Functions」、計算方法の確認などを行う。手順の詳細とトラブルシューティングは
[docs/setup_windows.md](docs/setup_windows.md)を参照。

## 毎朝の手順

1. `PricingSheet.xlsm`を**先に**開き、続けて`historical_data.xlsx`を開く
   (Excelの計算方法は最初に開いたブックの設定=手動が全体に適用されるため)。
2. `historical_data.xlsx`でLSEGのデータ更新が終わったら**保存**する
   (バッチは保存済みの値を読む。開いたままでよい)。
3. venvのPythonで、リポジトリ直下から日次カーブDBを更新する:

   ```
   .venv\Scripts\activate
   python -m swap_pricing.curve_batch
   ```

   2回目以降は入力が変わった日だけ作り直す。全日付を作り直すときは`--rebuild`を付ける。
4. `PricingSheet.xlsm`でF9を押して再計算する。評価日はMainシートの`評価日`セル(空欄=今日)。

コードを更新(`git pull`)したときは、xlwingsリボンの「Restart UDF Server」を押す
(UDFの追加・引数の変更があれば「Import Functions」も)。`templates/`が更新されていれば、
作業用のブックを`copy templates\*.xls* .`で置き換える。

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
- [docs/setup_windows.md](docs/setup_windows.md) — Windowsのセットアップ手順・トラブルシューティング

## 注意(このリポジトリはPublic)

- 秘密情報・環境固有の値はコミットしない。環境固有の運用値は`OPERATIONS.md`
  (gitignore対象)にだけ書く。
- **LSEGのデータはコミットしない**(再配布禁止)。直下のブックは作業用で`.gitignore`済み。
  コミットするのは`python -m tools.template`で作った`templates/`の雛形だけ
  (上の「ブックの構成を変えたとき」)。
