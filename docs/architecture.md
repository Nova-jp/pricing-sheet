# 全体構成

## 目的

JPY OIS(TONA)スワップのプライシング(`PricingSheet.xlsm`)を中心に、分析(`SwapAnalysis.xlsm`)・現物オプション
(`BondOption.xlsm`)・ポジション管理(`PositionSheet.xlsm`)のブックを、Python/QuantLibのUDF(xlwings)で計算する。
LSEG Workspaceのデータを、日中はライブのフィードから、過去分は朝バッチが作るローカルのDBから読む。
設計判断は[decisions.md](decisions.md)、ブックごとの構成は下の「ブックごとのドキュメント」。

## データフロー

```
[Excel: historical_data.xlsx(作業用。リポジトリにはtemplates/の雛形のみ)]       [Neon(Postgres、他プロジェクトと共用)]
  LSEG Workspace(RDP.HistoricalPricing)から取得し、保存する                      public."ASW_data"(JGB銘柄別ASW、他プロジェクトのテーブルは触れない)
  - Midシート: 日付×テナーのミッド(1M〜40YはJP{テナー}ONIPE=TRDTの引けの確定値。1W・会合スワップはJP…ONI=TRDT)
  - Fixingシート: TONA実績(JPONMU=RR)。バッチがDBに取り込み、過去起算とO/Nのピラーに使う
        │                                                                              │
        ▼  朝に手動実行: python -m engine.common.morning_batch                          │ SELECTのみ、updated_atで差分取得
[朝バッチ engine/common/morning_batch.py]  設定: historical_curve.toml(引き方・使うテナー)   │
  ① Mid/Fixingを読み、DBのquotes/fixingsに取り込む(過去に取り込んだ日も残る)              │
  ② 日付ごとに選択テナー(O/Nはその日のTONA実績)でカーブを構築 → 暦日ごとのDFを保存        │
     (欠損テナーは外して構築、日本の非営業日は作らない、入力が変わった日だけ作り直す)      │
  ③ engine/common/neon_source.py経由でASWスプレッドを差分取得 ◀─────────────────────────┘
     (Neonに繋がらなくても①②は続行する)
  (①〜⑥のどれも、保存するのは昨日までの日付だけ。今日の値はブックがライブで見る)
  ④ engine/common/jgb_source.py経由でLSEG(Python API)からJGBの銘柄情報・引け値(JCLの履歴、直近10日分)を取得
     (現物オプション用。Workspaceに繋がらなくても①〜③は続行する。--skip-jgbで行わない)
     --jgb-history-from を付けたときは、引け値の過去分も取る(10年債はその日から、他の年限はスワップと同じ期間。JCL、無い日は大和・みずほで補う)
  ⑤ engine/common/futures_source.py経由でLSEGから長期国債先物の限月ごとの清算値を取得(2020年から、差分)
     (国債先物のベーシス・CTD・つなぎ足用。計算は engine/common/jgb_futures.py。--skip-jgbで④と一緒に行わない)
  ⑥⑦ LSEGからスワップションのプレミアムを差分取得し、その日のカーブで営業日ベースのvolにする(vol_quotes)
  ⑧ スワップションの原資産(⑦と同じ契約)の格子の全銘柄のFwd・年金・前の日付のカーブでのFwdを計算(swaption_forwards)
     (入力が変わった日だけ。VolHV・VolLevelが読む。engine/common/swaption_forward.py)
        │
        ▼
[data/historical.db(ローカルのSQLite。LSEG/Neon由来なのでコミットしない)]
        │
        ▼  日中はDBを読むだけ(ブートストラップしない)
[Excel: 各ブック]
  評価日=今日: ライブのレート(LiveQuotes)+採否フラグ ─▶ UDFがその場でカーブを構築(最新の1組を共有)
  評価日=過去日・ヒストリカル系のUDF ─▶ DBのカーブ・引け値・vol等を読むだけ
```

