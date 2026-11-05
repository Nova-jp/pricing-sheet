# プライシングシート(`PricingSheet.xlsm`、`engine/swap_pricing/`)

任意のコンベンションのJPY OIS(TONA)スワップの、パーレート・PV・デルタ・リスクの合算(RiskGrid)と、
ヒストリカルなレート推移(アウトライト・カーブ・フライ)、JGBのASW(ヒストリカル・プライサー)。
横断的な方針(UDFの分担・カーブ・評価日・ライブのレート)は[decisions.md](decisions.md)。

シート: `Main` / `pricing` / `HistChart` / `ASW` / `ASWPricer` / `_xlwings.conf`。ASW以外のフロントシートの1行目は
取得モードの欄([architecture.md](architecture.md)の「データフロー」)。

## コード

`PricingSheet.py`は`engine/swap_pricing/udf.py`の再エクスポートだけ。`udf.py`自体も薄い再エクスポート層で、
実体はシート(機能)ごとに分かれている。共有部分は`engine/common/udf_common.py`。

| ファイル | 役割 |
|---|---|
| `udf.py` | Excelから呼べるUDFの単一の窓口(`Ping`以外の実体は下のモジュール) |
| `pricing/udf.py` | Main/pricingシートのUDF: `PricingTable`(pricing表全体)/`FairRate`/`SwapPV`/`SwapDelta`/`SwapAnnuityDelta`/`NotionalForDelta`(1行用)/`RiskGrid`/`MarketInfo`。どれも下の部品を組み合わせるだけ |
| `pricing/row.py` | 1行分のセルの値→取引条件の型(`swap_terms`・`swap_params`)。start列のテナー・営業日数の読み替えもここ |
| `pricing/measures.py` | 列ごとの計算: `par`(想定元本1・固定金利なしの評価)/`fair_rate`/`pv`/`parallel_delta_mm`/`annuity_delta_mm`/`notional_for_delta`。`par`の結果・PVを渡すと評価をやり直さない(PricingTableは1行3回の評価) |
| `pricing/table.py` | PricingTableの本体: 見出し名で列を探し、flag=1の行だけ計算し、行ごとのエラーをmessageに閉じ込める |
| `historical/pricer.py` | 保存済みの日次カーブから、任意コンベンションのヒストリカル・パーレート系列を計算(ブートストラップしない)。FIXED/ROLLING(下の「ヒストリカル」) |
| `historical/udf.py` | HistChartシートのUDF: `HistoricalRate`/`HistoricalRates`/`HedgeHistoricalRates` |
| `historical/hedge.py` | ヘッジの重ね表示用に、スポットのテナーごとのパーレートを指定した日付の並びで返す(`historical/pricer.py`と同じ経路、標準の規約) |
| `asw/udf.py` | ASWシートの`AswHistoricalRate`/`AswHistoricalRates`(DBの`asw_quotes`を読むだけ)、ASWPricerシートの`AswPricingTable` |
| `asw/pricer.py` | ASWPricerの計算: 現物の単価・複利(スポット受渡T+1、`bond.compound_yield`)、複利1bpのBPV(`bond.dollar_duration`)、par-par ASW(`ql.AssetSwap`)。MMSは`pricing/measures.py`の`par` |
| `asw/table.py` | `AswPricingTable`の本体(pricing表と同じく見出し名で列を探し、flag=1の行だけ、行ごとのエラーはmessageへ) |

テスト: `test_market_risk.py`(カーブの共有・デルタ・RiskGrid・評価日・startのテナー・HistChart)、`test_past_start.py`、
`test_imm.py`、`test_historical.py`、`test_hedge.py`、`test_asw.py`、`test_asw_pricer.py`。

## Mainシート

