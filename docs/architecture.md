# 全体構成

## 目的

任意のコンベンションのJPY OIS(TONA)スワップについて、
(a) リアルタイムのパーレート/PV/デルタ と (b) ヒストリカルなアウトライト推移を
1つのExcelブック(`PricingSheet.xlsm`)上で計算・表示する。

## データフロー

```
[Excel: historical_data.xlsx(作業用。リポジトリにはtemplates/の雛形のみ)]       [Neon(Postgres、他プロジェクトと共用)]
  LSEG Workspace(RDP.HistoricalPricing)から取得し、保存する                      public."ASW_data"(JGB銘柄別ASW、他プロジェクトのテーブルは触れない)
  - Midシート: 日付×テナーのミッド(JP{テナー}ONI=TRDT、リアルタイムのフィードと同じ系列)
  - Fixingシート: TONA実績(JPONMU=RR)。バッチがDBに取り込み、過去起算とO/Nのピラーに使う
        │                                                                              │
        ▼  朝に手動実行: python -m engine.common.morning_batch                          │ SELECTのみ、updated_atで差分取得
[朝バッチ engine/common/morning_batch.py]  設定: historical_curve.toml(引き方・使うテナー)   │
  ① Mid/Fixingを読み、DBのquotes/fixingsに取り込む(過去に取り込んだ日も残る)              │
  ② 日付ごとに選択テナー(O/Nはその日のTONA実績)でカーブを構築 → 暦日ごとのDFを保存        │
     (欠損テナーは外して構築、日本の非営業日は作らない、入力が変わった日だけ作り直す)      │
  ③ engine/common/neon_source.py経由でASWスプレッドを差分取得 ◀─────────────────────────┘
     (Neonに繋がらなくても①②は続行する)
  ④ engine/common/jgb_source.py経由でLSEG(Python API)からJGBの銘柄情報・引け値(JCL)を取得
     (現物オプション用。Workspaceに繋がらなくても①〜③は続行する。--skip-jgbで行わない)
        │
        ▼
[data/historical.db(ローカルのSQLite。LSEG/Neon由来なのでコミットしない)]
        │
        ▼  日中はDBを読むだけ(ブートストラップしない)
[Excel: PricingSheet.xlsm]
  各シートのライブのレート(LiveQuotes)+Mainの採否フラグ ─▶ FairRate等のUDF(その場でカーブ構築)
  HistoricalRate / HistoricalRates / HedgeHistoricalRates ─▶ DBのカーブで評価
```

リアルタイムのレートは、常駐のフィード(Pythonのプロセス。LSEGのストリーミングを購読し続ける)から
手動計算のブックへファイルで渡す(2026-10-19〜。それ以前は自動計算のExcelのLiveFeed.xlsm。理由は
[decisions.md](decisions.md)):

```
[LSEG Workspace(起動・ログイン中)] ─ localhostのAPI Proxy ─▶ [フィード: python -m engine.common.live_feed]
        │  live_feed.toml のRICを購読。値が変わったら最大 write_interval_sec(1秒)ごとに書く。
        │  ティックが無くても heartbeat_sec(2秒)ごとにデータセットごとの状態(ハートビート)を書く
        ▼
[data/live.db(ローカルのSQLite。LSEG由来なのでコミットしない)]
        │  そのシートの取得モード(B1)がLiveなら再計算(Shift+F9)のたびに読む。Fixedなら読まない。
        │  ハートビートが stale_after_sec(10秒)より古いか、LSEGとの接続が正常でなければエラー
        ▼
[Excel(手動計算): PricingSheet.xlsm / SwapAnalysis.xlsm / BondOption.xlsm]
  各フロントシートが自分で =IF($B$1="Fixed", LiveSnapshot("swap"), LiveQuotes("swap")) を持つ
        ─▶ そのシートのUDF(引数の形は変更なし)
```

手動計算のブックにRealTime系のシートは無い(2026-10-18に削除)。Shift+F9はそのシートだけを
再計算し、他のシートの値は計算し直さないので、ライブのレートは使うシートごとに読む。

- **取得モードの欄(各フロントシートの1行目、同じデザイン。セルの文字は英語)**: A1「Rates」、
  B1(薄黄色)=プルダウン`Live`/`Fixed`、C1「Updated」、D1=`=LiveInfo(データセット,ライブのセル#)`
  (そのシートが読んだレートの値がフィードで最後に変わった時刻)、E1=説明。
  - `Live`: `LiveQuotes`(volatile)がShift+F9のたびに最新を読み、読んだ値をそのシートの固定の値として
    `data/live.db`に保存する。フィードが止まっている・LSEGとの接続が正常でないときはエラーになる
    (古い値で黙って計算しない。そのまま計算したいときは`Fixed`にする)。
  - `Fixed`: `LiveSnapshot`(非volatile)がその固定の値を返す。IFで`LiveQuotes`が呼ばれないとセルの
    volatileが外れるので、Shift+F9で再計算されるのは入力を変えたセル(追加した行など)だけで、
    それらは切り替える前と同じレートで計算される。Ctrl+Alt+F9・UDFサーバの再起動でも変わらない。
- **ライブのレート**: `=IF($B$1="Fixed",LiveSnapshot("swap"),LiveQuotes("swap"))`(Ticker/値/起算日/満期日)。
  単独のセルに置き、加工は別のセルでする。列(下表のライブのレート〜カーブ用の範囲)は列のグループで
  折りたたんである(Main H:Q、pricing AF:AV(PricingTableの出力を含む)、HistChart AJ:AS、BOJ K:T、PCA CA:CD、VolPCA LA:LC、VolHV AA:BK(計算領域ごと))。
- **カーブ用の範囲**(PricingSheetのシート): `=LET(q,ライブ#,t,Main!$A$11:$A$54,HSTACK(Main!$B$11:$B$54,t,
  INDEX(q,XMATCH(t,INDEX(q,,1)),{2,3,4})))`=採否/Ticker/値/起算日/満期日(44行、Mainのテナーの並び)。
  UDFには`TAKE(範囲#,,3)`(採否/Ticker/値)を渡す。フィードに無いTickerは`#N/A`。

| シート | ライブのレート | カーブ用の範囲 |
|---|---|---|
| Main | H3 | M3 |
| pricing | AF4 | AK4 |
| HistChart | AJ3 | AO3 |
| BOJ | K3 | P3 |
| PCA(SwapAnalysis) | CA4(Ticker/値を`CHOOSECOLS`で渡す) | なし |
| VolPCA(SwapAnalysis) | LA3(`"vol"`、銘柄/RIC/値をそのまま渡す) | なし |
| VolHV(SwapAnalysis) | BB3(`"vol"`、`IV (Live)`の`XLOOKUP`だけが使う) | なし |

フィードの設定は`live_feed.toml`(リポジトリ直下。調整する値はここにまとめる): 秒数(`[timing]`)と
データセットごとのRIC。
- `"swap"`(kind=`ois`): O/N〜40Y・RD・M1〜M9。行=[Ticker, BID/ASKのミッド, 起算日`GV1_DATE`, 満期日`GV2_DATE`]
  (起算日・満期日は会合スワップだけ)。O/NはTradition `JP1DONI=TRDT`のミッド(=TONA)。
