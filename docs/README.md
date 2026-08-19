# pricing_sheet ドキュメント目次

JPY OIS(TONA)スワップのプライシングシート(Excel + Python/QuantLib + GCP)の
設計・運用情報。セッションをまたいでも参照できるよう、このフォルダに
集約している。

- [architecture.md](architecture.md) — 全体構成、データフロー、モジュール一覧、シート構成
- [decisions.md](decisions.md) — 主要な設計判断とその理由(なぜそうしたか)
- [roadmap.md](roadmap.md) — 保留中/次フェーズ以降に回した項目
- [setup_windows.md](setup_windows.md) — 会社PC(Windows)でのセットアップ手順
- `../OPERATIONS.md`(**gitignore対象、リポジトリ直下**) — GCPの実際のリソース名
  (プロジェクトID、バケット名、公開URL等)。このリポジトリは **GitHub上でPublic**
  のため、実リソース名は絶対にコミットしない。

## 関連ファイル(コード側)

- `swap_pricing/` — 計算エンジン本体(QuantLibベース)。各ファイル冒頭のdocstringに
  役割を明記しているので、詳細はコードを直接参照するのが最新かつ正確。
- `swap_pricing/udf.py` — Excelから直接呼べるUDF定義(`PricingSheet.py`が再エクスポート)
- `PricingSheet.xlsx` — 成果物のExcelブック本体(VBAなし、UDFのみ)
- `cloud/` — Cloud Run Job用のDockerfile・エントリポイント
- `src/`, `main.py`, `vba/BOJ_Analysis.bas`, `create_excel_sheet.py` — **本プロジェクトとは無関係の
  既存BOJ会合インプライド分析ツール。触らない。**