- B4: 評価日(名前`評価日`、空欄=今日)。全UDFの評価日とHistChartの基準日はこのセルを参照する。
  今日ならライブのレート、過去日ならヒストリカルDBのその日のレート(O/Nはその日のTONA実績)でカーブを組む。
  **過去日でもテナーはMainの採否フラグで選ぶ**(ユーザー選択。今日と過去日で同じグリッドになり比べやすい。DBの
  `historical_curve.toml`の選択とは異なることがある)。
- B5: `=MarketInfo(TAKE($M$3#,,3), 評価日)`(カーブの元データとテナー数)。
- A11:B54: カーブのテナー。A=Ticker(`live_feed.toml`の`"swap"`と同じ表記。O/N〜40Y、RD、M1〜M9)、
  B=**採否フラグ**(`1`/空欄=使う、`0`=使わない)。会合スワップ(RD・M1〜M9)は`0`(メインのカーブには使わず
  `curve.EXCLUDED_MEETING_TENORS`で`skipped_tenors`に計上。会合スワップの分析はSwapAnalysisのBOJシート)。
  フラグ0のテナーはカーブにもデルタのバンプにも入らない(RiskGridでは空欄)。
  フラグはブックごとの設定なのでフィードには持たせず、このブックではMainの1か所に置く(定数なので、Shift+F9で
  他のシートから参照しても問題ない)。
- 各シートのカーブ用の範囲(`=LET(q,ライブ#,t,Main!$A$11:$A$54,HSTACK(Main!$B$11:$B$54,t,INDEX(q,XMATCH(t,INDEX(q,,1)),{2,3,4})))`
  =採否/Ticker/値/起算日/満期日)がこの並びでライブのレートを引き当て、UDFには`TAKE(範囲#,,3)`を渡す。
  ライブのレート・カーブ用の範囲のセル: Main H3/M3、pricing AF4/AK4(LCH: AX4/BC4)、HistChart AJ3/AO3、ASWPricer AF4/AK4。

## ブローカーの切り替え(pricing!F1)

- **pricing!F1(定義名`Broker`、`Tradition`/`Totan`)で、ブック全体のライブのレートを一括で切り替える**(ユーザー指示。
  index列のような行ごとの選択にはしない)。定義名`LiveSwap`=`IF(Broker="Totan","swap_totan","swap")`、
  `LiveSwapLCH`=`IF(Broker="Totan","swap_lch_totan","swap_lch")`を、各シートの`LiveQuotes`/`LiveSnapshot`/`LiveInfo`の第1引数に渡す。
  UDFは変えない(四則演算・分岐はExcelに任せる)。カーブのキャッシュはレートの内容で見るので、切り替えれば引き直される。
- **東短はリアルタイムだけ**(ユーザー指示)。評価日が過去日のカーブ・HistChartのヒストリカル・ASWはBrokerに関係なくDB(Tradition)を使う。
- SwapAnalysis・BondOption・PositionSheetは切り替えない(`"swap"`のまま)。ブック同士を参照させない。
- 手動計算なので、切り替えた後はシートごとにShift+F9(B1のLive/Fixedと同じ)。B1が`Fixed`のシートは、そのブローカーで最後に
  Liveで読んだ値を返し、一度も読んでいなければエラーになる(黙って他のブローカーの値を使わない)。
- 東短に21Mは無い(Mainの`MarketInfo`に「値なし・未使用: 21m」と出る)。BID/ASKの幅は常に1bpで、使うのはミッド。

## pricingシート

列A-V: `flag / risk_flag / remarks / notional / delta(target) / adj_notional / start / tenor / end / fix rate /
fix freq / fix dcf / index / float freq / roll conv / target fixrate / spred / fly / pay/rec / PV / delta(bump) /
delta(annuity)`。W列はmessage(その行を計算できなかった理由)。1行目は取得モード、2行目はシート見出し、3行目が表の見出し、
取引は4〜103行目の100行。入力セル(A〜E・G〜O・S列)は薄黄色。spred・flyは1行上/下のtarget fixrateのExcel数式。
float dcfの列は持たない(TONAの変動脚の日数計算はQuantLibが指標`ql.Tonar`に固定し、入力しても使われない)。

