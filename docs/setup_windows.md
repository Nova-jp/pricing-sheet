# 会社PC(Windows)でのセットアップ手順

会社PCはNeonの接続情報を一切持たない。必要なのは (1) このリポジトリの
コード一式、(2) GCSから落とした`data/curve_cache.db`、(3) xlwings
アドインだけ。

## 1. リポジトリの取得

```
git clone https://github.com/Nova-jp/pricing-sheet.git
```

このリポジトリはGitHub上でPublicなので、認証なしでcloneできる。

## 2. Python環境

```
pip install -r requirements.txt
```

`psycopg2-binary`(Neon接続用)も一覧に含まれるが、会社PC上のUDF実行時
には呼ばれない(Neonへのアクセスは`cloud/`側のCloud Run Jobでのみ発生)。
インストール自体は失敗しないはず(バイナリwheel)。

## 3. ヒストリカルキャッシュDBの取得

`data/curve_cache.db`を、GCSの公開URL(`OPERATIONS.md`参照、社内の別
チャネルで共有)からダウンロードして`data/`直下に配置する。

```powershell
Invoke-WebRequest -Uri "<OPERATIONS.mdに記載の公開URL>" -OutFile "data\curve_cache.db"
```

このファイルは毎朝07:00 JSTに自動更新されるため、最新のヒストリカルを
見たい場合は都度再ダウンロードする(自動化は未実装、必要なら次フェーズで
タスクスケジューラ連携を検討)。

## 4. xlwings Excelアドインのインストール(初回のみ)

```
xlwings addin install
```

Excelを再起動すると、リボンに「xlwings」タブが表示される。

## 5. UDFの読み込み

1. `PricingSheet.xlsx`をExcelで開く。
2. xlwingsリボンの **「Import Functions」** をクリック。
   - これにより、ワークブックと同名の`PricingSheet.py`(実体は
     `swap_pricing/udf.py`を再エクスポートしているだけ)からUDF
     (`FairRate`, `SwapPV`, `SwapDelta`, `SwapAnnuityDelta`,
     `NotionalForDelta`, `HistoricalOutright`, `BucketedDelta`)が
     Excelに登録される。
   - `_xlwings.conf`シートの「UDF Modules」に`swap_pricing.udf`が
     設定済みだが、Windowsの「Import Functions」はワークブック名と
     同名の`.py`ファイルを見に行く仕様のため、`PricingSheet.py`が
     実際にUDF定義を含んでいる(再エクスポートしている)ことが必須。
3. 計算方法が「手動」になっていることを確認(通常はブック保存時の設定が
   そのまま反映される。ずれていたら 数式タブ→計算方法の設定→手動)。
4. 任意のセルでF9を押して再計算されることを確認する
   (VBAマクロ・ボタンは一切不要)。

## 6. 動作確認のチェックリスト

- [ ] `pricing`シートの行にコンベンションを入力し、`target fixrate`列に
      パーレートが表示される
- [ ] F9で再計算され、値が更新される(自動計算はオフのまま)
- [ ] `risk_flag`を1にした行で、リスクグリッド(Z列以降)に値が入る
- [ ] `Historical`シートの2Y/5Y/10Yアウトライト、カーブ、フライが
      表示される
- [ ] xlwingsの「Run main」等のマクロ実行ボタンは存在しない
      (VBAは完全に削除済み)

## トラブルシューティング

- **F9で計算されない/`#NAME?`エラーになる** → 「Import Functions」を
  再実行する。`PricingSheet.py`が単なる`main()`しか持たない古いバージョンに
  戻っていないか確認する(過去に実際にこの状態でハマった)。
- **ヒストリカルの値が古い/空** → `data/curve_cache.db`を再ダウンロードする。
- **Neonに関するエラーが出る** → 会社PC上で発生してはいけない
  (Neonアクセスはcloud側のみ)。`swap_pricing/neon_client.py`や
  `morning_batch.py`をUDF経路から誤って呼んでいないか確認する。