- `"vol"`(kind=`vol`): 満期×原資産の格子。行=[銘柄`1Mx2Y`, RIC`JPYTO{満期}X{原資産}ATM=R`, `MID_IV_NOR`=ノーマルvol bp]。
  `CF_LAST`/`PRIMACT_1`/`MID_IV`はシフト付きログノーマル%、`CF_BID`/`CF_ASK`は両者が混在しているので使わない。
- 銘柄の状態が異常(Suspect・存在しない等)か、BID・ASKのどちらかが無い行は値を空欄にする。
読む側はTicker(volはRIC)で引き当てるので、行の並びがブックごとに違ってもよい(フィードに無いTickerは`#N/A`)。
データセットごとの状態(`feed`テーブル): `ok`/`connecting`/`reconnecting`/`suspect`(全銘柄が異常)/`stopped`。
接続が切れたらまずライブラリの再接続を待ち、`resubscribe_after_sec`(30秒)たっても戻らなければセッションと
購読を開き直す。ログは`data/live_feed.log`。

データの供給元はLSEG Workspace(カーブ・プライシング本体)とNeon(ASWスプレッド、参照データのみ)。
Neonはクラウドだが、ASWスプレッド以外には使わない(クラウドの計算・保存基盤には戻さない)。
ASWはカーブのブートストラップには使わず、`asw_quotes`に保存するだけ(詳細は[decisions.md](decisions.md))。

## 計算エンジン(`engine/`)

プライシング計算(日付・スケジュール生成を含む)は自前実装を持たず、全て
QuantLib純正のAPI(`ql.MakeOIS`/`ql.OISRateHelper`/`ql.PeriodParser`/
`ql.Date.from_date`等)に委ねている。自前で持つのは「業務ルール(テナー表記・
roll_conv文字列等)→QuantLib引数」の対応表と、Excel境界の型変換のみ
(経緯は[decisions.md](decisions.md)参照)。

`engine/`配下は3つの並列パッケージ(2026-10-12、機能ごと+共通に分解。理由は[decisions.md](decisions.md)):

```
engine/
  common/         # pricing・historical・boj・pcaのどこからも使う共有エンジン・データ層
  swap_pricing/    # PricingSheet.xlsm固有のUDF(pricing/historical/bojの3機能)
  pca/             # SwapAnalysis.xlsm固有のPCA分析
  vol/             # SwapAnalysis.xlsm固有のIV vs HV(Fwdの日次変化)
  bond_option/     # BondOption.xlsm固有の現物オプション(先渡のデュレーション・Black式・ITMバイライト・ボラの補間)
```

現物オプション(`BondOption.xlsm`)の計算規則・ブックの構成は[bond_option.md](bond_option.md)。

### 共有エンジン・データ層(`engine/common/`)

| ファイル | 役割 |
|---|---|
| `conventions.py` | 業務上の表記→QuantLibの対応表を集約: `ql.Japan()`カレンダー、dcf文字列→DayCounter、freq、roll conv→生成ルール、IMMコード(M27)→日付、テナー表記の読み替え(12M→1Y)。自前の日付計算ロジックは持たず、呼び出し側がQuantLibのAPIを直接使う |
| `curve.py` | スポットレート辞書→`ql.PiecewiseConvexMonotoneForward`によるカーブ構築。O/Nは`DepositRateHelper`、それ以外は`OISRateHelper`(`rule=ql.DateGeneration.Forward`・`endOfMonth=False`で構築) |
| `terms.py` | 取引条件の型(3段): `SwapConvention`(規約: fix freq/fix dcf/float freq/roll conv。作るときに値を確認)⊂`SwapTerms`(規約+起算日+tenor/end=1本の具体的なスワップ。パーレートの計算単位)⊂`SwapParams`(取引条件+想定元本・PAY/REC・固定金利。PV・デルタの計算単位)。標準の規約`STANDARD`。Excelの行を型に直すのは各feature udfモジュール(`pricing/udf.py`等)の入口だけ |
| `market_data.py` | 評価日時点の市場データ一式の型`Market`(評価日・カーブ・TONA指標・TONA実績)。実績は作るときに「評価日より前」だけに切る(先読みの排除はここだけ)。QuantLib以外に依存しない |
| `swap_pricer.py` | `ql.MakeOIS`(→`ql.OvernightIndexedSwap`)によるPV・パーレート計算。取引条件(`SwapTerms`)と市場データ(`Market`)を受け取る。評価の前提(評価日・実績の登録・満期済み判定・実績の確認)は`_prepare_swap`に集約し、`price_swap`(パーレート・アニュイティ・PV)と`par_rate`(パーレートだけ、ヒストリカル用)が共通で通る。過去起算のスワップは、取引条件から必要な日を判断して`Market`の実績を登録(不足時はエラー)。評価日は呼び出しごとに設定し直す。`swap_dates`はカーブなしで同じビルダーのスケジュールから起算日・満期日(休日調整後)を返す(ヒストリカルのROLLINGで営業日数を数えるのに使う) |
| `market.py` | 評価日のマーケット(今日=RealTime、過去日=ヒストリカルDBのその日のレート。TONA実績はどちらもDB)と、全行・全UDFで共有するカーブ一式(基準カーブと全テナーを0.1bp動かしたカーブ。必要になったときに1回だけ作る。テナー別のバンプはカーブを作らず、基準カーブのクォートを一時的に動かして戻す。バンプはカーブに使っているテナーだけで、デルタは10倍して1bpあたりにする(risk.per_bp)。キャッシュは最新の1組だけで、ヒストリカル系UDFは入口で`release_curves`で手放す)。計算には`Market`を渡す |
| `risk.py` | 並行/バケットデルタ(`market`のカーブ一式を使う。バケットは`bucketed_deltas`で全ポジションをまとめて、テナーごとにクォートを1回だけ動かす)、notional逆算 |
| `historical_store.py` | ヒストリカル日次カーブDB(`data/historical.db`)の読み書き。カーブは評価日から暦日1日刻み・50年分のDFとして保存し、`ql.DiscountCurve`で復元する(引き方に依存しない形式。理由は[decisions.md](decisions.md))。生レート・TONA実績・ASWスプレッド(`asw_quotes`、JGB銘柄別、カーブには使わない参照データ)も保存。`engine/pca/`も`HistoricalStore`をここから読む |
| `morning_batch.py` | 日次カーブDBを作るバッチ(手動実行)。`historical_data.xlsx`を読み、`historical_curve.toml`の設定(引き方・使うテナー)でカーブを構築して保存。`neon_source.py`経由でASWスプレッドも差分取得する(Neonに繋がらなくてもカーブ構築は続行) |
| `neon_source.py` | Neon(Postgres)の`public."ASW_data"`から読み取り専用でASWスプレッドを取得する(他のテーブル・他プロジェクトのデータには触れない)。接続文字列は環境変数`NEON_CONNECTION_STRING`(`.env`)。`updated_at`を基準に差分取得できる |
| `live_feed.py` | リアルタイムのフィード(常駐プロセス、`python -m engine.common.live_feed`)。`live_feed.toml`を読み、LSEG(`lseg.data`のデスクトップセッション)でRICを購読し続け、ブックが読む形の行とデータセットごとの状態(ハートビート)を`live_store`に書く。切断時の開き直し、二重起動の防止 |
| `live_store.py` | リアルタイムのレートの受け渡しDB(`data/live.db`)。データセット("swap"・"vol")ごとに最新の行(JSON、日付は型を保って)と値が最後に変わった時刻、シートごとの固定の値、フィードの状態(`feed`)を持つ。値が変わらなければ書かない。ライブで読むときはフィードの状態を確かめる。WALで、書き込み中も別のプロセスから読める |
| `live_udf.py` | `live_store`のUDF(読むだけ)。`LiveQuotes`/`LiveSnapshot`/`LiveInfo`(PricingSheet・SwapAnalysis・BondOption共通。`LiveQuotes`はvolatileで最新の行を読み、シートごとの固定の値として保存する。フィードが止まっていればエラー。`LiveSnapshot`はその固定の値を返す。`LiveInfo`はその時刻) |
| `bond.py` | JGBの規約: 単利⇔単価(自前の式、小数3桁切り捨て)、うるう日除きの日数、経過利子・利払日(QuantLib)、受渡日。現物オプションが使う([bond_option.md](bond_option.md)) |
| `jgb_source.py` | LSEG(Python API `lseg.data`のデスクトップセッション)からJGB全銘柄の銘柄情報と引け値(`{RIC}JCL`)を取得する。朝バッチの④が使う |
| `paths.py` | リポジトリ内パス解決(クローン先の絶対パスに依存しない) |
| `udf_common.py` | pricing/historical/BOJ/PCAのどのUDFからも使う共有部分: Excel境界の型変換(`_to_date`等)、評価日の解決、評価日時点のカーブ一式(`_market`)。各feature udfモジュールは`udf_common.xxx(...)`の形(属性参照)で呼ぶ(テストでの差し替え方が1か所に集約できるため。理由は[decisions.md](decisions.md)) |