- **表全体を1回のUDF`PricingTable`で計算する**。行ごとのUDFを並べると再計算がUDFのセル数で決まり、Liveでは
  Shift+F9のたびに約120セル(約20秒)かかっていた。Python側は100行で約1.3秒。
  - 出力の列(F・P・T・U・V・W)は入力の列の間にあるので、右の計算領域AQ3
    `=PricingTable(HSTACK($A$3:$E$103,$G$3:$O$103,$S$3:$S$103),TAKE($AK$4#,,3),評価日,TAKE($BC$4#,,3))`
    (AQ:AV)にスピルさせ、左の列は`=AQ4`のように参照するだけ(ユーザーの案)。入力の列だけを`HSTACK`で渡す
    (出力の列を含む範囲を渡すと循環参照。`CHOOSECOLS`で除いても範囲全体を参照するので循環する)。列は見出し名で探す。
  - 計算はflag=1の行だけ(ユーザー指示)。PV・delta系はnotionalのある行、adj_notionalはdelta(target)のある行だけ。
  - 1行用のUDF(`FairRate`等)も同じ部品の組み合わせとして残す(HistChartのヘッジの縮尺が`SwapAnnuityDelta`を使う)。
  - レート系とデルタ系にUDFを分けない(手動計算では全部が終わってから表示されるので、呼び出しが増えるだけ。ユーザー了承)。
- **delta(annuity)・adj_notionalはアニュイティ(想定元本1あたり、正)にnotionalを掛けて出す**。delta(bump)・RiskGridは
  カーブのバンプ。フォワード物では両者が一致しない(どちらに揃えるかは[roadmap.md](roadmap.md)の15)。
- **index列(M、`JSCC`/`LCH`、空欄=JSCC)で行ごとにカーブを選ぶ**(ユーザー指示)。`LCH`の行はTraditionのLCHの
  アウトライト`JPY{テナー}OISL=TRDT`(フィードの`"swap_lch"`、AX4→BC4。Brokerが東短なら`"swap_lch_totan"`)で引いたカーブで計算する。
  - LCHもJSCCも変動側は同じTONAで規約も同じなので、違うのは元のレートだけ。指標・スワップの組み方・デルタの計算は
    そのまま使い、同じ`bootstrap_curve`でもう1本引く(指標を引数に取る一般化はしない)。ベーシス(`JPYTOLJ{テナー}S=TKFX`)から
    足し算で作る案もあったが、アウトライトが取れるのでそれを使う(ユーザーの希望)。
  - **ライブ(評価日が今日)だけ**。LCHのレートの履歴が無いので、過去の評価日でLCHの行を選ぶとその行をエラーにする
    (JSCCのDBのレートで黙って代用しない)。ヒストリカルは[roadmap.md](roadmap.md)の2。
  - 1行用のUDFは渡された範囲のカーブで計算するので、過去の評価日ではDBのJSCCのレートになる(シートではPricingTableを使う)。
- **start/endの入力**:
  - 日付、IMMコード(H/M/U/Z+2桁年、例 `M27`。第3水曜)。
  - startだけ、テナー(`10y`): 評価日のスポット日+tenor(休日調整前)を起算日にする(`10y`+tenor`20y`で10y20y)。
    `ql.MakeOIS`のフォワードスタート(`spotDate + forwardStart`)と同じ扱いにして、QuantLibの標準と食い違わないようにするため。
  - startだけ、営業日数(`2bd`・`10bd`): 評価日(休日なら翌営業日)からN営業日後(`ql.Japan`)。スポット日と同じ数え方なので
    `2bd`は常にスポット日と一致する。テナーの`2d`(暦日)とは別。
