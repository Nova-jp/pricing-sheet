# 全体構成

## 目的

任意のコンベンションのJPY OIS(TONA)スワップについて、
(a) リアルタイムのパーレート/PV/デルタ と (b) ヒストリカルなアウトライト推移を
1つのExcelブック(`PricingSheet.xlsm`)上で計算・表示する。

## データフロー

```
[Excel: historical_data.xlsx(作業用。リポジトリにはtemplates/の雛形のみ)]
  LSEG Workspace(RDP.HistoricalPricing)から取得し、保存する
  - Midシート: 日付×テナーのミッド(JP{テナー}ONI=TRDT、RealTimeシートと同じ系列)
  - Fixingシート: TONA実績(JPONMU=RR) ────────▶ 過去起算用のfixing_range引数(pricingシート)
        │
        ▼  朝に手動実行: python -m swap_pricing.curve_batch
[朝バッチ swap_pricing/curve_batch.py]  設定: historical_curve.toml(引き方・使うテナー)
  ① Mid/Fixingを読み、DBのquotes/fixingsに取り込む(過去に取り込んだ日も残る)
  ② 日付ごとに選択テナー(O/Nはその日のTONA実績)でカーブを構築 → 暦日ごとのDFを保存
     (欠損テナーは外して構築、日本の非営業日は作らない、入力が変わった日だけ作り直す)
        │
        ▼
[data/historical.db(ローカルのSQLite。LSEG由来なのでコミットしない)]
        │
        ▼  日中はDBを読むだけ(ブートストラップしない)
[Excel: PricingSheet.xlsm]
  RealTimeシート(LSEGアドインが手動更新するTicker/Value) ─▶ FairRate等のUDF(その場でカーブ構築)
  HistoricalRate / HistoricalOutright ─────────────────────▶ DBのカーブで評価
```

データの供給元はLSEG Workspaceだけ(クラウドは使わない)。

## 計算エンジン(`swap_pricing/`)

プライシング計算(日付・スケジュール生成を含む)は自前実装を持たず、全て
QuantLib純正のAPI(`ql.MakeOIS`/`ql.OISRateHelper`/`ql.PeriodParser`/
`ql.Date.from_date`等)に委ねている。自前で持つのは「業務ルール(テナー表記・
roll_conv文字列等)→QuantLib引数」の対応表と、Excel境界の型変換のみ
(経緯は[decisions.md](decisions.md)参照)。

| ファイル | 役割 |
|---|---|
| `conventions.py` | 業務上の表記→QuantLibの対応表を集約: `ql.Japan()`カレンダー、dcf文字列→DayCounter、freq、roll conv→生成ルール、IMMコード(M27)→日付、テナー表記の読み替え(12M→1Y)。自前の日付計算ロジックは持たず、呼び出し側がQuantLibのAPIを直接使う |
| `curve.py` | スポットレート辞書→`ql.PiecewiseConvexMonotoneForward`によるカーブ構築。O/Nは`DepositRateHelper`、それ以外は`OISRateHelper`(`rule=ql.DateGeneration.Forward`・`endOfMonth=False`で構築) |
| `swap_pricer.py` | `ql.MakeOIS`(→`ql.OvernightIndexedSwap`)によるPV・パーレート計算。過去起算のスワップは引数で受け取ったTONA実績フィキシングを登録(不足時はエラー)。評価日は呼び出しごとに設定し直す。`swap_dates`はカーブなしで同じビルダーのスケジュールから起算日・満期日(休日調整後)を返す(ヒストリカルのROLLINGで営業日数を数えるのに使う) |
| `market.py` | 評価日のマーケット(今日=RealTime、過去日=ヒストリカルDBのその日のレート)と、全行・全UDFで共有するカーブ一式(基準カーブ、全テナー+1bp、テナー別+1bp。必要になったときに1回だけ作り、バンプはカーブに使っているテナーだけ) |
| `risk.py` | 並行/バケットデルタ(`market`のカーブ一式を使い、ここではブートストラップしない)、notional逆算 |
| `historical_pricer.py` | 保存済みの日次カーブから、任意コンベンションのヒストリカル・パーレート系列を計算(ブートストラップしない=カーブの引き方を知らない)。モードはFIXED(同じ日付の取引をそのまま過去日で評価)とROLLING(基準日の取引の「スポット日→起算日」「起算日→満期日」の営業日数を保って各過去日へ平行移動。IMMコードも動かす。roll conv=IMMはエラー。暦日数=dcfの期間は一定にならない)。入力は種類に関係なく基準日の取引の2つの日付として読み、空欄のstartは基準日のスポット日。過去日Dの計算にはD当日以降のTONA実績を使わない。満期までの範囲だけカーブを読み出す |
| `curve_store.py` | ヒストリカル日次カーブDB(`data/historical.db`)の読み書き。カーブは評価日から暦日1日刻み・50年分のDFとして保存し、`ql.DiscountCurve`で復元する(引き方に依存しない形式。理由は[decisions.md](decisions.md))。生レート・TONA実績も保存 |
| `curve_batch.py` | 日次カーブDBを作るバッチ(手動実行)。`historical_data.xlsx`を読み、`historical_curve.toml`の設定(引き方・使うテナー)でカーブを構築して保存 |
| `meeting_curve.py` | BOJシート専用: O/N+会合スワップ(RD・M1〜)から、会合期間ごとにフォワード一定のカーブを組む。日付はLSEGの値をそのまま使い、決定日→翌営業日の夜は直前の期間(決定前)のフォワード。節目のDFを順に決めて`ql.DiscountCurve`に入れる(OISRateHelperを使わない理由は[decisions.md](decisions.md))。最後の会合期間より先は外挿しない |
| `paths.py` | リポジトリ内パス解決(クローン先の絶対パスに依存しない) |
| `udf.py` | **Excelから直接呼べるUDF定義。ここが計算エンジンとExcelの唯一の接点** |