### PricingSheet.xlsm固有のUDF(`engine/swap_pricing/`)

| ファイル | 役割 |
|---|---|
| `udf.py` | **Excelから直接呼べるUDFの単一の窓口**(xlwingsの「ワークブックと同名の.pyファイル」制約のための薄い再エクスポート層。`Ping`以外の実体は`pricing/udf.py`・`historical/udf.py`・`boj/udf.py`・`asw/udf.py`) |
| `pricing/udf.py` | Main/pricingシートのUDF: `PricingTable`(pricing表全体)/`FairRate`/`SwapPV`/`SwapDelta`/`SwapAnnuityDelta`/`NotionalForDelta`(1行用)/`RiskGrid`/`MarketInfo`。どれも下の部品を組み合わせるだけ |
| `pricing/row.py` | 1行分のセルの値→取引条件の型(`swap_terms`=SwapTerms、`swap_params`=SwapParams)。start列のテナー・営業日数の読み替えもここ |
| `pricing/measures.py` | 列ごとの計算: `par`(想定元本1・固定金利なしの評価)/`fair_rate`/`pv`/`parallel_delta_mm`/`annuity_delta_mm`/`notional_for_delta`。`par`の結果・PVを渡すと評価をやり直さない(PricingTableは1行3回の評価) |
| `pricing/table.py` | PricingTableの本体: 見出し名で列を探し、flag=1の行だけ計算し、行ごとのエラーをmessageに閉じ込める |
| `historical/pricer.py` | 保存済みの日次カーブから、任意コンベンションのヒストリカル・パーレート系列を計算(ブートストラップしない=カーブの引き方を知らない)。モードはFIXED(同じ日付の取引をそのまま過去日で評価)とROLLING(基準日の取引の「スポット日→起算日」「起算日→満期日」の営業日数を保って各過去日へ平行移動。IMMコードも動かす。roll conv=IMMはエラー。暦日数=dcfの期間は一定にならない)。入力は種類に関係なく基準日の取引の2つの日付として読み、空欄のstartは基準日のスポット日。過去日Dの計算にはD当日以降のTONA実績を使わない。満期までの範囲だけカーブを読み出す |
| `historical/udf.py` | HistChartシートのUDF: `HistoricalRate`/`HistoricalRates`/`HedgeHistoricalRates` |
| `historical/hedge.py` | ヘッジの重ね表示用に、スポットのテナーごとのパーレートを指定した日付の並びで返す(`historical/pricer.py`と同じ経路、標準の規約のスポットのスワップ)。その日のカーブに使っていないテナー(クォートが無かった日)は補間値を出さず空欄。O/N・会合スワップは受け付けない |
| `boj/meeting_curve.py` | BOJシート専用: O/N+会合スワップ(RD・M1〜)から、会合期間ごとにフォワード一定のカーブを組む。日付はLSEGの値をそのまま使い、決定日→翌営業日の夜は直前の期間(決定前)のフォワード。節目のDFを順に決めて`ql.DiscountCurve`に入れる(OISRateHelperを使わない理由は[decisions.md](decisions.md))。最後の会合期間より先は外挿しない |
| `boj/udf.py` | BOJシートのUDF: `BojImpliedRates` |
| `asw/udf.py` | ASWシートのUDF: `AswHistoricalRate`/`AswHistoricalRates`。カーブを組まず`historical_store.asw_quotes_for`を読むだけ(ROLLING/FIXEDの概念はない。銘柄コード(bond_code)ごとに日付→値が決まっているだけのため) |

分析ブック(`engine/pca/`・`engine/vol/`)は後述の「分析ブック」節を参照。

`tests/test_past_start.py`: 過去起算スワップ(合成フィキシング)と評価日の扱いの
リグレッションテスト(`python -m unittest tests.test_past_start -v`)。

`tests/test_historical.py`: ヒストリカル計算(FIXED/ROLLINGの日付の決め方、先読みしない
フィキシングの扱い、満期後の除外)のリグレッションテスト(`python -m unittest tests.test_historical -v`)。

`tests/test_historical_store.py` / `tests/test_morning_batch.py`: 日次カーブDBの保存・復元の精度と、
バッチ(テナー選択・欠損テナー・O/N・祝日除外・差分更新・ASW取得の失敗時の継続)のリグレッションテスト。
LSEGデータの代わりに合成したブックを使う。ASWはNeonへの接続をモックして確認する(実際には繋がない)。

`tests/test_neon_source.py`: ASWスプレッド取得の対象テーブル・差分取得の条件(`updated_at > since`)・
接続の後始末(成功・失敗どちらでもclose)のリグレッションテスト。psycopg2をモックし、実際のNeonには繋がない。

`tests/test_asw.py`: ASWシートUDF(`AswHistoricalRate`/`AswHistoricalRates`)のリグレッションテスト。
PA/SAの切り替え、日付範囲の絞り込み、複数銘柄の日付揃え(欠損銘柄はその脚だけ空欄)、脚数の上限。

`tests/test_meeting_curve.py`: 会合スワップのカーブの再現精度(各会合スワップ・O/Nが機械精度で戻る)、
決定日の夜が決定前のフォワードになること、休日の評価日、日付の重なり・範囲外のエラー、`BojImpliedRates`の範囲の読み方。

`tests/test_imm.py`: IMMコード(`M27`等)の解釈とIMMロールのスケジュール日付の
リグレッションテスト(`python -m unittest tests.test_imm -v`)。

価格計算の値そのもの(PV・パー等)はQuantLibを正とし、手計算の再実装による照合テストは
持たない。テストはQuantLibに渡す前後の自前ロジック(入力解釈・スケジュール・エラー処理・
ブートストラップの再現精度)を対象にする。テストはQuantLibの入ったx64 Python環境で
`python -m unittest discover -s tests` でまとめて実行できる。