- **roll conv列(O)の既定は数式**で、startとendが両方IMMコードなら`IMM`、それ以外は`STD`。UDFは値にそのまま従う
  (UDF側で自動判定すると、STDで計算したいときに選べなくなる)。startだけIMMコード+tenorのとき、満期をIMM日へ寄せない
  (ユーザー判断)。上書きを戻すときは上下の行から数式をコピーする。
- **リスクの合算`RiskGrid`(Y3)**: `=RiskGrid(pricing!$A$3:$S$103, TAKE($AK$4#,,3), 評価日, TAKE($BC$4#,,3))`が、risk_flag=1の
  全行のテナー別デルタ(1bpあたり、百万円)の合計をMainのテナーの並びでTicker/Deltaの2列にスピルする。AC3が全テナーの合計、
  AC4が参考としてdelta(bump)のrisk_flag=1の合計。数式の先頭に`@`を付けない(先頭の1セルしか出ない)。
  - 行ごとのグリッドはやめて全行の合算を1つのUDFで出す(ユーザー指示)。マクロではなくUDFなのは、入力が変わったときだけ
    再計算され古い値が残らないため。重さはカーブの共有で解決する(副作用が要るときに改めてマクロを検討する)。
  - LCHの行はLCHのカーブのテナーを動かし、同じTickerの行に足す(同じTONAのレートの感応度なので清算機関で分けない)。
  - バンプするのはカーブに実際に使っているテナーだけ(ユーザー指示)。
  - 計算時間(33テナー×100行で約9〜10秒)は[roadmap.md](roadmap.md)の4。

## ヒストリカル(HistChartシートのUDF)

データは日次カーブDB。DBが無いとエラー。

- **任意のコンベンションのヒストリカルはFIXED/ROLLINGをmode引数で選ぶ**(ユーザー指示: 「同じ取引の過去の値」と
  「一定期間の推移」はどちらも要る)。入力の種類(日付・IMMコード・空欄・tenor・営業日数)に関係なく、まず基準日
  (通常は評価日)の取引の起算日・満期日の2つの日付に直す(空欄のstartは基準日のスポット日)。
  - FIXED: その2つの日付の取引を過去の各日で評価する。満期後の日付は系列から外す。
  - ROLLING: 基準日の取引の「スポット日→起算日」「起算日→満期日」の**営業日数**(`ql.Japan`、休日調整後の日付で数える)を
    保って、各過去日のスポット日へ平行移動する(ユーザー判断)。土日祝の並びでスワップの長さが変わり、それが変化に混ざると
    分析の支障になるため。暦日数は一定にならない(Z27→Z28で249営業日一定、暦日は371〜381日)ので、dcfで測る期間の長さと
    パーレートには日ごとの差が残る(1年物で最大約3%の期間差。暦日固定と比べたうえで営業日固定を選んだ)。
    start空欄+tenorの標準アウトライトも同じ扱いで、満期日が「スポット日+tenor」から数日ずれる日がある(ユーザー了承)。
  - **roll convがIMMの脚をROLLINGで計算するとエラー**(移動後の日付は第3水曜でなくなる。ユーザー指示)。
  - 過去日Dの計算にはD当日以降のTONA実績を渡さない(リアルタイムと同じ扱い)。
- **カーブ・フライは脚をまとめて計算する`HistoricalRates`(最大3脚)+Excelの四則演算**。日付ごとにカーブを1回だけ復元して
  全脚を評価する。期間内のDBの全日付を必ず返し、計算できない日は空欄にするので、脚・系列の間で日付が揃い突き合わせが要らない。
- `HistoricalRate(start, tenor, end, fix_freq, fix_dcf, float_freq, roll_conv, mode, [基準日], [from], [to])`は1本だけの確認用
  (Date/Rate/Start/Endの4列)。