`tests/test_past_start.py`: 過去起算スワップ(合成フィキシング)と評価日の扱いの
リグレッションテスト(`python -m unittest tests.test_past_start -v`)。

`tests/test_historical.py`: ヒストリカル計算(FIXED/ROLLINGの日付の決め方、先読みしない
フィキシングの扱い、満期後の除外)のリグレッションテスト(`python -m unittest tests.test_historical -v`)。

`tests/test_curve_store.py` / `tests/test_curve_batch.py`: 日次カーブDBの保存・復元の精度と、
バッチ(テナー選択・欠損テナー・O/N・祝日除外・差分更新)のリグレッションテスト。
LSEGデータの代わりに合成したブックを使う。

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

- `PricingSheet.py`(ワークブックと同名)は`from swap_pricing.udf import *`のみ。
  xlwingsのExcelアドイン(Windows)は「Import Functions」実行時に
  **ワークブックと同名の.pyファイル**からUDFを探すため、実体を
  `swap_pricing/udf.py`に置いたうえで、ここに再エクスポートしている。
- ブックの計算方法は「手動」。UDFは非volatileのままでよく、F9(再計算)を
  押すたびに最新のセル値を使って再評価される。VBAマクロ・ボタンは一切不要。
- UDFにするのは**QuantLibが必要な計算だけ**(パーレート、PV、デルタ、
  notional逆算、リスクの合算(RiskGrid)、ヒストリカル)。カーブ(スプレッド)や
  フライ、合計といった四則演算はExcel自身の数式(`B1#`等のスピル参照)
  に任せる。「エクセル上は平易な数式、高度な計算だけUDF化」という方針。

## Excelブック構成(`PricingSheet.xlsm`)

シート: `Main` / `RealTime` / `HistChart` / `BOJ` / `pricing` / `Historical` / `Calc` / `_xlwings.conf`

### RealTimeシート

列構成(2行目以降):

| 列 | 内容 |
|---|---|
| A | 空。ただし行36-45(会合スワップ)は`RD`/1-9(D〜F列の数式が`"JPBOJ"&A&"ONI=TRDT"`でRICを作る) |
| B | **スポット採否フラグ**。`1`または空欄=カーブ構築に使う、`0`=使わない。行2-35に設定。会合スワップの行36-45は`0` |
| C | Ticker(テナーラベル。LSEGアドインが参照) |
| D | Value(LSEG/RTDのライブ数式が更新するミッドレート=(BID+ASK)/2) |
| E/F | 会合スワップの行だけ: 起算日/満期日(`TR(RIC,"GV1_DATE")`/`"GV2_DATE"`) |

