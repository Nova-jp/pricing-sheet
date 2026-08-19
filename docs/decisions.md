# 主要な設計判断と理由

時系列順。「なぜそうしたか」を優先して書く(コードを読めば分かる「何を
したか」は書かない)。

## カーブ構築・計算ロジック

- **補間法はHagan-West Monotone Convex(瞬間フォワード)**。理由:
  市場スポットレートを精度良く逆算できることが必須要件だったため。
  当初は自前実装(段階的ブートストラップ後にgapテナーの再精緻化パスが
  必要だった)→ QuantLib(`ql.PiecewiseConvexMonotoneForward`)へ移行し、
  全pillar確定後の整合性も含めてソルバーが解くため精緻化パスが不要になった
  (265営業日で機械精度一致を確認済み)。
- **QuantLibへの全面移行**。理由: 計算ロジックの簡略化(ユーザー指示)。
  daycount/calendar/schedule/カーブ/スワッププライシングを全てQuantLib製に
  置き換え、自前実装のエッジケース対応コードを削除できた。
- **O/NのみOISRateHelperでなくDepositRateHelperを使用**。理由:
  1日物のOISRateHelperは特定の評価日(2025-12-30等)でQuantLib内部の
  スケジュール生成が"degenerate single date"エラーを起こす。O/Nは複利
  計算が実質不要な1日物のため、DepositRateHelperで代替しても等価。
- **ヒストリカルの標準コンベンションは`ql.MakeOIS`で直接構築**。理由:
  自前のForward生成スケジュールは月末を跨ぐケースでMakeOIS内部の
  スケジュールとごく僅か(0.1bp未満)にずれることがあった。カーブ自体が
  使っている標準コンベンションと完全一致させるため、標準コンベンション
  (`PA/act365f/PA/act365f/STD`)の場合だけMakeOISに切り替えている
  (非標準コンベンションは引き続き汎用の`price_swap`を使う)。
- **ローカルキャッシュは生レートのみ保存、ブートストラップ済みカーブは
  保存しない**。理由: 日中のリアルタイム計算とヒストリカル計算を
  完全に同じ`bootstrap_curve()`関数に通すことで、計算経路の食い違いを
  構造的に排除するため(ヒストリカルチャートの再構築コストはミリ秒
  オーダーで軽いので問題ない)。
- **過去起算スワップの実績フィキシングはローカルキャッシュのO/Nレートで
  代用**。理由: 本物のTONA実績フィキシング系列は持っていないが、DBに
  O/Nレートがあるため代用として十分と判断(ユーザー確認済み)。

## テナー表記

- **RealTimeシートは2Y/3Yを24M/36M表記、Neon側は2Y/3Y表記のまま**。理由:
  Excel上の見やすさ(月数表記に統一)を優先しつつ、既存DBのスキーマは
  変更したくなかったため。`curve.py`の`TENOR_ALIASES`で入力境界において
  正規化し、内部処理は常に2Y/3Y表記に統一している。

## Excel/VBA構成

- **最終的にVBAは一切使わない(UDF専用設計)**。理由: ユーザーが
  「VBAには必要最低限の役割しか持たせたくない」→ 最終的に「今は完全に
  ゼロでよい」という方針転換。手動計算モード+F9でxlwingsのUDFが
  ネイティブのExcel関数と同じ挙動で再計算されることを確認済みのため、
  トリガー用のマクロボタンが不要になった。
- **`PricingSheet.py`は`swap_pricing/udf.py`を再エクスポートするだけ**。
  理由: xlwingsのExcelアドイン(Windows)の「Import Functions」は、
  ワークブックと同名の`.py`ファイルからUDFを探す仕様。`_xlwings.conf`
  シートの「UDF Modules」設定だけでは不十分だった(実際にWindows実機で
  F9が効かず、旧`main()`ベースの`PricingSheet.py`にはUDF定義が
  存在しなかったことが根本原因と判明)。
- **カーブ(スプレッド)・フライ・合計はExcelの数式に任せ、UDF化しない**。
  理由: ユーザー方針「平易な数式で足りるものは平易な数式、高度な計算
  だけUDF化。勝手に複雑にせず相談する」。スピル参照演算子(`#`)を使うことで
  `HistoricalOutright`の出力サイズが変わっても追随する。
- **BucketedDeltaに`include_labels`引数を追加**。理由: リスクグリッドで
  複数行に同じUDFを配置すると、各行が2行(ラベル行+値行)スピルして
  隣の行のセルと衝突する。行ごとには値のみ(1行)を返す設計にし、
  ラベルは別セルで1回だけ取得する形にした。
- **リスクグリッドは`risk_flag=1`の行だけ計算**。理由: 全行で
  `BucketedDelta`(重いバンプ再計算)を評価すると無駄が大きいため、
  `IF(risk_flag=1, BucketedDelta(...), "")`の短絡評価で対象行だけ計算する。

## データ配布(Neon → 会社PC)

- **メール配信ではなくGCS公開オブジェクト方式を採用**。理由: 会社PCに
  Neonの接続情報を一切持たせたくない、かつメールより実装・運用が単純。
- **オブジェクトパスにUUIDを含める(公開だが推測困難)**。理由: バケットを
  丸ごと公開するのではなく、対象オブジェクト1つだけを読み取り専用で
  公開する。パスに長いランダム文字列を含めることで、直接URLを知る人
  以外はアクセスできないようにする。
- **バケットのuniform bucket-level accessを無効化**。理由: 有効なままだと
  `blob.make_public()`が使えない(legacy ACLが使えないエラー)。
  `allUsers`+条件付きIAMバインディングの組み合わせもGCSでは許可されて
  いないため、バケット単位でfine-grained ACLに切り替えた
  (オブジェクト単位の読み取り専用公開であることは確認済み)。
- **Cloud Run Job + Cloud Scheduler構成**。理由: 朝1回のバッチ処理に
  常時稼働のサーバーは不要。Secret Manager経由でNeon接続文字列を注入し、
  Cloud Run実行環境にすら平文の接続情報を持たせない。

## その他の技術的なハマりどころ(再発防止のため記録)

- **openpyxlの`insert_cols`/`delete_cols`はDataValidation範囲を自動追随
  しない**。列の増減を行うたびにドロップダウンがずれる不具合が2回発生。
  対策として、ヘッダー名から現在の列位置を都度検索し、DataValidationを
  作り直すルーチンで固定化した。
- **psycopg2の`channel_binding=require`パラメータはCloud Run上の
  libpqバージョンで接続エラーになる**。`sslmode=require`だけでもTLS
  暗号化は確保されるため、`neon_client.py`で当該パラメータを除去している。
- **Mac Excel + 旧xlwingsバージョンでAppleScript保存エラー**
  (`-50 Parameter error`)。`xlwings`を0.36系にアップグレードして解消。

## リポジトリの公開範囲について(要注意)

- **このGitHubリポジトリ(`Nova-jp/pricing-sheet`)はPublicである**。
  `cloud/cloudbuild.yaml`にGCPプロジェクトID(`turnkey-diode-472203-q6`)が
  そのままコミットされている。プロジェクトID単体は機密情報ではないが、
  GCSバケット名・オブジェクトパス(UUID)・Cloud Run/Schedulerジョブ名等の
  **実運用リソース情報は今後も一切コミットしない**方針とし、`OPERATIONS.md`
  (gitignore対象)に分離して記録している。