リアルタイムのレートは、常駐のフィード(Pythonのプロセス。LSEGのストリーミングを購読し続ける)から
手動計算のブックへファイルで渡す(理由は[decisions.md](decisions.md)の「リアルタイムのレート」):

```
[LSEG Workspace(起動・ログイン中)] ─ localhostのAPI Proxy ─▶ [フィード: python -m engine.common.live_feed]
        │  live_feed.toml のRICを購読。値が変わったら最大 write_interval_sec(1秒)ごとに書く。
        │  ティックが無くても heartbeat_sec(2秒)ごとにデータセットごとの状態(ハートビート)を書く
        ▼
[data/live.db(ローカルのSQLite。LSEG由来なのでコミットしない。git worktreeからもメインの作業ツリーのものを読む)]
        │  そのシートの取得モード(B1)がLiveなら再計算(Shift+F9)のたびに読む。Fixedなら読まない。
        │  ハートビートが stale_after_sec(10秒)より古いか、LSEGとの接続が正常でなければエラー
        ▼
[Excel(手動計算): PricingSheet.xlsm / SwapAnalysis.xlsm / BondOption.xlsm]
  各フロントシートが自分で =IF($B$1="Fixed", LiveSnapshot("swap"), LiveQuotes("swap")) を持つ
        ─▶ そのシートのUDF(引数の形は変更なし)
```

Shift+F9はそのシートだけを再計算し、他のシートの値は計算し直さないので、ライブのレートは使うシートごとに読む
(共通のRealTimeシートは持たない)。

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
  PricingSheetは`"swap"`の代わりに定義名`LiveSwap`(LCHは`LiveSwapLCH`)を渡し、ブローカーを切り替える(pricing.md)。
  単独のセルに置き、加工は別のセルでする。列(下表のライブのレート〜カーブ用の範囲)は列のグループで
  折りたたんである(Main H:Q、pricing AF:BG(PricingTableの出力・LCHのレートを含む)、ASWPricer AF:BF(AswPricingTableの出力を含む)、HistChart AJ:AS、PCA CA:CD、PCAGrid CA:HZ(計算領域ごと)、VolPCA LA:LC、VolHV AA:BK(計算領域ごと)、VolLevel AA:ZZ(計算領域ごと)、BOJ K:W)。
- **カーブ用の範囲**(PricingSheetのシート): `=LET(q,ライブ#,t,Main!$A$11:$A$54,HSTACK(Main!$B$11:$B$54,t,
  INDEX(q,XMATCH(t,INDEX(q,,1)),{2,3,4})))`=採否/Ticker/値/起算日/満期日(44行、Mainのテナーの並び)。
  UDFには`TAKE(範囲#,,3)`(採否/Ticker/値)を渡す。フィードに無いTickerは`#N/A`。

| シート | ライブのレート | カーブ用の範囲 |
|---|---|---|
| Main | H3 | M3 |
| pricing | AF4 | AK4 |
| pricing(LCH) | AX4(`"swap_lch"`) | BC4 |
| HistChart | AJ3 | AO3 |
| ASWPricer | AF4 | AK4 |
| PCA(SwapAnalysis) | CA4(Ticker/値を`CHOOSECOLS`で渡す) | なし |
| PCAGrid(SwapAnalysis) | CD4(PCAと同じ渡し方) | なし |
| VolPCA(SwapAnalysis) | LA3(`"vol"`、銘柄/RIC/値をそのまま渡す) | なし |
| VolHV(SwapAnalysis) | BB3(`"vol"`、`IV (Live)`の`XLOOKUP`だけが使う) | なし |
| VolLevel(SwapAnalysis) | AA3(`"vol"`)・AE3(`"swap"`) | AJ3(テナー表はこのシートのAP:AQ) |
| BOJ(SwapAnalysis) | K3 | P3(テナー表はMainではなくこのシートのV:W) |