- **最新日(今日)はDBの引け値ではなくライブのレートで計算する**(ユーザー指示)。`HistoricalRates`・`HedgeHistoricalRates`の
  最後の引数`real_time_range`にカーブ用の範囲を渡すと、基準日が今日で今日が営業日のとき、pricingと同じカーブで今日の行を加える。
  基準日=取引日のときは今の気配を見たいため。今日が休日なら加えない(直前の営業日と同じ値の点が増えるだけ)。日付軸(脚なしの呼び出し)
  にも同じ範囲を渡して並びを揃える。
- **昨日までの結果はUDFが覚え、今日の1行だけを毎回計算する**(ユーザー判断)。キーは引数・今日の日付・DB(パス・更新時刻・サイズ)で
  最大8件(覚えるのは数値だけで1MB未満)。Liveでレートが変わるたびの全期間の計算(約24秒)が約0.2秒になる。
  - 採らなかった案: 昨日までと今日を別のセルに分けてVSTACKでつなぐ。昨日までのセルをライブにつながらない場所に置く必要があり、
    日付が変わった・DBが変わったときに自動で再計算されず古い値が黙って残る。
  - 覚えている結果を使う呼び出しは評価日を切り替えないので、pricingのカーブを手放さない(`release_curves`は全期間を計算するときだけ)。
  - 系列の条件を変えたときの速さは[roadmap.md](roadmap.md)の7。

## HistChartシート

pricingシートの行を指定して、最大3系列(アウトライト/カーブ/フライ)のヒストリカルを1枚のグラフで比較する。

- 共通設定: B4=基準日(Mainの評価日に連動、空欄=今日)、B5/B6=計算期間、B7=単位(カーブ/フライが1本でもあれば全系列bp、
  アウトライトだけなら%)、B8=倍率。**軸は1本**(系列ごとの第2軸の動的な切り替えにはVBAが要る)。
- 系列表(10〜12行): 表示(○/×)・行A/B/C(pricingのExcel行番号)・モード(系列ごと)・系列名。種類は行番号の数で決まる
  (1つ=アウトライト、2つ=カーブ`B−A`、3つ=フライ`2B−A−C`。pricingのspred/flyと同じ符号)。
- 脚の確認表(16〜24行): `LET(v,INDEX(pricing!列,行),IF(v="","",v))`(空セルをINDEXで参照すると0になるため)。
  表示×の系列は脚を空にし、UDFは計算しない。
- 計算領域(29行〜): 系列ごとに`=LET(r,HistoricalRates(脚3行×8列, モード, B4, B5, B6, TAKE($AO$3#,,3)),…SORTBY(…,-1))`。
  **新しい日付を上にする並べ替えはExcelの`SORTBY`**(ユーザー指示。UDFは昇順で返す)。P列が日付軸、Q〜S列が系列値
  (計算できない日は`#N/A`で線を出さない)。
- グラフ`HistChart_Chart`の系列は名前`HistChart_Date`/`HistChart_S1〜S3`(スピル行数に追随)。未使用の系列は凡例に空の項目が
  残る(VBAなしでは消せない)。

### ヘッジ(4本目の系列)

フローの取引をpricingで計算し、RiskGridを見てユーザーがヘッジ(スポットのアウトライト・カーブ・フライの組み合わせ)を決める。
そのヘッジの過去の値動きをフローのヒストリカルに重ね、ヘッジで消えていないリスクがどう動いたかを見る。

- **ヘッジの線は仮想金利`V(t) = Σ d_i × r_i(t)`**。d_iはテナーiのバンプデルタ(RiskGridと同じ定義の百万円/bp、手入力)、
  r_iはテナーiのスポット金利。ΔVはそのグリッドの一次の損益そのもので、組み合わせにレートの概念が無くても1本の線になる。
  一次近似で十分(100bp超動いた1年間でも完全ヘッジとの乖離は1〜2bp)。