テナー表記は全て大文字。Reutersフィードの都合で **1Y/2Y/3Yは`12M`/`24M`/`36M`表記**
(`1Y`/`2Y`/`3Y`表記とは`conventions.py`の`TENOR_ALIASES`で吸収)。
行2のO/Nは`JP1DONI=TRDT`(Traditionの1日物OIS、チェーン`JPONI=TRDT`の先頭)のミッドで、
メインのカーブにも使う(過去の評価日・ヒストリカルのO/Nはその日のTONA実績)。
行36-45は会合スワップ(TraditionのRD・M1〜M9、チェーン`JPONIBOJ=TRDT`)。RD=スポット日→会合1の
決定日、M{n}=会合nの決定日の翌営業日→会合n+1の決定日。メインのカーブには使わず
(`curve.EXCLUDED_MEETING_TENORS`で`skipped_tenors`計上)、BOJシートだけが使う。

UDFに渡す標準範囲は `RealTime!$B$2:$D$45`(フラグ/Ticker/Value)。
過去起算のスワップでは、各プライシングUDFの最後の省略可能引数`fixing_range`に
`[historical_data.xlsx]Fixing!$A$5#`(日付/TONAの2列)を渡す(同ブックを開いておくこと)。
「どのスポットレートをカーブに使うか」の判定は`swap_pricing/udf.py`の
`_rates_from_range`(過去の評価日では`_market`が同じフラグ)で行い、フラグ0のテナーは
カーブにもデルタのバンプにも入らない(RiskGridでは空欄になる)。
15M/18M/21Mも普通のスポットとして扱う(旧`include_odd_tenors`は廃止、
不要ならB列を0にする)。

### pricingシート

列A-W: `flag / risk_flag / remarks / notional / delta(target) / adj_notional /
start / tenor / end / fix rate / fix freq / fix dcf / index / float freq /
float dcf / roll conv / target fixrate / spred / fly / pay/rec / PV /
delta(bump) / delta(annuity)`。target fixrate・PV・delta・adj_notionalは
UDF、spred・flyはExcel数式(1行上/下のtarget fixrateの単純な四則演算)。
target fixrate(Q列)は`IF(flag=1, FairRate(...), "")`で、A列のflagが1の行だけ計算する。

start/end列には日付のほか、IMMコード(月記号H/M/U/Z+2桁年、例: `M27`)も入力できる
(`swap_pricing/conventions.py`の`imm_code_to_date`で第3水曜に変換)。start列にはテナー
(例: `10y`)も入力でき、評価日のスポット日+tenor(休日調整前)を起算日にする
(フォワードスタート。`10y`+tenor`20y`で10y20y。`conventions.forward_start_date`)。roll conv列(P列)は
既定で数式になっており、startとendが両方IMMコードなら`IMM`、それ以外は`STD`を返す。
STD等で計算したい行はドロップダウンで上書きする(UDFはroll convの値にそのまま従う)。
上書きを元に戻すときは、上下の行からP列の数式をコピーする。

評価日の引数はすべて名前`評価日`(Mainシート)を参照する(`TODAY()`は使わない)。

列Z以降: リスクの合算。Z1の`=RiskGrid(pricing!$A$1:$T$201, RealTime!$B$2:$D$45, 評価日,
フィキシング範囲)`が、risk_flag=1の全行のテナー別デルタ(+1bp、百万円)の合計を
RealTimeのテナー行の並びでTicker/Deltaの2列にスピルする(カーブに使っていないテナーは空欄)。
AD1が全テナーの合計、AD2が参考として行ごとのdelta(V列、全テナー+1bp)のrisk_flag=1の合計。
pricing表の列は見出し名で探すので、列を移動しても壊れない(見出し行=1行目から渡すこと)。
数式の先頭に`@`を付けないこと(付くと先頭の1セルしか表示されない)。

### Mainシート