フィードの設定は`live_feed.toml`(リポジトリ直下。調整する値はここにまとめる): 秒数(`[timing]`)と
データセットごとのRIC。
- `"swap"`(kind=`ois`): O/N〜40Y・RD・M1〜M9。行=[Ticker, BID/ASKのミッド, 起算日`GV1_DATE`, 満期日`GV2_DATE`]
  (起算日・満期日は会合スワップだけ)。O/NはTradition `JP1DONI=TRDT`のミッド(=TONA)。
- `"swap_lch"`(kind=`ois`): LCH清算の円OIS(TraditionのLCHのアウトライト`JPY{テナー}OISL=TRDT`)。O/N〜40Y
  (Tickerは`"swap"`と同じ表記。12M/24M/36Mは`JPY1Y/2Y/3YOISL=TRDT`、1Wはスポット・ウィーク`JPYSWOISL=TRDT`、
  O/Nは`JPY1DOISL=TRDT`。会合スワップは無い)。pricingシートのindex=LCHの行だけが使う(下の「pricingシート」)。
- `"swap_totan"`・`"swap_lch_totan"`(kind=`ois`): 東短(Totan ICAP)の`JPY{テナー}OIS=TKFX`・`JPY{テナー}OISL=TKFX`、会合スワップは
  `JPYBJ{n}OIS=TKFX`(BJ0=RD)。Tickerと行の並びは`"swap"`・`"swap_lch"`と同じで、東短に無い21MはRICを空欄にする(値が空欄になり
  カーブから外れる)。起算日・満期日は`START_DT`/`MATUR_DATE`で、RDには無い。PricingSheetのBrokerで切り替えて読む(pricing.md)。
- `"vol"`(kind=`vol`): 満期×原資産の格子(満期1M〜20Yの13本×原資産13本(1Y〜10Y・15Y・20Y・30Y。満期+原資産が40年を超える15Yx30Y・20Yx30Yは除く)。2M・6Y・8Y・9Y満期はクオートが無いので使わない)。行=[銘柄`1Mx2Y`, RIC`JPY{満期}{原資産}TOATM=TRDT`,
  営業日ベースのノーマルvol(bp/年 = bp/日×√252)]。購読するのはTraditionのATMストラドルのフォワード・プレミアム
  (`PRIMACT_1`、bp)で、フィードが書くたびに`curve`のデータセット(`"swap"`)のライブのレートでカーブを組み
  (テナー・構築方法は`historical_curve.toml`)、`swaption.py`でvolに換える(取引日=書く時刻の日付。どちらかが
  変われば計算し直す)。カーブを組めなければ値を空欄にし、状態のメッセージに理由を出す。2Mx6Y・8Y・9Yは値が無く空欄。
- 銘柄の状態が異常(Suspect・存在しない等)か、BID・ASKのどちらかが無い行は値を空欄にする。
読む側はTicker(volはRIC)で引き当てるので、行の並びがブックごとに違ってもよい(フィードに無いTickerは`#N/A`)。
データセットごとの状態(`feed`テーブル): `ok`/`connecting`/`reconnecting`/`suspect`(全銘柄が異常)/`stopped`。
接続が切れたらまずライブラリの再接続を待ち、`resubscribe_after_sec`(30秒)たっても戻らなければセッションと
購読を開き直す。ログは`data/live_feed.log`。

データの供給元はLSEG Workspace(カーブ・プライシング本体)とNeon(ASWスプレッド、参照データのみ。カーブには使わない)。
詳細は[decisions.md](decisions.md)の「データの供給元」。

## 計算エンジン(`engine/`)

プライシング計算(日付・スケジュール生成を含む)は自前実装を持たず、全て
QuantLib純正のAPI(`ql.MakeOIS`/`ql.OISRateHelper`/`ql.PeriodParser`/
`ql.Date.from_date`等)に委ねている。自前で持つのは「業務ルール(テナー表記・
roll_conv文字列等)→QuantLib引数」の対応表と、Excel境界の型変換のみ
(理由は[decisions.md](decisions.md)の「カーブと評価」)。