- **フローの線は既存の`HistoricalRates`のまま**(フォワード・カーブ・フライには正確な定義がある。ユーザー判断)。
- **縮尺はフローのアニュイティデルタで割って合わせる**(ユーザー判断)。at-marketでは「バケットデルタ_i = アニュイティデルタ ×
  ∂(パーレート)/∂r_i」なので、アニュイティで割るとヘッジの1bpがフローのパーレートの1bpに対応する。バンプデルタで割ると
  フォワード物で縮尺がずれる(10y10yで完全ヘッジの最大の乖離がアニュイティ2.2bp/バンプ11.2bp)。
- **入力はフローと同じ符号(ヘッジで再現するリスク)**。符号付きのdelta(annuity)(PAY=+、REC=−)で割り、
  `H = 系列1(スタート日) + ΔV × 倍率 ÷ 縮尺`。実際に執行するヘッジ取引は逆の符号。
- **縮尺・水準は系列1に揃える**。縮尺の既定はアウトライト=行A、カーブ=行B、フライ=行Bの1/2のdelta(annuity)
  (DV01ニュートラルなら、カーブは脚1本分、フライは胴体R・両ウィング−R/2で損益=R/2×Δフライ)。系列1の行の取引条件・notional・
  pay/recから`SwapAnnuityDelta`で計算する(チャート用の行にはPV・delta系の数式が無いことがあるため。pay/rec空欄はPAY)。
  手で上書きできる。水準は系列1とヘッジの両方に値がある最も古い日で合わせる。
- **モードは選べる。既定は系列1のモード**。FIXEDのヘッジは基準日に取引したスポットのスワップを日付固定で評価する。
- **欠損日は穴埋めしない**。その日のカーブに使っていないテナー(DBの`used_tenors`)は空欄にし、入力したテナーのどれかが
  空欄の日は線を出さない。使えるのはスポットのテナー(1W〜40Y)だけ。
- 配置: AD4〜AE8が設定(表示・モード・縮尺・スタート日・状態のメッセージ)、AD9〜AG43が縦のグリッド(Ticker・フロー
  (`pricing!$Y$3#`)・ヘッジ入力AF10:AF42・残差・合計)。U30で0以外のテナーだけを`FILTER`して`HedgeHistoricalRates`を呼び、
  `BYROW`でΣを計算する。T30(名前`HistChart_H`)がグラフ用。残差の線の見せ方は未決定。

## ASWシート

JGB銘柄別のASWスプレッド(DBの`asw_quotes`、朝バッチがNeonから取得)のヒストリカルを、最大3系列で1枚のグラフに重ねる。

- **HistChartと同じ「脚A/B/C→アウトライト・カーブ・フライ」の構成**(ユーザー要望)で、pricingの行番号の代わりに銘柄を指定する。
  カーブ・フライの計算はExcelの数式で、UDFは各脚のASWだけを返す。
- **銘柄は「接頭辞+回号」(`JB380`)の手入力で、9桁のbond_codeへの読み替えはExcelの数式**(ユーザー判断)。bond_codeは
  「回号5桁+種別4桁」で、接頭辞→種別の対応表(H15:J24、JN=0042・JS=0045・JB=0067・JL=0069・JX=0068・JU=0054。行を足せる)を
  引く。文字列の加工と表引きだけなのでUDFにしない。満期年などで選べるようにするには銘柄マスタの取得が要る(要るようになったら相談)。
- **値は%単位、表示はbp(倍率100)**。スプレッドなのでアウトライトもbp。
- **PA/SAはシート全体で1つ**(B3)。系列ごとにすると、カーブ・フライで違うコンベンションの値を混ぜられてしまう。
- **ROLLING/FIXEDの区別もライブのレートの参照も無い**(銘柄ごとに日付→値が決まっているだけ。ASWは朝バッチの取り込みだけで、
  当日分はまだ入っていないことがある)。
- **複数銘柄の日付は和集合で揃え、値の無い脚だけ空欄**(日付ごとに「ある銘柄だけ」に絞ると脚の対応がずれる)。
  系列ごとに日付が違うので、P列で3系列の日付の和集合(`SORT(UNIQUE(VSTACK(…)),,-1)`)を作り、Q〜S列で`XLOOKUP`する。