- B3: 評価日(名前`評価日`、空欄=今日)。全UDFの評価日はこのセルを参照する。
  今日ならRealTimeのライブのレート、過去日ならヒストリカルDBのその日のレート
  (テナーはRealTimeのB列フラグで選ぶ。O/Nはその日のTONA実績)でカーブを組む。
  過去起算のTONA実績は評価日より前の日だけを使う。HistChartの基準日もこのセルに連動する。
- B4: `=MarketInfo(RealTime!$B$2:$D$45, 評価日)`(カーブの元データとテナー数)。
- `TODAY()`をやめたので、F9だけでは値は変わらない(レート・入力・評価日が変わったセルだけ
  再計算される)。日付をまたいでもRealTimeのレートが変われば再計算される。

### Historicalシート

2Y/5Y/10Yの`HistoricalOutright`UDF(B1/F1/J1、Date/Rateの2列スピル配列、1行目は見出し)+
2s10sカーブ(N2: 10Y−2Y)・2s5s10sフライ(R2: 2×5Y−2Y−10Y)をExcel数式(スピル参照
`B1#`等、`XLOOKUP`で日付を突き合わせ)で計算。単位はpricingシートのspred/flyと同じ%。
データは日次カーブDB(`python -m swap_pricing.curve_batch`で作成)。DBが無いとエラーになる。

任意のコンベンションは`HistoricalRate(start, tenor, end, fix_freq, fix_dcf, float_freq, float_dcf,
roll_conv, mode, [基準日], [from], [to])`(Date/Rate/Start/Endの4列スピル)。modeは`FIXED`/`ROLLING`。
基準日には通常`評価日`(Mainシート、空欄=今日)を渡す(`TODAY()`はF9のたびに再計算されるため使わない)。
`HistoricalOutright`は`HistoricalRate`のROLLING・start空欄と同じ(既存数式のために残している)。
`HistoricalRates`(HistChart用、最大3脚)は省略可能な最後の引数`real_time_range`にRealTimeの範囲を渡すと、
基準日が今日で営業日なら今日をRealTimeのライブのレートで計算した最新日として加える(HistChart参照)。
ROLLINGは営業日数を固定するので、満期日は「スポット日+tenor」から数日ずれる日があり、
暦日の長さ(act/365fixed等のdcfで測る期間)も日ごとに変わる(理由と影響は[decisions.md](decisions.md))。
roll convがIMMの脚をROLLINGで計算するとエラーになる(FIXEDで見るか、STDにする)。

### HistChartシート

pricingシートの行を指定して、最大3系列(アウトライト/カーブ/フライ)のヒストリカルを
1枚のグラフで比較するシート。

- 共通設定: B3=ROLLINGの基準日(`=IF(評価日="","",評価日)`でMainシートの評価日に連動、
  空欄=今日)、B4/B5=計算期間(任意、薄黄色)。B6=単位(表示中の系列にカーブ/フライが1本でもあれば
  全系列bp、アウトライトだけなら%。軸は1本。VBAなしでは系列ごとの第2軸を動的に切り替え
  られないため)、B7=倍率(bpなら100)。
- 系列表(9〜11行): 表示(○/×)・行A・行B・行C(pricingのExcel行番号)・モード
  (ROLLING/FIXED、系列ごと)・系列名(任意)。種類は行番号の数で自動判定
  (1つ=アウトライト、2つ=カーブ `行B−行A`、3つ=フライ `2×行B−行A−行C`。pricingシートの
  spred/flyと同じ符号)。凡例名は自動(系列名を入れればそれを使う)。
- 脚の確認表(15〜23行): 3系列×3脚のremarks/start/…/roll convを
  `LET(v,INDEX(pricing!列,行),IF(v="","",v))`で表示(空セルをINDEXで参照すると0になるため
  空文字に置き換える)。表示×の系列は脚を空にし、UDFは計算しない。
- 計算領域(28行〜): 系列ごとに
  `=LET(r,HistoricalRates(脚3行×8列, モード, B3, B4, B5, RealTime!$B$2:$D$45),d,DROP(r,1),
  VSTACK(TAKE(r,1),SORTBY(d,INDEX(d,,1),-1)))`(A/F/K列、Date+脚A/B/Cのスピル)。
  **新しい日付(基準日に近い方)が上**になるよう、Excelの`SORTBY`で降順に並べ替えている
  (UDFは昇順で返す)。P列はグラフ用の日付軸(脚なしで呼ぶと日付だけが返る。同じ範囲を渡して
  `SORT`で降順)。Q〜S列が系列値(種類に応じた四則演算×倍率。計算できない日は`#N/A`で
  線を出さない)。3系列とも同じ日付の並びなので、XLOOKUPによる日付の突き合わせは不要。