`engine/`配下はブックごとの並列パッケージと共有の`common/`(理由は[decisions.md](decisions.md)の「パッケージ構成」):

```
engine/
  common/          # どのブックからも使う共有エンジン・データ層(朝バッチ・フィードを含む)
  swap_pricing/    # PricingSheet.xlsm固有のUDF(pricing/historical/aswの3機能)
  pca/             # SwapAnalysis.xlsm固有のPCA分析
  vol/             # SwapAnalysis.xlsm固有のIV vs HV(Fwdの日次変化)・ボラとFwdの水準の散布図
  fwd/             # SwapAnalysis.xlsm固有のFwd(NyMy)の監視とヒストリカル(3方式)
  fut/             # SwapAnalysis.xlsm固有の長期国債先物(つなぎ足・CTDのベーシスのヒストリカル・限月の重ね合わせ)
  boj/             # SwapAnalysis.xlsm固有のBOJ会合スワップとスポットのずれ
  bond_option/     # BondOption.xlsm固有の現物オプションの表UDF(計算の部品は common/jgb_option/)
  position/        # PositionSheet.xlsm固有のポジション・リスク管理(会社のリスクの抽出+日中の取引のデルタ・ガンマ+現物オプションのグリークス)
```


### 共有エンジン・データ層(`engine/common/`)