`tests/test_curve_repricing.py`: カーブ・ブートストラップとスワップ評価の
リグレッションテスト(`python -m unittest tests.test_curve_repricing -v`)。
実データ相当のスポットレートで全pillarが機械精度で再評価できることを確認する
(このプロジェクトのハード要件である「市場レートの正確な逆算」の回帰防止用)。
LSEGのデータには依存しない。

## UDF専用設計(VBAなし)

- `PricingSheet.py`(ワークブックと同名)は`from engine.swap_pricing.udf import *`のみ。
  xlwingsのExcelアドイン(Windows)は「Import Functions」実行時に
  **ワークブックと同名の.pyファイル**からUDFを探すため、この1段の間接参照が必要。
  `engine/swap_pricing/udf.py`自体は薄い再エクスポート層で、実体はシート(機能)ごとに
  `pricing_udf.py`(Main/pricing)・`historical/udf.py`(HistChart)・
  `boj/udf.py`(BOJ)に分かれている。共有部分(Excel境界の型変換・評価日解決・`_market`)は
  `udf_common.py`に集約している(2026-10-12、機能ごとのディレクトリに分割。理由は[decisions.md](decisions.md))。
- ブックの計算方法は「手動」。UDFは非volatileのままでよく、F9(再計算)を
  押すたびに最新のセル値を使って再評価される。VBAマクロ・ボタンは一切不要。
- UDFにするのは**QuantLibが必要な計算だけ**(パーレート、PV、デルタ、
  notional逆算、リスクの合算(RiskGrid)、ヒストリカル)。カーブ(スプレッド)や
  フライ、合計といった四則演算はExcel自身の数式(`B1#`等のスピル参照)
  に任せる。「エクセル上は平易な数式、高度な計算だけUDF化」という方針。

## Excelブック構成(`PricingSheet.xlsm`)

シート: `Main` / `pricing` / `HistChart` / `ASW` / `BOJ` / `_xlwings.conf`(RealTimeシートは2026-10-18に削除。
ASW以外のフロントシートの1行目は取得モードの欄。上の「データフロー」参照)

### カーブのテナー(Mainシート A10:B54)

| 列 | 内容 |
|---|---|
| A(A11:A54) | Ticker(テナーラベル。`live_feed.toml`の`"swap"`のTickerと同じ表記で引き当てる)。O/N〜40Y、RD、M1〜M9 |
| B(B11:B54) | **スポット採否フラグ**。`1`または空欄=カーブ構築に使う、`0`=使わない。会合スワップの行(RD・M1〜M9)は`0` |

各シートのカーブ用の範囲がこの並びで値・起算日・満期日を引き当てる(上の「データフロー」)。
テナー表記は全て大文字。Reutersフィードの都合で **1Y/2Y/3Yは`12M`/`24M`/`36M`表記**
(`1Y`/`2Y`/`3Y`表記とは`conventions.py`の`TENOR_ALIASES`で吸収)。
O/Nは`JP1DONI=TRDT`(Traditionの1日物OIS、チェーン`JPONI=TRDT`の先頭)のミッドで、
メインのカーブにも使う(過去の評価日・ヒストリカルのO/Nはその日のTONA実績)。
LSEGのRICは`live_feed.toml`にある。
RD・M1〜M9は会合スワップ(TraditionのRD・M1〜M9、チェーン`JPONIBOJ=TRDT`)。RD=スポット日→会合1の
決定日、M{n}=会合nの決定日の翌営業日→会合n+1の決定日。メインのカーブには使わず
(`curve.EXCLUDED_MEETING_TENORS`で`skipped_tenors`計上)、BOJシートだけが使う。

UDFに渡す範囲は、そのシートのカーブ用の範囲の`TAKE(範囲#,,3)`(フラグ/Ticker/Value。旧`RealTime!$B$2:$D$45`と同じ形)。
過去起算のスワップのTONA実績は、ヒストリカルDB(朝のバッチが取り込んだもの)から読む。UDFの引数では
受け取らず、`historical_data.xlsx`を開いておく必要もない(以前の`fixing_range`引数と外部リンクは
2026-10-12に廃止)。DBが無ければスポット起点の計算だけが動き、過去起算は「実績が不足」のエラーになる。
「どのスポットレートをカーブに使うか」の判定は`engine/swap_pricing/udf.py`の
`_rates_from_range`(過去の評価日では`_market`が同じフラグ)で行い、フラグ0のテナーは
カーブにもデルタのバンプにも入らない(RiskGridでは空欄になる)。
15M/18M/21Mも普通のスポットとして扱う(旧`include_odd_tenors`は廃止、
不要ならB列を0にする)。

### pricingシート

列A-V: `flag / risk_flag / remarks / notional / delta(target) / adj_notional /
start / tenor / end / fix rate / fix freq / fix dcf / index / float freq /
roll conv / target fixrate / spred / fly / pay/rec / PV /
delta(bump) / delta(annuity)`。float dcfの列は持たない(TONAの変動脚の日数計算はQuantLibが
指標に固定し、入力しても使われないため。2026-10-12に削除)。W列はmessage(その行を計算できなかった理由)。
spred・flyはExcel数式(1行上/下のtarget fixrateの単純な四則演算)。
1行目は取得モードの欄、2行目はシート見出し(`Pricing`)、3行目が表の見出し、取引は4〜103行目の100行
(2026-10-19に見出し行を挿入。HistChartの行番号もExcelの行番号)。意匠は他のフロントシートと同じ
(シート見出しは太字13pt、表の見出しは太字、入力セル(A〜E・G〜O・S列)は薄黄色)。

target fixrate(P)・adj_notional(F)・PV(T)・delta(bump)(U)・delta(annuity)(V)・message(W)は、
右の計算用の領域のAQ3`=PricingTable(HSTACK($A$3:$E$103,$G$3:$O$103,$S$3:$S$103),TAKE($AK$4#,,3),評価日)`
(AQ:AV、見出し行+100行)が表全体を1回で計算し、左の列は`=AQ4`のようにその行のセルを参照するだけ
(2026-10-19。理由は[decisions.md](decisions.md))。計算するのはflag=1の行だけで、PV・delta系は
notionalのある行、adj_notionalはdelta(目標delta)のある行だけ。1行の入力の誤りでは表全体を
エラーにせず、その行の値を空欄にしてmessageに理由を出す。PricingTableには出力の列(F・P等)を
渡さない(循環参照になる)ので、入力の列だけを`HSTACK`で渡す(列は見出し名で探す)。
計算用の領域(ライブのレート〜PricingTable、AF:AV)は列のグループで折りたたんである。

start/end列には日付のほか、IMMコード(月記号H/M/U/Z+2桁年、例: `M27`)も入力できる
(`engine/common/conventions.py`の`imm_code_to_date`で第3水曜に変換)。start列にはテナー
(例: `10y`)も入力でき、評価日のスポット日+tenor(休日調整前)を起算日にする
(フォワードスタート。`10y`+tenor`20y`で10y20y。`conventions.forward_start_date`)。営業日数
(例: `2bd`・`10bd`)も入力でき、評価日からN営業日後(ql.Japan)を起算日にする(`2bd`=スポット日。
`conventions.business_days_start`)。HistChartでは基準日からN営業日後の日付に直し、他の日付と同じく
FIXED/ROLLINGで扱う。roll conv列(P列)は
既定で数式になっており、startとendが両方IMMコードなら`IMM`、それ以外は`STD`を返す。
STD等で計算したい行はドロップダウンで上書きする(UDFはroll convの値にそのまま従う)。
上書きを元に戻すときは、上下の行からP列の数式をコピーする。

