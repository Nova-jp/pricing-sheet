# pricing_sheet ドキュメント目次

JPY OIS(TONA)スワップのプライシングシート(Excel + Python/QuantLib + LSEG)の
設計・運用情報。セッションをまたいでも参照できるよう、このフォルダに
集約している。

- [architecture.md](architecture.md) — 全体構成、データフロー、モジュール一覧、シート構成
- [decisions.md](decisions.md) — 主要な設計判断とその理由(なぜそうしたか)
- [roadmap.md](roadmap.md) — 保留中/次フェーズ以降に回した項目
- [setup_windows.md](setup_windows.md) — 会社PC(Windows)でのセットアップ手順
- `../OPERATIONS.md`(**gitignore対象、リポジトリ直下**) — 環境固有の運用値。このリポジトリは
  **GitHub上でPublic**のため、秘密情報・環境固有の値は絶対にコミットしない。

## 関連ファイル(コード側)

- `swap_pricing/` — 計算エンジン本体(QuantLibベース)。各ファイル冒頭のdocstringに
  役割を明記しているので、詳細はコードを直接参照するのが最新かつ正確。
- `swap_pricing/udf.py` — Excelから直接呼べるUDF定義(`PricingSheet.py`が再エクスポート)
- `swap_pricing/conventions.py` — 業務上の表記(テナー・dcf・freq・roll・IMMコード・startのテナー)→
  QuantLibの対応表とカレンダー
- `tests/` — リグレッションテスト(`python -m unittest discover -s tests`、QuantLibの入った
  x64 Python環境で実行)。価格の値はQuantLibを正とし、自前ロジックだけをテストする
  - `test_curve_repricing.py` — 市場レート逆算精度の要件(機械精度〜0.1bp未満)
  - `test_past_start.py` — 過去起算(フィキシング不足時のエラー等)と評価日の扱い
  - `test_market_risk.py` — カーブ一式の共有とデルタ(旧手順との一致)、RiskGrid、評価日の切り替え、startのテナー入力、HistChartの最新日(RealTime)
  - `test_imm.py` — IMMコードの解釈とIMMロールのスケジュール、startのテナー表記の判定
  - `test_historical.py` — ヒストリカル計算(FIXED/ROLLINGの日付の決め方・営業日数の固定、先読みしないフィキシング、満期後の除外)
  - `test_curve_store.py` / `test_curve_batch.py` — 日次カーブDBの保存・復元精度とバッチ
- `historical_curve.toml` — ヒストリカル日次カーブの設定(引き方・使うテナー)。
  `python -m swap_pricing.curve_batch`が読む
- `templates/` — Excelブックの雛形(LSEGのデータを含まない)。直下にコピーして作業用として使う。
  直下のブックは`.gitignore`済み。雛形は`python -m tools.template`(`tools/template.py`)で作る
  (仕組みはarchitecture.mdの「Excelブックの雛形」)
- `PricingSheet.xlsm` — 成果物のExcelブック本体(計算はUDFのみ。マクロ有効ブックだが
  VBAはxlwingsアドイン参照の保持用で業務ロジックは持たない)
- `historical_data.xlsx` — LSEG Workspaceから取得するヒストリカル生データ(日付×銘柄の
  ミッド、TONAフィキシング)のブック。過去起算の
  UDFがFixingシートを参照するので、PricingSheetと一緒に開く(構成はarchitecture.md)