| ファイル | 役割 |
|---|---|
| `conventions.py` | 業務上の表記→QuantLibの対応表を集約: `ql.Japan()`カレンダー、dcf文字列→DayCounter、freq、roll conv→生成ルール、IMMコード(M27)→日付、テナー表記の読み替え(12M→1Y)。自前の日付計算ロジックは持たず、呼び出し側がQuantLibのAPIを直接使う |
| `curve.py` | スポットレート辞書→`ql.PiecewiseConvexMonotoneForward`によるカーブ構築。O/Nは`DepositRateHelper`、それ以外は`OISRateHelper`(`rule=ql.DateGeneration.Forward`・`endOfMonth=False`で構築) |
| `terms.py` | 取引条件の型(3段): `SwapConvention`(規約: fix freq/fix dcf/float freq/roll conv。作るときに値を確認)⊂`SwapTerms`(規約+起算日+tenor/end=1本の具体的なスワップ。パーレートの計算単位)⊂`SwapParams`(取引条件+想定元本・PAY/REC・固定金利。PV・デルタの計算単位)。標準の規約`STANDARD`。Excelの行を型に直すのは各feature udfモジュール(`pricing/udf.py`等)の入口だけ |
| `market_data.py` | 評価日時点の市場データ一式の型`Market`(評価日・カーブ・TONA指標・TONA実績)。実績は作るときに「評価日より前」だけに切る(先読みの排除はここだけ)。QuantLib以外に依存しない |
| `swap_pricer.py` | `ql.MakeOIS`(→`ql.OvernightIndexedSwap`)によるPV・パーレート計算。取引条件(`SwapTerms`)と市場データ(`Market`)を受け取る。評価の前提(評価日・実績の登録・満期済み判定・実績の確認)は`_prepare_swap`に集約し、`price_swap`(パーレート・アニュイティ・PV)と`par_rate`(パーレートだけ、ヒストリカル用)が共通で通る。過去起算のスワップは、取引条件から必要な日を判断して`Market`の実績を登録(不足時はエラー)。評価日は呼び出しごとに設定し直す。`swap_dates`はカーブなしで同じビルダーのスケジュールから起算日・満期日(休日調整後)を返す(ヒストリカルのROLLINGで営業日数を数えるのに使う) |
| `market.py` | 評価日のマーケット(今日=ライブのレート、過去日=ヒストリカルDBのその日のレート。TONA実績はどちらもDB。清算機関(JSCC/LCH)ごとのスナップショット)と、全行・全UDFで共有するカーブ一式(基準カーブと全テナーを0.1bp動かしたカーブ。必要になったときに1回だけ作る。テナー別のバンプはカーブを作らず、基準カーブのクォートを一時的に動かして戻す。バンプはカーブに使っているテナーだけで、デルタは10倍して1bpあたりにする(risk.per_bp)。キャッシュは清算機関ごとに最新の1組だけで、ヒストリカル系UDFは入口で`release_curves`で手放す)。計算には`Market`を渡す |
| `risk.py` | 並行/バケットデルタ(`market`のカーブ一式を使う。バケットは`bucketed_deltas`で全ポジションをまとめて、テナーごとにクォートを1回だけ動かす)、notional逆算 |
| `historical_store.py` | ヒストリカル日次カーブDB(`data/historical.db`)の読み書き。カーブは評価日から暦日1日刻み・50年分のDFとして保存し、`ql.DiscountCurve`で復元する(引き方に依存しない形式。理由は[decisions.md](decisions.md))。生レート・TONA実績・ASWスプレッド(`asw_quotes`、JGB銘柄別、カーブには使わない参照データ)・スワップションのvol(`vol_quotes`)と原資産のFwd(`swaption_forwards`)も保存。`engine/common/history.py`(PCA)も`HistoricalStore`をここから読む |
| `morning_batch.py` | 日次カーブDBを作るバッチ(手動実行)。`historical_data.xlsx`を読み、`historical_curve.toml`の設定(引き方・使うテナー)でカーブを構築して保存。`neon_source.py`経由でASWスプレッドも差分取得する(Neonに繋がらなくてもカーブ構築は続行)。スワップションのプレミアムを差分取得し(`swaption_source.py`)、その日のカーブで営業日ベースのvolにして`vol_quotes`に保存する(入力が同じ日は計算し直さない)。続けてスワップションの原資産のFwdを`swaption_forwards`に保存する(`swaption_forward.py`) |
| `neon_source.py` | Neon(Postgres)の`public."ASW_data"`から読み取り専用でASWスプレッドを取得する(他のテーブル・他プロジェクトのデータには触れない)。接続文字列は環境変数`NEON_CONNECTION_STRING`(`.env`)。`updated_at`を基準に差分取得できる。5432番に繋がらなければHTTPS(443番、`https://<ホスト>/sql`)で10万行ずつ取る |
| `live_feed.py` | リアルタイムのフィード(常駐プロセス、`python -m engine.common.live_feed`)。`live_feed.toml`を読み、LSEG(`lseg.data`のデスクトップセッション)でRICを購読し続け、ブックが読む形の行とデータセットごとの状態(ハートビート)を`live_store`に書く。切断時の開き直し、二重起動の防止 |
| `live_store.py` | リアルタイムのレートの受け渡しDB(`data/live.db`)。データセット("swap"・"vol")ごとに最新の行(JSON、日付は型を保って)と値が最後に変わった時刻、シートごとの固定の値、フィードの状態(`feed`)を持つ。値が変わらなければ書かない。ライブで読むときはフィードの状態を確かめる。WALで、書き込み中も別のプロセスから読める |
| `live_udf.py` | `live_store`のUDF(読むだけ)。`LiveQuotes`/`LiveSnapshot`/`LiveInfo`(PricingSheet・SwapAnalysis・BondOption共通。`LiveQuotes`はvolatileで最新の行を読み、シートごとの固定の値として保存する。フィードが止まっていればエラー。`LiveSnapshot`はその固定の値を返す。`LiveInfo`はその時刻) |
| `forward.py` | 保存済みの日次カーブで多数のFwdスワップレートを日付ごとに評価する`ForwardPricer`(同じ取引条件のスワップを1回だけ組み、カーブのハンドルをつなぎ替えて`fairRate`を読む。値は`swap_pricer.par_rate`と一致)。`engine/vol/`・`engine/fwd/`が使う |
| `bond.py` | JGBの規約: 単利⇔単価(自前の式、小数3桁切り捨て)、うるう日除きの日数、経過利子・利払日(QuantLib)、受渡日、レポでの先渡単価(`forward_price`)。現物オプション([bond_option.md](bond_option.md))と国債先物が使う |
| `jgb_option/` | 国債店頭オプションの部品(BondOptionとPositionSheetが使う): `pricer.py`(先渡MD・期間・Black式・ITMバイライトのストライク)、`vol.py`(スワップションのボラの格子からの補間)、`hv.py`(現物とスワップのHVの比、Vol ratio)。式は[bond_option.md](bond_option.md) |
| `jgb_source.py` | LSEG(`lseg.data`のデスクトップセッション)からJGB全銘柄の銘柄情報と引け値(`{RIC}JCL`の履歴の単利、JCLに無い日は大和・みずほで補う)を取得する(朝バッチの④。毎朝は昨日までの直近10日分、`--jgb-history-from`のときは過去分も同じ取り方) |
| `jgb_futures.py` | 長期国債先物の規約と計算(LSEGに依存しない): 限月・受渡適格銘柄・交換係数・ベーシス・CTD(ネットベーシス最小)・レポの階段・つなぎ足。規則はモジュールのdocstring、判断の理由は[analysis.md](analysis.md)の「長期国債先物のベーシス」 |
| `futures_source.py` | LSEGから長期国債先物の限月ごとの清算値を取得する(朝バッチの⑤) |
| `swaption.py` | スワップションのATMプレミアム(Tradition、ATMストラドルのフォワード・プレミアム bp)→ 営業日ベースのノーマルvol(bp/年)。原資産のフォワード・スワップの年金とフォワードから`ql.bachelierBlackFormulaImpliedVol`でσ√Tを求め、満期までの営業日数(当日を含む)で割って×√252。契約は`underlying`の1か所(満期日=取引日+満期(Following)、起算日=満期日+2営業日=スワップのスポットと同じT+2)。朝バッチの⑦⑧・フィード・`VolFwdLive`が使い、BondOptionの補間も同じ満期日・営業日数を使う |
| `swaption_forward.py` | スワップションの原資産のFwd(DBの`swaption_forwards`)を日次カーブから計算する(朝バッチの⑧)。日付×格子の全銘柄ごとに、契約(`swaption.underlying`)の日付・営業日数・Fwd(%)・年金と、同じ契約を前の日付のカーブで評価したFwd(`forward_prev`、日次変化=ロールダウンを含まない)。入力(計算方法・その日と前の日付のカーブの入力ハッシュ・銘柄)が変わった日だけ計算する(全日付で約1分、以後は1日0.1秒程度) |
| `swaption_source.py` | LSEG(Python API)からTraditionのプレミアムのヒストリカル(`JPY{満期}{原資産}TOATM=TRDT`の`FWPM_OCPHM`・`ATM_STRIKE`、2022-01-12〜)を取得する。格子は満期13本(2M・6Y・8Y・9Yは使わない)×原資産13本(1Y〜10Y・15Y・20Y・30Y。満期+原資産が40年を超える15Yx30Y・20Yx30Yは除く)(`in_grid`、フィードも同じ規則)。DBに無い銘柄(格子に足した銘柄)は最初の日から取る。朝バッチの⑥が使う |
| `history.py` | DBを日付×銘柄の表で読む。swap=`quotes`(生ミッド、M1〜M8を含む全テナー)+`fixings`(O/N=その日のTONA実績)、vol=`vol_quotes`(満期x原資産)。日本の営業日のみ。DBの更新時刻が変わるまでプロセス内にキャッシュ(PCA用。SwapAnalysisが使う) |
| `pca.py` | PCA: windowの選び方(`estimate`)、復元日の実績(`observe`)、固有値分解。`principal_components`は「サンプル行列→固有値・固有ベクトル」だけの汎用関数(SwapAnalysisが使う) |
| `paths.py` | リポジトリ内パス解決(クローン先の絶対パスに依存しない) |
| `udf_common.py` | どのブックのUDFからも使う共有部分: Excel境界の型変換(`_to_date`等)、採否フラグでのレートの選別(`_rates_from_range`)、評価日の解決、評価日時点のカーブ一式(`_market`)。各UDFモジュールは`udf_common.xxx(...)`の形(属性参照)で呼ぶ(理由は[decisions.md](decisions.md)の「パッケージ構成」) |