評価日の引数はすべて名前`評価日`(Mainシート)を参照する(`TODAY()`は使わない)。

列Y以降: リスクの合算。Y3の`=RiskGrid(pricing!$A$3:$S$103, TAKE($AK$4#,,3), 評価日)`が、
risk_flag=1の全行のテナー別デルタ(1bpあたり、百万円。0.1bpのバンプを10倍)の合計を
Mainのテナーの並びでTicker/Deltaの2列にスピルする(カーブに使っていないテナーは空欄)。
AC3が全テナーの合計、AC4が参考として行ごとのdelta(U列、全テナーを動かしたときの1bpあたり)のrisk_flag=1の合計。
pricing表の列は見出し名で探すので、列を移動しても壊れない(見出し行=3行目から渡すこと)。
数式の先頭に`@`を付けないこと(付くと先頭の1セルしか表示されない)。

### Mainシート

- B4: 評価日(名前`評価日`、空欄=今日)。全UDFの評価日はこのセルを参照する。
  今日ならライブのレート、過去日ならヒストリカルDBのその日のレート
  (テナーはMainのB列の採否フラグで選ぶ。O/Nはその日のTONA実績)でカーブを組む。
  過去起算のTONA実績は評価日より前の日だけを使う。HistChartの基準日もこのセルに連動する。
- B5: `=MarketInfo(TAKE($M$3#,,3), 評価日)`(カーブの元データとテナー数)。A10:B54がカーブのテナー(上記)。
- `TODAY()`をやめたので、F9だけでは値は変わらない(レート・入力・評価日が変わったセルだけ
  再計算される)。取得モードが`Live`なら、再計算のたびにレートを読み直してそれに依存するセルが再計算される。

### ヒストリカルのUDF

データは日次カーブDB(`python -m engine.common.morning_batch`で作成)。DBが無いとエラーになる。
(旧Historicalシート(2Y/5Y/10Y固定)と`HistoricalOutright`、空のCalcシートは2026-10-12に削除した。
グラフはHistChartシートで見る。)

任意のコンベンションは`HistoricalRate(start, tenor, end, fix_freq, fix_dcf, float_freq,
roll_conv, mode, [基準日], [from], [to])`(Date/Rate/Start/Endの4列スピル)。modeは`FIXED`/`ROLLING`。
基準日には通常`評価日`(Mainシート、空欄=今日)を渡す(`TODAY()`はF9のたびに再計算されるため使わない)。
`HedgeHistoricalRates(テナーの列, モード, 日付の列, 基準日, real_time_range)`(HistChartのヘッジ用)は、日付の列の並びどおりにテナーごとのスポットのパーレート(%)を返す(見出し行なし。カーブの無い日・その日のカーブに使っていないテナーは空欄)。
`HistoricalRates`(HistChart用、最大3脚)は省略可能な最後の引数`real_time_range`にカーブ用の範囲を渡すと、
基準日が今日で営業日なら今日をライブのレートで計算した最新日として加える(HistChart参照)。
ROLLINGは営業日数を固定するので、満期日は「スポット日+tenor」から数日ずれる日があり、
暦日の長さ(act/365fixed等のdcfで測る期間)も日ごとに変わる(理由と影響は[decisions.md](decisions.md))。
roll convがIMMの脚をROLLINGで計算するとエラーになる(FIXEDで見るか、STDにする)。

### HistChartシート

pricingシートの行を指定して、最大3系列(アウトライト/カーブ/フライ)のヒストリカルを
1枚のグラフで比較するシート。

- 共通設定(1行目は取得モードの欄): B4=ROLLINGの基準日(`=IF(評価日="","",評価日)`でMainシートの評価日に連動、
  空欄=今日)、B5/B6=計算期間(任意、薄黄色)。B7=単位(表示中の系列にカーブ/フライが1本でもあれば
  全系列bp、アウトライトだけなら%。軸は1本。VBAなしでは系列ごとの第2軸を動的に切り替え
  られないため)、B8=倍率(bpなら100)。
- 系列表(10〜12行): 表示(○/×)・行A・行B・行C(pricingのExcel行番号。取引は4行目から)・モード
  (ROLLING/FIXED、系列ごと)・系列名(任意)。種類は行番号の数で自動判定
  (1つ=アウトライト、2つ=カーブ `行B−行A`、3つ=フライ `2×行B−行A−行C`。pricingシートの
  spred/flyと同じ符号)。凡例名は自動(系列名を入れればそれを使う)。
- 脚の確認表(16〜24行): 3系列×3脚のremarks/start/…/roll convを
  `LET(v,INDEX(pricing!列,行),IF(v="","",v))`で表示(空セルをINDEXで参照すると0になるため
  空文字に置き換える)。表示×の系列は脚を空にし、UDFは計算しない。
- 計算領域(29行〜): 系列ごとに
  `=LET(r,HistoricalRates(脚3行×8列, モード, B4, B5, B6, TAKE($AO$3#,,3)),d,DROP(r,1),
  VSTACK(TAKE(r,1),SORTBY(d,INDEX(d,,1),-1)))`(A/F/K列、Date+脚A/B/Cのスピル)。
  **新しい日付(基準日に近い方)が上**になるよう、Excelの`SORTBY`で降順に並べ替えている
  (UDFは昇順で返す)。P列はグラフ用の日付軸(脚なしで呼ぶと日付だけが返る。同じ範囲を渡して
  `SORT`で降順)。Q〜S列が系列値(種類に応じた四則演算×倍率。計算できない日は`#N/A`で
  線を出さない)。3系列とも同じ日付の並びなので、XLOOKUPによる日付の突き合わせは不要。
- 最新日: 基準日が今日(B4が空欄、または評価日=今日)で今日が営業日なら、今日の行を
  ライブのレートで計算する(pricingシートの`FairRate`と同じカーブ。DBに今日の引け値が
  あってもライブを優先)。それ以外の日はDBの引け値。ライブのレートを参照するので、取得モード(B1)が
  `Live`だと再計算のたびにHistChartの全系列が再計算される(1系列約1.2秒)。`Fixed`にしておけば再計算されない。
- グラフ`HistChart_Chart`(折れ線・日付軸・3系列固定)の系列は名前`HistChart_Date`/
  `HistChart_S1〜S3`(日付軸のスピル行数に合わせた参照)で、行数が変わっても追随する。
  未使用の系列は凡例に空の項目が残る(VBAなしでは消せない)。
