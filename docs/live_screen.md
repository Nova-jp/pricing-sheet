# LiveScreen.xlsm(スワップとボラのリアルタイムの画面)

スワップ(アウトライト・カーブ・フライ)とスワップションのボラ、長期国債先物オプションの板を、リアルタイムで見るためのブック。
プライシング・分析のブックとは別のExcelのインスタンスで開き、**計算方法を自動**にして使う。
UDFは`engine/live_screen/`(`LiveScreen.py`は再エクスポートだけ)。`common/`だけに依存する。

## 使い方

1. 別のExcelのインスタンスを開く(タスクバーのExcelをAltを押しながら起動し「新しいインスタンスを起動しますか」で
   「はい」、または`excel.exe /x`)。そのExcelで`LiveScreen.xlsm`を開く。最初に開いたブックの設定で、そのExcelは
   自動計算になる(ブックは自動計算で保存してある)。PricingSheet等の手動計算のExcelとは別のプロセスなので、
   計算方法は互いに影響しない。
2. LSEG Workspaceにログインしておく(`RtGet`を使う。新しいインスタンスにもLSEGのアドインが読み込まれているかを確かめる)。
3. 初回とUDFの変更時は、xlwingsのリボンで「Restart UDF Server」→「Import Functions」。
4. 毎朝、先頭の`Control`シートのB3(`Trade date`)に今日の日付を入れる(Ctrl+;。他のブックと同じ形、[udf.md](udf.md)の
   「評価日」)。日付を入れ直せば下流が計算し直される(Ctrl+Alt+F9は要らない)。
5. 値は`RtGet`に値が届くたびに再計算される(F9は要らない)。

フィード(`live_feed`)は使わない。レートはこのブックがLSEGのアドインから直接読む。

## シート

### Swap

- **アウトライト(A:K)**: `Flag`(黄色。0でカーブから外す)/`Ticker`/`RIC`/`Bid`/`Ask`(`RtGet`)/`Mid`=(Bid+Ask)/2 /
  `Days`/`Prev days`/`Diff`/`Adj (bp)`/`Message`(`SwapLengthAdjust`、5列のスピル)。テナーはO/N〜40Y(会合スワップは
  入れていない)。RICはTraditionの`JP{テナー}ONI=TRDT`(`live_feed.toml`の`"swap"`と同じ)。
- **Curve(M:P)**: `Leg 1`・`Leg 2`(黄色)を入れると`Spread (bp)`=(Leg 2 − Leg 1)×100、`Adj (bp)`=Adj(Leg 2) − Adj(Leg 1)。
- **Fly(R:V)**: `Wing 1`・`Body`・`Wing 2`(黄色)で`Fly (bp)`=(2×Body − Wing 1 − Wing 2)×100、`Adj (bp)`も同じ組み合わせ。
- スプレッド・フライ・それぞれの調整幅はExcelの数式(`XLOOKUP`でTickerから引く)。

### Dates

前後の営業日(B2・B3、既定5日)の取引日ごとに、テナー(SwapシートのO/N以外のTicker)のスポットのスワップの日付を
並べる(`SwapBusinessDays`・`SwapDateGrid`)。B1は中心の日付(`Center date`、任意。空欄なら`TradeDate`。名前`DATES_CenterIn`→
`DATES_Center`)。日付は暦だけで決まるので、過去・将来の日付も入れられる(来週の長さの変化を見る等)。`TradeDate`の行は黄色。

| 表 | 中身 |
|---|---|
| Days | 長さ = 満期日 − 起算日(暦日) |
| Days change | 長さ − 前の営業日の長さ(0でないセルはオレンジ) |
| Maturity | 満期日 |

起算日(Spot列)=T+2、規約は標準(年1回払い・ACT/365F・Modified Following)。

### Vol

満期13本(1M〜20Y)×原資産13本(1Y〜30Y)。格子は`live_feed.toml`の`"vol"`と同じ。