## ブックごとのドキュメント

| ブック | パッケージ | ドキュメント |
|---|---|---|
| `PricingSheet.xlsm` | `engine/swap_pricing/` | [pricing.md](pricing.md) |
| `SwapAnalysis.xlsm` | `engine/pca/`・`vol/`・`fwd/`・`fut/`・`boj/` | [analysis.md](analysis.md) |
| `BondOption.xlsm` | `engine/bond_option/` | [bond_option.md](bond_option.md) |
| `PositionSheet.xlsm` | `engine/position/` | [position.md](position.md) |

## テスト(`tests/`)

`python -m unittest discover -s tests`(QuantLibの入ったx64のPython環境)。LSEG・Neonには繋がない(取得部分はモック、
データは合成)。価格の値はQuantLibを正とし、自前ロジック(入力の解釈・スケジュール・エラー処理・ブートストラップの
再現精度)をテストする。

| テスト | 対象 |
|---|---|
| `test_curve_repricing.py` | 市場レート逆算の精度(全pillarが機械精度で再評価できる。ハード要件の回帰防止) |
| `test_terms.py`・`test_market_data.py`・`test_past_start.py`・`test_imm.py` | 取引条件の型、市場データ、過去起算と評価日、IMMコードとstartの表記 |
| `test_market_risk.py`・`test_pricing_table.py`・`test_lch_curve.py` | カーブの共有・デルタ・RiskGrid・PricingTable・LCHのカーブ |
| `test_historical.py`・`test_hedge.py` | ヒストリカル(FIXED/ROLLING・先読みしないフィキシング)、ヘッジの重ね表示 |
| `test_historical_store.py`・`test_morning_batch.py` | 日次カーブDBの保存・復元精度と朝バッチ |
| `test_neon_source.py` | ASWの取得(対象テーブル・差分取得・接続の後始末・HTTPSへの切り替え) |
| `test_asw.py`・`test_asw_pricer.py` | ASWシート・ASWPricer |
| `test_live.py`・`test_live_feed.py`・`test_paths.py` | ライブのレートの受け渡し・フィード・worktreeのパス |
| `test_swaption.py`・`test_swaption_forward.py` | スワップションのボラの換算・原資産のFwd |
| `test_pca.py`・`test_vol.py`・`test_fwd.py`・`test_fut.py`・`test_jgb_futures.py`・`test_meeting_curve.py` | 分析ブック(PCA・ボラ・Fwd・先物・BOJ) |
| `test_bond_option.py` | 現物オプション(ローカルにサンプルがあればプライサーとの一致も) |
| `test_bond_option_hv.py` | 現物オプションのVol ratio(現物とスワップのHVの比) |
| `test_position.py` | ポジション・リスク管理 |
| `test_position_bond_option.py` | ポジション・リスク管理の現物オプション(グリークス・ピラーとベガの格子への分解) |
| `test_template.py`・`test_udf_arg_names.py` | 雛形の作成と検査、UDFの引数名 |