- **ヘッジ(4本目の系列、実線)**: 系列1(フロー)に、手入力したヘッジのグリッドの仮想金利
  `V(t) = Σ d_i × r_i(t)` を重ねる(計算の仕組みと理由は[decisions.md](decisions.md)の「ヘッジのヒストリカル」)。
  - 設定(AD4〜AE8): 表示(○/×)、モード(既定は系列1のモードに連動する数式、ドロップダウンで上書き可)、
    縮尺(百万円/bp。既定は系列1の行の取引条件(脚の確認表16〜17行)とpricingのnotional・pay/recから
    `SwapAnnuityDelta`で計算: アウトライト=行A、カーブ=行B、フライ=行Bの1/2。pay/rec空欄はPAYとみなす。
    上書き可)、スタート日(系列1とヘッジの両方に値がある最も古い日、自動)、状態(AE8: 縮尺が計算できない・
    0のときにメッセージ。notionalが空欄の行は縮尺を手入力する)。
  - 縦のグリッド(AD9〜AG43): Ticker(`=TAKE(DROP($AO$3#,1,1),33,1)`=1W〜40Yのスポット。O/N・会合スワップは
    使わない)、フロー(pricingのRiskGrid `pricing!$Y$3#`をXLOOKUP)、ヘッジ入力(AF10:AF42、薄黄色、
    百万円/bp。**フローと同じ符号=ヘッジで再現するリスク**。実際に執行するヘッジ取引は逆の符号)、
    残差(フロー−入力)、合計(43行)。
  - U30: 0以外を入力したテナーだけを`FILTER`して`HedgeHistoricalRates(テナー, モード, HistChart_Date,
    B4, TAKE($AO$3#,,3))`(日付×テナーのパーレート%、日付軸と同じ並び)を呼び、`BYROW`で
    Σ d_i × r_i(百万円×%)を計算する。入力したテナーのどれかに値が無い日は`#N/A`(線を出さない)。
  - T30(グラフ用、名前`HistChart_H`): `系列1(スタート日) + (V(t) − V(スタート日)) × B8 ÷ 縮尺`。
    delta(annuity)は符号付き(PAY=+、REC=−)なので、PAY/RECに関係なくフローのレートと同じ向きに動く。
    T29が凡例名(`ヘッジ(モード)`、入力が無ければ空)。

### BOJシート

