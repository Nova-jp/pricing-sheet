# pricing_sheet

JPY OIS(TONA)スワップのプライシングシート。Excel(UDF専用、VBAなし) +
Python/QuantLib + GCP(Neon→Cloud Run→GCS)。詳細設計・運用情報は
`docs/`配下を参照(このファイルは要点のみ、肥大化させない)。

- 全体構成・データフロー・モジュール一覧: `docs/architecture.md`
- 設計判断とその理由: `docs/decisions.md`
- 保留中/次フェーズの項目: `docs/roadmap.md`
- Windowsセットアップ手順: `docs/setup_windows.md`
- 実際のGCPリソース名・URL: `OPERATIONS.md`(gitignore対象、直接見るか
  ユーザーに確認する。コード中にハードコードしない)

## 触ってよい範囲

- `swap_pricing/`, `PricingSheet.py`, `PricingSheet.xlsx`, `cloud/` が本プロジェクト。
- `src/`, `main.py`, `create_excel_sheet.py`, `vba/BOJ_Analysis.bas` は
  **既存の別プロジェクト(BOJ会合インプライド分析ツール)。明示的な指示が
  ない限り一切触らない。**

## 設計方針(必ず守る)

- **UDF専用、VBAなし**。QuantLibが必要な計算(パーレート/PV/デルタ/
  ヒストリカル/バケットデルタ)だけを`swap_pricing/udf.py`のUDFにする。
  カーブのスプレッド・フライ・合計のような四則演算はExcel自身の数式に
  任せる。「高度な計算が必要そうなら、勝手にUDF化・複雑化せず先に相談する」。
- `PricingSheet.py`は`swap_pricing/udf.py`の再エクスポート専用
  (`from swap_pricing.udf import *`)。ワークブックと同名の.pyファイルに
  UDF実体が必要というxlwings(Windows)の制約のため。VBAのRunPython/
  マクロボタンでUDFセルを静的値で上書きするような実装は絶対に作らない
  (過去に実際に事故が起きた設計)。
- 会社PCはNeonの接続情報を一切持たない。Neonアクセスは`cloud/`側の
  Cloud Run Jobでのみ発生し、会社PCは`data/curve_cache.db`をGCSの
  公開オブジェクトからダウンロードするだけ。
- **このGitHubリポジトリはPublic。** 秘密情報・実GCPリソース名
  (バケット名、オブジェクトパス、接続文字列等)は絶対にコードやdocs/に
  書かない。運用値は`OPERATIONS.md`(gitignore済み)にのみ書く。

## その他

- コメント・ドキュメント・コミュニケーションは日本語。
- 市場レートの正確な逆算(ブートストラップの精度)は本プロジェクトの
  ハード要件。カーブ関連の変更をする際は、既存の検証済み許容誤差
  (機械精度〜0.1bp未満)を落とさないこと。