- 最新日: 基準日が今日(B3が空欄、または評価日=今日)で今日が営業日なら、今日の行を
  RealTimeのライブのレートで計算する(pricingシートの`FairRate`と同じカーブ。DBに今日の引け値が
  あってもライブを優先)。それ以外の日はDBの引け値。RealTimeを参照するので、RealTimeの値が
  変わるとHistChartの全系列が再計算される(F9で1系列約1.2秒)。
- グラフ`HistChart_Chart`(折れ線・日付軸・3系列固定)の系列は名前`HistChart_Date`/
  `HistChart_S1〜S3`(日付軸のスピル行数に合わせた参照)で、行数が変わっても追随する。
  未使用の系列は凡例に空の項目が残る(VBAなしでは消せない)。

### BOJシート

BOJ会合スワップ(RD・M1〜M9)と1年以下のスポット(1W〜12M)のずれを、2方向で表示する(リアルタイムのみ。
ヒストリカルは未対応)。行36-45のRealTimeを参照する。

- 上部: B2=評価日(空欄なら「今日(RealTime)」)、B3=O/N(RealTime!D2)。
- **A(7〜16行)スポット→会合期間**: 各会合スワップの起算日・満期日(RealTime E/F)で、pricingと同じ
  メインのカーブの`FairRate(起算日,"",満期日,"PA","act/365fixed","PA","act/365fixed","STD",…)`を計算。
  G=ずれ(bp)=会合スワップ−スポットからのフォワード、H=会合スワップの前の期間からの変化(bp、RDはO/Nとの差)。
- **B(21〜33行)会合スワップ→スポット**: C21の`=BojImpliedRates(A21:A33, RealTime!$B$2:$D$45,
  RealTime!$C$36:$F$45, 評価日)`が、O/N+RD+M1〜M9の階段状のカーブ(`meeting_curve.py`)での
  スポット起算・標準コンベンションのパーレートを1列でスピル。D=ずれ(bp)=スポット−会合スワップのカーブ。
  評価日が今日でなければエラー。値か日付が欠けた会合スワップの行以降は使わない。
  最後の会合期間より先のテナーはその行だけエラーの文字列。
- ずれはミッド同士(売値・買値の幅は考慮しない)。差・変化はExcelの数式。

## 分析ブック(`SwapAnalysis.xlsm`、`swap_analysis/`)

pricingとは切り分けた分析用のブック。現状はスワップとボラ(スワップションATMノーマルvol)の
PCA(主成分で復元した値と実績の差分、主成分ベクトル)。将来はボラのリアライズとインプライドの
軌跡を追加する予定。設計方針はPricingSheetと同じ(VBAなし、手動計算+F9、UDFは配列を返して
スピルに任せる)。Excelの関数では難しい計算(固有値分解・DBの読み出し)はPythonのUDF、
得点・復元(行列の積)・差分・寄与率などはExcelの数式。`SwapAnalysis.py`は
`swap_analysis/udf.py`の再エクスポートだけ(xlwingsのブック名の制約)。

| ファイル | 役割 |
|---|---|
| `swap_analysis/history.py` | DBを日付×銘柄の表で読む。swap=`quotes`(生ミッド、M1〜M8を含む全テナー)+`fixings`(O/N=その日のTONA実績)、vol=`vol_quotes`(満期x原資産)。日本の営業日のみ。DBの更新時刻が変わるまでプロセス内にキャッシュ |
| `swap_analysis/pca.py` | windowの選び方(`estimate`)、比較日の実績(`observe`)、固有値分解。`principal_components`は「サンプル行列→固有値・固有ベクトル」だけの汎用関数 |
| `swap_analysis/udf.py` | `PcaModel`/`PcaEigen`/`PcaWindow`(推定)、`PcaActual`/`PcaActualInfo`(比較日の実績)。最後の引数`dataset`で"swap"/"vol"を選ぶ。推定は引数とDBの更新時刻ごとに1回だけ計算して共有 |