| 表 | 式 |
|---|---|
| Premium (bp) | `RtGet("IDN","JPY{満期}{原資産}TOATM=TRDT","PRIMACT_1")`(TraditionのATMストラドルのフォワード・プレミアム) |
| P:S | 満期日・残りの営業日数・暦日数(`SwaptionExpiryDays`) |
| Stdev σ√T (bp) | `SwaptionStdevGrid`(カーブはSwapシートの`Mid`) |
| Normal vol, business (bp/yr) | σ√T / √営業日数 × √252 |
| Normal vol, calendar (bp/yr) | σ√T / √暦日数 × √365 |
| BP vol, business (bp/day) | σ√T / √営業日数 |
| BP vol, calendar (bp/day) | σ√T / √暦日数 |

- 換算(年金・フォワード・`ql.bachelierBlackFormulaImpliedVol`)はフィード・朝バッチと同じ
  `engine/common/option/swaption/convert.py`。営業日ベースのノーマルvolはフィードの`LiveQuotes("vol")`と同じ値になる
  (実DBの引けで一致を確認)。
- 営業日数は満期の15時までの残り(`engine/common/option/term.py`。日中も減る)。暦日数 = 満期日 − 今日
  (Traditionのvolと同じT=暦日/365。`calendar_factor`と同じ数え方)。

### FutOpt

長期国債先物オプション(大阪取引所)の板。同じ形の表を3つ横に並べ(A:M・O:AA・AC:AO、既定は12月限・1月限・3月限)、
期近・限月の間のオプション等を同時に見る。表ごとに、3行目(`Option month`、黄色)に限月の月の日付(例 2026-12)を入れると、
ストライク21本(中心の上下に0.25刻みで10本ずつ)の気配とボラが並ぶ。中心は4行目(`Center strike`、黄色、任意)、空欄なら
ATM(先物のMidに一番近い0.25刻み、その行は色付き)。

1つ目の表(A:M)の場所。2つ目・3つ目は14列ずつ右にずれる(入力・値の列は C・Q・AE)。

| 場所 | 中身 |
|---|---|
| C5:C6 | 原資産の先物のRIC・取引最終日(`FutOptContract`。規則は`engine/common/jgb_futures.py`) |
| C7 | 取引最終日の15時までの残りの営業日数(`FutOptBusDays`、日中も減る) |
| C8:C11 | 先物のBid・Ask・Last(`RtGet`)とMid(気配が無ければLast)。ボラはこのMidで計算する |
| C12:C13 | ATMのストライク・使う中心 |
| A:F / G / H:M(17〜37行) | コールの Vol Bid・Vol Ask・Bid size・Bid・Ask・Ask size / Strike / プットの Bid size・Bid・Ask・Ask size・Vol Bid・Vol Ask |
| E3:H5 | 中心の行のLSEGの`UNDERLYING`・`EXPIR_DATE`(規則で出した原資産・取引最終日の答え合わせ) |
| AR:BU(折りたたみ) | 表ごとに10列(AR:BA・BB:BK・BL:BU): コール・プットのRIC(数式で組み立てる)と、参考の Last・Settle・OI・LSEGのボラ(`IMP_VOLT`) |

- 気配は`RtGet("IDN",RIC,"BID"/"ASK"/"BIDSIZE"/"ASKSIZE")`。RICは`JGB{ストライク×10の4桁}{月コード}{年の下1桁}`で、
  月コードはコールが A〜L、プットが M〜X(1〜12月)。例 `JGB1280L6` = 2026年12月限 128.00 コール。
- ボラ(`FutOptVol`、コール・プットで1回ずつ): Black式(割引なし)の価格ボラ(%/年)。PositionSheetの国債先物オプションと同じ
  定義(`pricer.implied_price_vol`。アメリカン型をヨーロピアンで近似)。T = 取引最終日の15時までの残りの営業日数 / 252
  (`engine/common/option/term.py`)。気配が無い・本質的価値以下のセルは空欄。
- 限月は毎月。原資産はその月以降で一番近い3・6・9・12月限(1・2月限は3月限)、取引最終日は限月の前月の最終営業日
  (LSEGの2026-11-28の値と一致)。

## 日数の調整幅(Adj)