BOJ会合スワップ(RD・M1〜M9)と1年以下のスポット(1W〜12M)のずれを、2方向で表示する(リアルタイムのみ。
ヒストリカルは未対応)。カーブ用の範囲(P3#)の会合スワップの行(35〜44行目)を参照する。

- 上部(1行目は取得モードの欄): B3=評価日(空欄なら「今日(ライブ)」)、B4=O/N(`INDEX($P$3#,1,3)`)。
- **A(8〜17行)スポット→会合期間**: 各会合スワップの起算日・満期日(カーブ用の範囲の4・5列目)で、pricingと同じ
  メインのカーブの`FairRate(起算日,"",満期日,"PA","act/365fixed","PA","act/365fixed","STD",…)`を計算。
  G=ずれ(bp)=会合スワップ−スポットからのフォワード、H=会合スワップの前の期間からの変化(bp、RDはO/Nとの差)。
- **B(22〜34行)会合スワップ→スポット**: C22の`=BojImpliedRates(A22:A34, TAKE($P$3#,,3),
  TAKE(DROP($P$3#,34,1),10,4), 評価日)`が、O/N+RD+M1〜M9の階段状のカーブ(`boj/meeting_curve.py`)での
  スポット起算・標準コンベンションのパーレートを1列でスピル。D=ずれ(bp)=スポット−会合スワップのカーブ。
  評価日が今日でなければエラー。値か日付が欠けた会合スワップの行以降は使わない。
  最後の会合期間より先のテナーはその行だけエラーの文字列。
- ずれはミッド同士(売値・買値の幅は考慮しない)。差・変化はExcelの数式。

### ASWシート

JGB銘柄別のASWスプレッド(`data/historical.db`の`asw_quotes`、朝バッチがNeonから取得)の
ヒストリカルを、最大3系列(アウトライト/カーブ/フライ)で1枚のグラフに比較するシート。
HistChartと同じ「脚A/B/C→1本=アウトライト、2本=カーブ、3本=フライ」の構成だが、
pricingの行番号の代わりに銘柄を指定する。ROLLING/FIXEDの区別やライブのレートの参照はない(取得モードの欄も無い)
(銘柄ごとに日付→値が決まっているだけで、ASWはNeonの朝バッチ取り込みのみ。当日分はまだ
入っていないことがある)。

- 共通設定: B3=コンベンション(PA/SA、全系列共通)、B4/B5=計算期間(任意)、B6=倍率
  (ASWは%単位、bp表示のため100)。
- 系列表(9〜11行): 表示(○/×)・銘柄A/B/C・系列名(任意)・種類(自動)・凡例名(自動)。
  銘柄は「接頭辞+回号」(`JB380`)か9桁の銘柄コード(`003800067`)で入力する。
- 銘柄コード(15〜17行): 入力をUDFに渡す9桁のbond_codeに読み替える(Excelの数式)。
  bond_codeは「回号5桁+種別4桁」で、接頭辞→種別の対応表(H15:J24、JN=2年0042・JS=5年0045・
  JB=10年0067・JL=20年0069・JX=30年0068・JU=40年0054。行を足せる)を引く。知らない接頭辞は
  末尾が「?」になり、その脚は空欄になる。表示×の系列は空にしてUDFを計算しない。
- 計算領域(28行〜): A/F/K列が系列ごとの
  `=LET(r,AswHistoricalRates(TRANSPOSE(C15:E15),$B$3,$B$4,$B$5),…SORTBY(…,-1))`
  (Date/A/B/Cの4列スピル、新しい日付が上)。日付は系列内の銘柄の和集合で、値の無い脚は空欄。
  HistChartと違い系列ごとに日付の並びが違う(銘柄ごとに発行日・欠損が違う)ため、P列で
  3系列の日付を合わせ(`SORT(UNIQUE(VSTACK(…)),,-1)`)、Q〜S列で`XLOOKUP`により各系列の値を
  突き合わせる(種類に応じた四則演算×倍率。値の無い日は`#N/A`で線を出さない)。
  1銘柄だけの確認には`AswHistoricalRate(銘柄, コンベンション, from, to)`(Date/Rateの2列)も使える。
- グラフ`ASW_Chart`(折れ線・日付軸・3系列固定)の系列は名前`ASW_Date`/`ASW_S1〜S3`
  (P28のスピル行数に合わせた参照)で、行数が変わっても追随する。

## 分析ブック(`SwapAnalysis.xlsm`、`engine/pca/`・`engine/vol/`)

pricingとは切り分けた分析用のブック。スワップとボラ(スワップションATMノーマルvol)の
PCA(主成分で復元した値と実績の差分、主成分ベクトル)と、ボラのインプライドとヒストリカルの比較
(`VolHV`シート、下の節)。設計方針はPricingSheetと同じ(VBAなし、手動計算+F9、UDFは配列を返して
スピルに任せる)。Excelの関数では難しい計算(固有値分解・DBの読み出し・Fwdの計算)はPythonのUDF、
得点・復元(行列の積)・差分・寄与率・窓の集計などはExcelの数式。`SwapAnalysis.py`は
`engine/pca/udf.py`・`engine/vol/udf.py`の再エクスポートだけ(xlwingsのブック名の制約)。

| ファイル | 役割 |
|---|---|
| `engine/pca/history.py` | DBを日付×銘柄の表で読む。swap=`quotes`(生ミッド、M1〜M8を含む全テナー)+`fixings`(O/N=その日のTONA実績)、vol=`vol_quotes`(満期x原資産)。日本の営業日のみ。DBの更新時刻が変わるまでプロセス内にキャッシュ |
| `engine/pca/pca.py` | windowの選び方(`estimate`)、比較日の実績(`observe`)、固有値分解。`principal_components`は「サンプル行列→固有値・固有ベクトル」だけの汎用関数 |
| `engine/pca/udf.py` | `PcaModel`/`PcaEigen`/`PcaWindow`(推定)、`PcaActual`/`PcaActualInfo`(比較日の実績)。最後の引数`dataset`で"swap"/"vol"を選ぶ。推定は引数とDBの更新時刻ごとに1回だけ計算して共有 |

ボラのヒストリカルは、朝のバッチ(`engine/common/morning_batch.py`)が`historical_data.xlsx`の
Volシートを読んでDBの`vol_quotes`に取り込む(カーブには使わない)。

PCAの手順(標準的なPCA。理由は[decisions.md](decisions.md)):
- 推定: 推定基準日(windowの最終日。空欄=比較日より前の直近)からさかのぼって、選択した銘柄が
  そろった営業日のサンプルをN個(テナーが欠けた日は除外して、さらに過去へ)。モードは
  レベル(各日の水準)/日次変化(隣り合う営業日の差。間が欠けたペアは使わない)。
  平均μと、標準化ありなら標準偏差σ(なしはσ=1)を求め、z=(サンプル−μ)/σの共分散行列を
  固有値分解する(標準化なし=分散共分散(bp)、あり=相関行列)。主成分の符号は、銘柄の並び上の
  ルジャンドル多項式P_jとの内積が正になる向き(PC1=全体が正、PC2=後ろ側が正、PC3=両端が正)。
- 比較: 比較日(空欄=今日でそのシートのライブの値、日付=DBのその日の引け値)の実績x
  (日次変化モードは「比較日 − DBの前営業日」。前営業日がなければ直近の日との差で警告)について
  z=(x−μ)/σ、得点=V_kᵀz、復元=μ+σ·V_k·得点、差分=x−復元。推定基準日と独立に選べる。

シートは1機能1枚(スワップ: `PCA`、ボラ: `VolPCA`)。設定・結果・計算を同じシートに置き、
設定を変えてF9を押せば同じ画面で結果が更新される(理由は[decisions.md](decisions.md))。
どちらも左から「設定」「結果」「計算領域(右端、列のグループで畳んである)」の順:
- **設定(A〜D列)**: 1行目は取得モードの欄。「主成分ベクトルの推定」(B4推定基準日・B5 window・B6モード・B7標準化)と
  「比較」(B10比較日・B11主成分数k)。各項目の説明はセルの入力時メッセージ(選ぶと表示される)。
  名前はスワップが`PCA_推定基準日`等、ボラが`VOL_推定基準日`等。その下が銘柄の選択:
  スワップはテナー表(A15:B56。O/N〜40Y、M1〜M8)の○×で、既定は1Y〜40Yの13本
  (O/Nは×。ライブのO/Nは使えるが既定では外す)。ボラは満期(A:B)・原資産(C:D)の○×で、
  既定は全16×12=192点。
- **結果**: 上段に推定windowの情報(`PcaWindow`)と比較日の実績の情報(`PcaActualInfo`。実績の元・
  変化の基準日・警告)をUDFの出力のまま置く。スワップ(`PCA`)はその下にグラフ3枚(実績・平均・
  復元/差分/主成分ベクトル。系列は名前`PCA_Tenor`等のスピル参照)と、34行目からテナー別の表
  (実績・平均・復元・差分(bp)・平均からの乖離(bp))、固有値・寄与率・累積寄与率・比較日の
  スコア、主成分ベクトル(PC1〜PC5)。ボラ(`VolPCA`)は満期×原資産のマトリックス(差分・実績・
  復元・平均・PC1〜PC3。差分とPCは0を白にしたカラースケール)を`XLOOKUP`で並べ、上位10本の
  主成分の表を置く。
- **計算領域**(スワップはZ列〜、ボラはBF列〜。直接編集しない): `PcaEigen`、`PcaModel`
  [Label/Mean/Scale/PC1…]、選択銘柄の並び(スワップは`FILTER`=`PCA_テナー`、ボラは選択満期・
  選択原資産の`FILTER`と格子の全点の`TOCOL`=`VOL_銘柄`(満期ごとに原資産の順))。ボラは
  銘柄ごとの実績・平均・スケール・復元・差分もここで計算する。
- **ライブの値**: PCAはCA4の`=IF($B$1="Fixed",LiveSnapshot("swap"),LiveQuotes("swap"))`(Ticker/値…。
  `CHOOSECOLS(…,1,2)`を`PcaActual`に渡す)、VolPCAはLA3の同じ形の`"vol"`(銘柄/RIC/値をそのまま渡す)。RealTime/RealTimeVolシートは
  2026-10-18に削除(RICは`live_feed.toml`。上の「データフロー」参照)。

### IV vs HV(`VolHV`シート、`engine/vol/`)

スワップションのIV(DBの`vol_quotes`)と、同じFwdの日次変化から計算したHVを比べる(理由は
[decisions.md](decisions.md)の「IV vs HV」)。カーブ・フライはボラの差(重み付きの和)。

| ファイル | 役割 |
|---|---|
| `engine/vol/hv.py` | 日次カーブDBから、銘柄(満期x原資産)ごとのFwd(起算日=その日のスポット日+満期)と、その同じ契約の前の日付のカーブからの変化(bp)を計算する。スワップは1回だけ組み、カーブを差し替えて`fairRate`を読む(値は`swap_pricer.par_rate`と一致)。IVは`vol_quotes`から日付で突き合わせる |
| `engine/vol/udf.py` | `VolHistory(labels)`: [Date, 銘柄ごとに IV/Fwd/dF] の表(最初のカーブの日付は変化が無いので除く。IVが無い日は空文字)。引数とDBの更新時刻ごとに1回だけ計算する(3銘柄・約460日で約1秒) |

シートの構成(左から設定・結果・計算領域):
- **設定(A〜D列)**: 1行目は取得モード(他のフロントシートと同じ)。足(B5:B7=満期x原資産、C5:C7=重み。
  空欄の足は使わない。D列=足ごとの窓)、B9 `Window`(`Match`=満期に合わせる/営業日数)、B10 `Max window`
  (Matchの上限、既定126)、B11 `Short window`(既定20)、B12 `Days per year`(既定252)、B13 `From`
  (グラフ・パーセンタイルの開始日、空欄=全期間)。名前`VHV_Days`・`VHV_From`。
- **結果(F〜J列)**: 足ごと・合成の表(最新のIV(DB)・ライブのIV・HV(窓/短い窓)・IV−HV・
  パーセンタイル(From以降でIV−HV/IVが最新の値以下の日の割合))。その下にグラフ2枚(合成のIV・HV・
  短い窓のHV、IV−HV)。系列は名前`VHV_ChDate`等のスピル参照。元データが畳んだ列にあるので、
  グラフは「非表示の行と列のデータを表示」にしてある。
- **計算領域(AA:BK、列のグループで畳んである)**: AB3 `VolHistory`(名前`VHV_Data`)、AL4 日付
  (`VHV_Date`)、AM:AU 足ごとのIV・HV・短い窓のHV(HVは`SCAN`で2乗の累積をとり、N行前との差/N×年の日数の
  平方根)、AV:AZ 合成(IV・HV・短い窓のHV・IV−HV・IV−短い窓のHV)、BB3 ライブのボラ、BF:BK From以降の
  グラフ用の列(`FILTER`)。足の列はUDFの見出し(`1Yx10Y IV`等)を`XMATCH`で引くので、足の間に空欄があってもよい。

## ヒストリカル生データブック(`historical_data.xlsx`)

LSEG Workspaceから取得する生データブック(本プロジェクトの唯一のデータ供給元)。
リポジトリには`templates/`の雛形(受信領域Raw/RawFixing/RawVolの3行目以降と計算結果を消したもの)
だけを置く。開くとLSEGから自動取得してデータ入りになる(作業用のブックは`.gitignore`済み。
雛形の作り方は下の「Excelブックの雛形」)。
Python/UDFは使わず、LSEG Workspaceの`RDP.HistoricalPricing`(旧`RHistory`)と
Excel標準の関数だけで構成する。

- **設定**: 終了日(空欄なら今日)/取得営業日数(既定500)/開始日(任意)/
  銘柄グループ/並び順/フィールド(`MID_PRICE`)を入力。RIC一覧とリクエスト文字列は数式で組み立てる。
- **銘柄**: グループ・表示名・RIC・使用(○/×)。RICは`JP{テナー}ONI=TRDT`と
  `JPBOJ{n}ONI=TRDT`(リアルタイムのフィードと同じ系列)。
- **Raw**: `RDP.HistoricalPricing(RIC一覧,"MID_PRICE",リクエスト,,"CH:In;Fd",$A$3)`
  を1本だけ置く。出力は銘柄ごとに`Timestamp / Mid Price Close / Mid Price`の3列ブロックで、
  日付は銘柄ごとに異なる(同じ日付の行に揃っていない)。
- **Mid**: Rawの見出し(3行目=RIC、4行目=フィールド)から該当列を探し、
  共通の日付軸に`XLOOKUP`で揃えた日付×銘柄のマトリックス。
- **RawFixing / Fixing**: TONA実績(`JPONMU=RR`、既定フィールドの`Last Quote Close`)。
  日付はフィキシング対象日(参照日)で、直近日はBOJ速報値。`Fixing!A5#`(名前`TONAフィキシング`)が
  日付/レートの2列スピル。ql.Japanの営業日と過不足なく一致することを確認済み(2024-10〜2026-09)。
- 設定セルは名前付き範囲(`入力_終了日`、`実効開始日`、`リクエスト`など)で参照している。

### Implied Vol(Vol設定 / RawVol / Vol / VolMatrix)

JPY TONAスワップションのATMノーマルvol(bp/年)のヒストリカル。LSEG計算系列
`JPYTO{満期}X{原資産}ATM=R`(例 `JPYTO1YX10YATM=R`、チェーン`JPYTOSTNATM=R`、2022-03〜)を
フィールド`IMP_VOLT`で取得する(TRDTではなくこの系列を使う理由は[decisions.md](decisions.md))。
終了日・並び順は「設定」シートと共用。

- **Vol設定**: フィールド/開始日(任意)/RIC接頭辞・接尾辞と、満期軸(E列)・原資産軸(H列)の
  リスト(使用○/×、各最大20行)。選択軸の直積で`Vol選択銘柄`(満期/原資産/表示名`1Yx10Y`/RIC、
  N4#)を組み立て、`Vol_RIC一覧`・`Volリクエスト`を作る。既定は16満期×12原資産=192銘柄
  (1回の`RDP.HistoricalPricing`で約15秒、欠損なし)。
- **RawVol**: `RDP.HistoricalPricing(Vol_RIC一覧,Vol入力_フィールド,Volリクエスト,,"CH:In;Fd",$A$3)`。
  出力は銘柄ごとに`Timestamp / Implied Volatility`の2列ブロック。
- **Vol**: Midと同じ方式の日付×銘柄マトリックス(`Vol日付`=A6#、最大300銘柄)。
- **VolMatrix**: 基準日(空欄=最新)・比較日(任意)の満期×原資産マトリックスと差分(基準−比較)。
  入力日にデータが無ければ直前のデータ日を使う(`XLOOKUP`のmatch_mode -1)。差分は四則演算のみ。

実環境で確認した仕様: `"BID;ASK"`の複数フィールド指定は「無効なフィールド」、
Displayの`RH:Timestamp`は「無効な表示パラメーター」になる(公式例と異なる)。
Excelの`RDP.HistoricalPricing`はAPIの生フィールド名(`NRMV_OCPHM`等)を受け付けず、
正規化名(`IMP_VOLT`等)のみ。`IMP_VOLT`の中身は系列依存で、TRDTの
`JPY{満期}{原資産}TOATM=TRDT`ではログノーマルvol、LSEG計算の`=R`系列ではノーマルvol。

## Excelブックの雛形(`templates/`、`tools/template.py`)

リポジトリはPublicで、LSEGのコンテンツは再配布禁止のため、ブックはLSEGのデータを含まない
雛形だけを管理する。リポジトリ直下のブック(`PricingSheet.xlsm`・`SwapAnalysis.xlsm`・
`historical_data.xlsx`)は作業用で`.gitignore`済み。`python -m tools.template`が作業用のブックから
`templates/`に雛形を書き出す(ブックを展開してXMLを書き換える。Excel・QuantLib不要)。

消すもの(場所の一覧ではなく規則で消す。LSEGのデータはほぼ全て数式の計算結果として入るため):

| 規則 | 消えるもの |
|---|---|
| 数式のセルの計算結果(キャッシュ値)を消し、数式だけ残す | RtGet/TRのレート、UDFの結果、xlwingsのエラー表示 |
| 配列数式(`t="array"`)の`ref`範囲の先頭以外のセルを消す | スピル結果(値のセルとして保存される) |
| `RECEIVE_AREAS`に宣言したシートの指定行以降を消す | `RDP.HistoricalPricing`が値として書き込む受信領域(historical_dataのRaw/RawFixing/RawVol) |
| グラフ(`numCache`/`strCache`)・外部リンク(`sheetData`)のキャッシュを消す | HistChart等のグラフの系列値、historical_data.xlsx参照の値 |
| `xl/volatileDependencies.xml`を削除する | RTDの受信値のキャッシュ |
| absPath・外部リンクの代替URL(`file:///`)・作成者/更新者名を消す | ローカルの絶対パス、本名 |
| どのセルからも参照されなくなった共有文字列を空にする(番号は保つ) | 受信領域のRIC名など |

手入力の値(コンベンション、フラグ、取引の入力など)は数式ではないので残る。開いたときに全再計算する
(`fullCalcOnLoad`)。

検査(`python -m tools.template --check`、`--staged`でステージ済みの内容):
- 雛形にもう一度同じ処理をかけても変わらないこと(作る処理と検査の判断が食い違わない)
- 許可した種類の部品だけであること(ピボットのキャッシュ等、知らない部品があれば失敗)
- ローカルのパス(`C:\Users\…`、`file:///`)が無いこと(`vbaProject.bin`のxlwings参照は既知で除外)
- 配列を返すUDF(`udf.py`の戻り値の型が`List[...]`のもの)のセルが動的配列(`cm`属性)であること
- `templates/`以外にブックがgitに入っていないこと(`git ls-files`で次にコミットされる中身を見る)

pre-commitフック(`.githooks/pre-commit`、`git config core.hooksPath .githooks`で有効化)と
GitHub Actions(`.github/workflows/templates.yml`)で走る。テストは`tests/test_template.py`。