ボラのヒストリカルは、朝のバッチ(`swap_pricing/curve_batch.py`)が`historical_data.xlsx`の
Volシートを読んでDBの`vol_quotes`に取り込む(カーブには使わない)。

PCAの手順(標準的なPCA。理由は[decisions.md](decisions.md)):
- 推定: 推定基準日(windowの最終日。空欄=比較日より前の直近)からさかのぼって、選択した銘柄が
  そろった営業日のサンプルをN個(テナーが欠けた日は除外して、さらに過去へ)。モードは
  レベル(各日の水準)/日次変化(隣り合う営業日の差。間が欠けたペアは使わない)。
  平均μと、標準化ありなら標準偏差σ(なしはσ=1)を求め、z=(サンプル−μ)/σの共分散行列を
  固有値分解する(標準化なし=分散共分散(bp)、あり=相関行列)。主成分の符号は、銘柄の並び上の
  ルジャンドル多項式P_jとの内積が正になる向き(PC1=全体が正、PC2=後ろ側が正、PC3=両端が正)。
- 比較: 比較日(空欄=今日でRealTimeのライブの値、日付=DBのその日の引け値)の実績x
  (日次変化モードは「比較日 − DBの前営業日」。前営業日がなければ直近の日との差で警告)について
  z=(x−μ)/σ、得点=V_kᵀz、復元=μ+σ·V_k·得点、差分=x−復元。推定基準日と独立に選べる。

シート(スワップ: PCA設定/PCA結果/PCA計算、ボラ: VolPCA設定/VolPCA結果/VolPCA計算):
- **設定**: 「主成分ベクトルの推定」(推定基準日・window・モード・標準化)と「比較」(比較日・
  主成分数k)の2ブロック。名前はスワップが`PCA_推定基準日`等、ボラが`VOL_推定基準日`等。
  スワップはテナー表(O/N〜40Y、M1〜M8)の○×で選び、D14の`FILTER`(`PCA_テナー`)が選択テナーの
  並び。既定は1Y〜40Yの13本(O/Nは×。RealTimeのO/Nは使えるが既定では外す)。
  ボラは満期・原資産の○×(既定は全16×12=192点)で、J14の`TOCOL`(`VOL_銘柄`)が格子の全点
  (満期ごとに原資産の順)。
- **計算**: UDFの出力そのもの(A3=`PcaWindow`、A10=`PcaActualInfo`、A16=`PcaEigen`、
  スワップはD3・ボラはK3=`PcaModel`[Label/Mean/Scale/PC1…])。ボラはD〜I列で銘柄ごとの
  実績・平均・スケール・復元・差分も計算する。
- **結果**: 推定windowの情報、比較日の実績の情報(実績の元・変化の基準日・警告)、設定の表示。
  スワップは33行目からテナー別の表(実績・平均・復元・差分(bp)・平均からの乖離(bp))、
  固有値・寄与率・累積寄与率・比較日のスコア、主成分ベクトル(PC1〜PC5)と、グラフ3枚
  (実績・平均・復元/差分/主成分ベクトル。系列は名前`PCA_Tenor`等のスピル参照)。
  ボラは満期×原資産のマトリックス(差分・実績・復元・平均・PC1〜PC3。差分とPCは0を白にした
  カラースケール)を`XLOOKUP`で並べ、上位10本の主成分の表を置く。
- **RealTime**: PricingSheetのRealTimeと同じRIC(A=BOJ会合#、B=Ticker、C=Value。採否フラグ列は
  なし)。O/NはTradition `JP1DONI=TRDT`のBID/ASKミッド(=TONA)。
- **RealTimeVol**: 満期・原資産・銘柄・RIC(`JPYTO{満期}X{原資産}ATM=R`)・値。値は
  `RtGet(…,"MID_IV_NOR")`(ノーマルvol bp。`CF_LAST`/`PRIMACT_1`/`MID_IV`はシフト付きログノーマル%、
  `CF_BID`/`CF_ASK`は両者が混在しているので使わない)。

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
  `JPBOJ{n}ONI=TRDT`(RealTimeシートと同じ系列)。
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
