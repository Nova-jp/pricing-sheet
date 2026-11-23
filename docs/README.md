# pricing_sheet ドキュメント目次

JPY OIS(TONA)スワップのプライシングシートと周辺のブック(Excel + Python/QuantLib + LSEG)の設計・運用情報。

| ファイル | 内容 |
|---|---|
| [architecture.md](architecture.md) | 全体構成、データフロー(朝バッチ・フィード)、共有モジュール(`engine/common/`)、テスト、`historical_data.xlsx`、雛形 |
| [udf.md](udf.md) | UDFの作り方(全ブック共通。評価日`TradeDate`の受け取り方・エラーの出し方・書き方)。UDFを作る・変えるときは必ず読む |
| [decisions.md](decisions.md) | 複数のブックにまたがる設計判断とその理由(Excelとの分担・カーブ・DB・データの供給元・フィード・公開範囲) |
| [pricing.md](pricing.md) | `PricingSheet.xlsm`(pricing・RiskGrid・HistChart・ASW・ASWPricer)の構成と判断 |
| [analysis.md](analysis.md) | `SwapAnalysis.xlsm`(PCA・ボラ・FwdMonitor・FutBasis・BOJ)の構成と判断 |
| [bond_option.md](bond_option.md) | `BondOption.xlsm`(現物オプション)の計算規則・構成と判断 |
| [position.md](position.md) | `PositionSheet.xlsm`(ポジション・リスク管理)の手法・構成と判断 |
| [live_screen.md](live_screen.md) | `LiveScreen.xlsm`(スワップとボラのリアルタイムの画面、自動計算)の構成と判断 |
| [roadmap.md](roadmap.md) | まだ済んでいない保留中の項目 |
| [setup_windows.md](setup_windows.md) | 会社PC(Windows)でのセットアップ・毎日の手順・トラブルシューティング |

`../OPERATIONS.md`(**gitignore対象、リポジトリ直下**)は環境固有の運用値。このリポジトリはGitHub上でPublicのため、
秘密情報・環境固有の値は絶対にコミットしない。

## 書き方

- UDFの作り方の決まりは`udf.md`、その理由と横断的な判断は`decisions.md`、ブックに固有のものはそのブックのドキュメントに書く。
- 今も有効なことだけを書く。判断を置き換えたら古い記述は消して書き換える(経緯は`git log -p`とPRにある)。
  済んだ保留項目は`roadmap.md`から消す。
- コードの細部(関数の中身・計測の推移)はdocstring・コメントに書き、docsには書かない。各ファイル冒頭のdocstringが
  役割の最新かつ正確な説明。