スワップは取引日によって起算日・満期日の休日調整が変わり、前の営業日と長さ(暦日)が違う日がある。その分のレートの
ずれを出す(ユーザー決定 2026-11-15「長さだけ」。式は`engine/live_screen/day_length.py`):

    Adj = パーレート(今日の起算日 → 今日の満期日) − パーレート(今日の起算日 → 今日の起算日 + 前の営業日の長さ)

- どちらも今のライブのカーブで評価する。今日のほうが短く、カーブが右上がりなら負。長さが同じなら0。
- 起算日は同じにし、期間が1日先へずれる分(期間の前進)は入れない。入れると長さが同じ日でも、1Yで毎日約+0.2bp出る
  (2026-10-27の実DBのカーブ。利上げの織り込みで短期側が右上がり)。
- 2本目の満期日は休日調整しない(`SwapTerms.adjust_end=False`)。調整すると休日に落ちた満期日が今日の満期日に寄り、
  長くなった日の調整幅が0になる。
- 大きさの目安(2026-10-27の実DBのカーブで、前の営業日との比較、2026-10-28〜2027-01-26):

  | テナー | 1日あたり | 最大の絶対値 |
  |---|---|---|
  | 1M | 約0.16bp | 約1.1bp(月末をまたいで+4日) |
  | 3M・6M | 約0.12bp | 約0.4bp |
  | 1Y | 約0.08bp | 約0.2bp |
  | 2Y | 約0.04bp | 約0.13bp |
  | 5Y〜10Y | 約0.02〜0.03bp | 約0.06〜0.08bp |
  | 30Y | ほぼ0 | ほぼ0 |

## 判断

- **自動計算+`RtGet`で更新する(ユーザー決定)**。VBAのタイマーは使わない。自動計算のExcelは`RtGet`(RTD)に値が届くと
  依存するセルだけを計算し直す。フィード経由(`LiveQuotes`)にしないのは、このブックはShift+F9で読み直すのではなく
  常に最新を見たいので、LSEGから直接読む方が素直なため。
- **ボラはプレミアムを`RtGet`で読み、UDFで換算する(ユーザー決定)**。フィードが止まっていても動く。
- **UDFが返すのはσ√Tと日数だけ**で、年率・1日あたりへの直し方はExcelの数式(四則演算はExcel)。
- **日付はQuantLibで出し、プロセスの中で覚える**(取引日とテナーだけで決まる)。ティックごとの計算はパーレートだけに
  なり、調整幅の再計算は34テナーで約0.07秒(初回だけ約0.7秒)。カーブはレートが同じなら作り直さない(`market.curve_set`)。
- **ライブのレートを使うUDFは、評価日(`TradeDate`)が今日でないとエラー**(ユーザー了承、2026-11-23)。`SwapLengthAdjust`・
  `SwaptionStdevGrid`・`SwaptionExpiryDays`は`TradeDate`を末尾の引数で受け取る(`engine/live_screen/udf.py`の`_live_date`)。
  他のブックのように過去日でDBの引けに切り替えないのは、`RtGet`のレート・プレミアムが今の値で、DBの引けのカーブと混ざるため。
  日付の表(`SwapBusinessDays`)は暦だけで決まるので、どの日付でもよい。
- 採らなかった案: 起算日・満期日を前日のものに丸ごと替えた差(期間の前進が混ざる)、満期日を休日調整した差(長くなった日が0になる)。

## 未確認

- 新しいExcelのインスタンスでLSEGのアドイン(`RtGet`)とxlwingsのアドインが読み込まれるか、`RtGet`の更新で再計算の
  重さが気にならないか(Excelでの確認)。
- FutOptの気配(`BID`・`ASK`・`BIDSIZE`・`ASKSIZE`)が取引時間中に`RtGet`で届くか、LSEGの`EXPIR_DATE`の表示の形
  (2026-11-28の確認は週末で、Python APIで静的な項目だけ)。
- ヒストリカル(過去のスプレッド・フライ・ボラ)は開発が済んでから追加する([roadmap.md](roadmap.md)の22)。