## ヒストリカル生データブック(`historical_data.xlsx`)

LSEG Workspaceから取得する生データブック(スワップの引け値とTONA実績の供給元。朝バッチが保存済みの値を読む)。
リポジトリには`templates/`の雛形(受信領域Raw/RawFixing/RawVolの3行目以降と計算結果を消したもの)
だけを置く。開くとLSEGから自動取得してデータ入りになる(作業用のブックは`.gitignore`済み。
雛形の作り方は下の「Excelブックの雛形」)。
Python/UDFは使わず、LSEG Workspaceの`RDP.HistoricalPricing`(旧`RHistory`)と
Excel標準の関数だけで構成する。

- **設定**: 終了日(空欄なら今日)/取得営業日数(既定500)/開始日(任意)/
  銘柄グループ/並び順/フィールド(`MID_PRICE`)を入力。RIC一覧とリクエスト文字列は数式で組み立てる。
- **銘柄**: グループ・表示名・RIC・使用(○/×)。RICは1M〜40Yが`JP{テナー}ONIPE=TRDT`(15時ごろの引けの確定値)、
  1Wと会合スワップが`JP1WONI=TRDT`・`JPBOJ{n}ONI=TRDT`(ONIPEの系列が無い。リアルタイムのフィードと同じ系列)。
  理由は[decisions.md](decisions.md)の「ヒストリカルDBと朝バッチ」。