- DBは`historical_store.asw_quotes_for(bond_code, from, to)`で銘柄・期間を絞って読む(`bond_code`にインデックス。
  テーブルは90万行規模で、全件読みは重い)。
- グラフ`ASW_Chart`の系列は名前`ASW_Date`/`ASW_S1〜S3`。

## ASWPricerシート

アセットスワップのプライサー。1行=1銘柄の表で、ASW = 現物の複利 − MMS を出す。

- **定義はNeonの`ASW_data`と同じ: ASW = 現物の複利 − MMS**(ユーザー指示)。書き込み元(ユーザーの別プロジェクトの
  `QuantLibHelper.calculate_mms`)を読んで規約を合わせた: MMSは起算日=スポット(T+2)、満期日=償還日(休日調整)、償還日から
  遡る日程(Backward)、固定側Act/365でPA/SA、変動側も同じ頻度。現物側はNeonではJSDAの平均値の複利だが、こちらはJSDAを
  取れないので、引け値(JCL)の単利+スライド→単価→半年複利(うるう日除き/365、小数3桁切り捨て)。2026-09-25のDBで、PAとSAの差が
  Neonと0.01bpの範囲で一致した。
- **スワップの側はpricingのパーレートと同じ経路・同じカーブ**。規約は行ごとに選べる(ユーザー要望)。roll convに`BWD`
  (`ql.DateGeneration.Backward`)を追加した(STD/EOM/IMMはForwardで、MMSの支払日を利払日にそろえられない)。
- **ASW(bp)の引き算はシートの式**(W=(S−V)×100)。
- **par-par ASWは参考値**(ユーザー要望)。`ql.AssetSwap`(parAssetSwap=True)のfairSpread。TONAでは変動側の日程を渡す必要がある。
  `bond.schedule`(NullCalendar)のままだと受渡日が土曜になるので日本のカレンダーで組んだ債券を使う。起算日が評価日より前の行は
  計算しない(変動側の実績を登録しないため)。
- **delta→想定元本はpricing表と同じ`notional_for_delta`をMMSに適用**。PV・デルタは今は出さない(ユーザー判断)。
- **約定したASWからMMSのレートとノーショナルを出す(AA:AD、2026-11-04)**。入力は現物のノーショナル(億円)と約定ASW(bp)、スライドは既存のC列。
  MMSのレート = 複利 − 約定ASW/100、MMSのノーショナル = 現物のノーショナル × hedge ratio(どちらもシートの式。UDFの入力範囲は変えない)。
  - 現物のリスクは**その銘柄の複利の修正デュレーション×利含み単価**(`bond bpv`、複利1bpあたり。カーブはバンプしない。ユーザー判断)。ASWを固定するとMMSの1bpは複利の1bpなので、
    ASWの定義(複利 − MMS)と整合する。単利の1bp・カーブの平行シフト+Zスプレッド固定は採らない。
  - スワップ側は`adj_notional`と同じアニュイティデルタ。`hedge ratio` = `bond bpv`に合うMMSの想定元本/現物の額面で、PAY/RECによらず正の数。
    向きはK列(現物を買うならPAY)。現物はT+1受渡時点の価値、スワップのアニュイティは評価日時点で、その差(1日分の割引)は無視する。端数は丸めない。
- 配置: 入力A〜K(4〜53行。flag・bond・slide(bp)・yield override・delta・start・fix freq・fix dcf・float freq・roll conv(既定`BWD`)・
  pay/rec)、表示L〜Z、約定AA〜AD、計算AQ3`=AswPricingTable($A$3:$K$53,TAKE($AK$4#,,3),評価日)`(AF:BFは折りたたみ)。
- 先物ASW・リファレンスの列は[roadmap.md](roadmap.md)の11。