- **Raw**: `RDP.HistoricalPricing(RIC一覧,"MID_PRICE",リクエスト,,"CH:In;Fd",$A$3)`
  を1本だけ置く。出力は銘柄ごとに`Timestamp / Mid Price Close / Mid Price`の3列ブロックで、
  日付は銘柄ごとに異なる(同じ日付の行に揃っていない)。
- **Mid**: Rawの見出し(3行目=RIC、4行目=フィールド)から該当列を探し、
  共通の日付軸に`XLOOKUP`で揃えた日付×銘柄のマトリックス。
- **RawFixing / Fixing**: TONA実績(`JPONMU=RR`、既定フィールドの`Last Quote Close`)。
  日付はフィキシング対象日(参照日)で、直近日はBOJ速報値。`Fixing!A5#`(名前`TONAフィキシング`)が
  日付/レートの2列スピル。ql.Japanの営業日と過不足なく一致することを確認済み(2024-10〜2026-09)。
- 設定セルは名前付き範囲(`入力_終了日`、`実効開始日`、`リクエスト`など)で参照している。
- 実環境で確認した仕様: `"BID;ASK"`の複数フィールド指定は「無効なフィールド」、Displayの`RH:Timestamp`は
  「無効な表示パラメーター」になる(公式例と異なる)。APIの生フィールド名(`NRMV_OCPHM`・`FWPM_OCPHM`等)は受け付けず、
  正規化名(`IMP_VOLT`等)のみ。

`Vol設定`/`RawVol`/`Vol`/`VolMatrix`のシート(LSEG計算系列のvol)は朝バッチが読まなくなり、使っていない
(ブックから消すのは[roadmap.md](roadmap.md)の16)。

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
- ノートブック(`.ipynb`)に出力・実行番号が残っていないこと(出力にはDBの値が入る)

pre-commitフック(`.githooks/pre-commit`、`git config core.hooksPath .githooks`で有効化)と
GitHub Actions(`.github/workflows/templates.yml`)で走る。テストは`tests/test_template.py`。

## 分析ノートブック(`notebooks/`)

ブックとは別の、手元で動かす分析用のJupyterノートブック。`data/historical.db`を読むだけで、UDF・ブックには
関わらない。追加のパッケージは`notebooks/requirements.txt`(matplotlib・jupyterlab等)。

- `vol_strategies.ipynb` — ボラサーフェスの戦略(1M売り・1M/1Y比のタイミング・PC2売り・日銀会合の前後・
  週末とIVの形)の可視化と検証。部品は`vol_lab.py`(Traditionのプレミアム→IV、ATMストラドルの一括の時価評価、
  ローリングPCA)。P&Lは建てた時のベガ1単位あたりのボラbp。

出力にはLSEG・Traditionのデータが入るので、**出力を消してからコミットする**(`tools.template --check`が止める)。
