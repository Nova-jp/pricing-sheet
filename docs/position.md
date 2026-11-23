# ポジション・リスク管理(PositionSheet.xlsm)

会社のリスク(基準日の引けの値)と、今日の取引のリスク(ライブのカーブでこちらが計算)と、会社のファイルに入っていない
現物オプションのリスク(約定から引けの値でこちらが計算)を合わせて、デルタ・ガンマ・ベガと、基準日の引けからの
おおよそのPLを管理するブック。2026-11-08に簡素化した(Raw_*以外のシートを作り直し、名前付きの数式(LAMBDA)と使っていない
UDFを消した)。

コード: `engine/position/`(`common/`だけに依存)、`PositionSheet.py`(再エクスポート)。
テスト: `tests/test_position.py`・`tests/test_position_bond_option.py`。

## 方針(ユーザーと決定)

- **UDFはQuantLibが要る計算とデータの読み出しだけ**(2026-11-08)。今のデルタ(基準日のデルタ + ガンマ × Move +
  今日の取引)・会社のリスクの抽出・合計は、Excelの1行ずつの普通の数式(四則演算・XLOOKUP・INDEX・FILTER)にする。
  名前付きの数式(LAMBDA)は使わない。値を手で上書きしても他のセルが壊れないように、Nowの列はスピルにしない。
- **会社のデータは生シートに丸ごと貼る**: 会社のファイル(デルタ・ガンマのシートと、ベガのシート)を
  `Raw_IR`・`Raw_IRVOL`のA1にそのまま貼る。現物オプションの全約定は`Raw_BondOption`、現物の保有銘柄は`Raw_JGB`(下の「現物」。ブックへの組み込みは[roadmap.md](roadmap.md)の20)、
  `Raw_Swaption`は満期の近いスワップションの約定(まだ使っていない)。生シートと取引日(Control!B3)は雛形では消す
  (`tools/template.py`の`RECEIVE_AREAS`)。Setupの列名・dataset名・テナーの対応(黄色のセル)は社内データではないので雛形に残す。
  生シートの作り物の見本は`templates/raw_samples/PositionSheet/`にあり、雛形から作ったブックに
  `python -m tools.raw_sample load PositionSheet.xlsm`で貼る(2026-11-23、ユーザー了承。`docs/architecture.md`)。
- **先頭のControlシート(全ブック共通の形)のB3に取引日を入れ(毎朝Ctrl+;)、基準日はその前の直近の引け**。B4
  の名前`TradeDate`のほかに、B7 `=PosBaseDate(TradeDate)`が名前`BaseDate`(取引日より前でDBにカーブがある直近の日 =
  取引日が今日なら朝バッチが置いた昨日の引け。2026-11-18、ユーザー了承)。今日やDBで結果が変わるUDFは、すべて
  `TradeDate`か`BaseDate`を引数で受け取る(今日の取引の`PosIntradayRisk`・`PosIntradayGrid`・ライブのvolの掛け目
  `PosVolCalendarFactor`・セータの日数`PosBusinessDays`は`TradeDate`)。過去の基準日で見たいときは、B3にその翌営業日を入れる。
  Controlシートの形は[udf.md](udf.md)の「評価日」。
- **会社のファイルの形が変わっても直すのはSetupだけ**: 列は見出しの名前で探す(Setupの列名 → 列番号)。
- **デルタの合計 = derivative + bond**(XVAは含まない。ユーザー確認)。
- **グリッドのピラーはマーケットの基準(ライブのカーブの34本)**。会社のファイルに無いテナー(4M・5M・15M等)の
  Closeの列は空欄。
- **引けのレートは朝バッチのDB**(`PosCloseRates`。他のブックと仕組みを揃える。ユーザー指示、2026-11-08)。
- **金利のMoveはスワップと現物で別**(2026-11-10、ユーザー指示)。スワップはライブ − 引け(上書き可)、現物は手入力の
  `Bond move`(複利のbp。空欄ならMoveなし)。JGBのリアルタイムが無いため。現物オプションも現物のMoveを使う。
- **PLは引けのリスクからの近似**(2026-11-10、ユーザーと決定): デルタ × Move + ½ × ガンマ × Move²(ピラーごと、スワップと現物で
  別)、現物オプションのセータ × 基準日からの営業日数、スワップションのベガ × Vol move、今日のDoneの取引(OISはライブのカーブの
  PV、現物は約定の利回りからの動き)。計算はShift+F9(他の列と同じ)。
- **今のデルタは一次式 `基準日のデルタ + 基準日のガンマ × Move(bp)`**(ピラーごと。2026-11-05)。他のピラーの動きによる
  デルタの変化(クロスガンマ)は入れない。満期が近いオプションはこの式では足りない(ストライクをまたぐとデルタが
  段差のように変わる)ので、スワップションはその分だけ約定から計算して補正する予定(下の「満期が近いスワップションの補正」)。
- **VBAは貼り付けのような操作だけ**。今のブックにマクロは無い(xlwingsのUDFの呼び出しだけ)。
- 計算するのはPositionシートのShift+F9のとき(手動計算)。ライブの読み込み(`LiveQuotes`)もPositionシートに置く
  (Shift+F9は開いているシートしか計算しない)。会社のファイルを貼り直したときは、Setupも計算するのでF9を押す。

## ブックの構成

| シート | 中身 |
|---|---|
| Position | 管理の1枚。上段の左(C:G)にMove、H:PにNowのデルタ、Q:Zにデルタの分解(ASW・Fwdを取り除いた残り)、AC:AGにNowのガンマ、AH:ASにClose(スワップは会社の値、現物・現物オプション・国債先物オプションはRaw_JGB・Raw_BondOptionから計算)、AU:BBにPL、BD:BGに今日のDoneの取引、BI:BTにベガ(会社の値・ボラのMove・ベガのPL・まとめたグリッド)、BX:CE・CG:CT・CV:DIにライブのレート・ボラと引けのボラ・計算用の表(折りたたみ)。下段(41行目〜)に今日の取引の入力、AU40:AV53にPLのまとめ |
| Control | 取引日(B3)・`TradeDate`(B4)・`DbStamp`(B5)・`DataStatus`(B6)・`BaseDate`(B7) |
| Setup | 現物・先物オプションのレポ(H7:K7。I7が上書き、空欄なら基準日のO/Nの引け = J7)・HVの窓・`Raw_BondOption`の列名、`Raw_JGB`の列名(L10:N17)、先物オプションのリスクを載せるピラー(I9)、会社のファイルの列名(黄色)と列番号、項目ごとの「生シートの合計」と「グリッドの合計」の差、ピラーの表(カーブの採否・会社のテナーの表記・何番目の行か・生シートの行番号)、ベガのバケットの表(G26:K43) |
| BondOpt | 現物オプションの約定ごとのグリークスと、ピラーへの分解(下の「現物オプション」) |
| JGB | 現物の保有銘柄(Raw_JGB)の銘柄ごとのデルタ・ガンマと、ピラーごとの合計・会社のbondとの差(下の「現物」)。Holding Typeが`OPT`の行は読まない |
| FutOpt | Raw_JGBの国債先物オプション(Holding Typeが`OPT`の行)の行ごとのグリークスと合計(下の「国債先物オプション」) |
| Raw_IR / Raw_IRVOL / Raw_Swaption / Raw_BondOption / Raw_JGB | 会社のシートの貼り付け先(全行が雛形で消える受信領域) |

### Positionシートの上段(ピラー34本 × 4〜37行目、38行目が合計)

並びは左から Move → Nowのデルタ → デルタの分解 → Nowのガンマ → Close で、どこでもBondが先・Swapが後(2026-11-23、ユーザー指示)。
**Close = 貼った生シート(会社のファイル・Raw_JGB・Raw_BondOption)の引け地点のポジション、Now = Close + 今日の取引(下段のDone)+
ガンマによるデルタの変化(Γ × Move)**。現物オプションは引け地点のデータなのでCloseのBondに入る(2026-11-23、ユーザー指示)。
項目(Pillar・Move・Delta Bond・Delta Swap・Delta Total・Remove・Residual Delta・Gamma・Close Delta・Close Gamma)ごとに太線で囲む。

| 列 | 中身 |
|---|---|
| B2 / C2 / E2 | Live/Fixed、ライブの値が最後に変わった時刻(`LiveInfo`)、ライブの状態(`Live: OK`かエラーの理由。赤字) |
| C `Bond move (bp)` | 手入力(黄色)。現物の複利の利回りの、基準日の引けからの動き(bp)。空欄ならMoveなし(0) |
| D `Swap move (bp)` | `=IF(上書き<>"", 上書き, (Live − Close) × 100)`。どちらかが空欄なら空欄(ガンマの分は0) |
| E:G(折りたたみ) | `Live (%)`: BS列のライブの値をピラー名で引く(フィードが止まっていればエラー。0や空欄にしない)。`Close (%)`: DBの基準日の引け(`PosCloseRates`、O/NはTONA実績。DBに無いピラーは空欄)。`Move override`: 手入力(黄色)、空欄ならライブを使う |
| H:K `Delta Bond` | Close + trades = `Close のデルタ Bond(AH) + 今日のDone`、Γ x Move = `Close のガンマ Bond(AO) × Bond move`(灰色の斜体)、Total = 2つの和、棒 |
| L:O `Delta Swap` | Close + trades = `Close のデルタ Swap(AJ) + 今日のDone`、Γ x Move = `Close のガンマ Swap(AQ) × Swap move`、Total、棒 |
| P `Delta Total` | Bond の Total + Swap の Total |
| Q:T `Remove` | 取り除くリスク(下の「デルタの分解」)。Q `ASW sep.`・R `Fwd nY1Y` は手入力(黄色)、S・T はFwdの想定元本とスポット換算(`PosForwardSpotDeltas`、S4にスピル) |
| U:AB `Residual Delta` | Bond = `Delta Bond Total − S`・棒、Swap = `Delta Swap Total + S − Fwd spot`・棒、ASW = `Close の ASW held + S`・棒、Total = BondとSwapの和・棒 |
| AC:AG `Gamma` | Bond(Close + 今日のDone)・棒、Swap(同じ)・棒、Total |
| AH:AN / AO:AS | Close: Delta Bond・棒、Swap・棒、ASW・棒、Total / Gamma Bond・棒、Swap・棒、Total。Swapは会社の値そのもの(Raw_IR)。ASWは会社のASW held(下の「ASW held」。TotalはBond + Swap)。Bondは現物(Raw_JGBから計算、JGBシートのS:T)+ 現物オプション(Raw_BondOptionから計算、BondOptのBB:BC)+ 国債先物オプション(FutOptの合計、SetupのI9のピラーの行だけ) |
| AU:BB | PL(下の「PL」) |
| BD:BG | 今日のDoneの取引のピラーごとの [Delta Swap, Delta Bond, Gamma Swap, Gamma Bond](`PosIntradayGrid`) |
| BI:BT | ベガ(下の「ベガとベガのPL」) |
| BX:CA / CC:CE | ライブの読み込み(Fixedなら`LiveSnapshot`)/ 日中の取引のUDFに渡すカーブの範囲(フラグ・ピラー・レート) |
| CG:CI / CK:CT | ライブのボラ(`LiveQuotes("vol")`、Fixedなら`LiveSnapshot`)/ 基準日の引けのボラ(`PosCloseVols`、原資産1Y〜30Y) |
| CV / CW / CY:DI | 暦日ベースへの掛け目(ライブ = 今日、引け = 基準日。`PosVolCalendarFactor`)/ 会社のベガの格子(円、`PosVegaGrid`) |

- **棒グラフ**: 値の右隣の列(K・O・V・X・Z・AB・AD・AF・AI・AK・AM・AP・AR。値を参照して数値は隠す)に条件付き書式のデータバー。対象はNowの
  デルタ(Bond・SwapのTotal)・残差(Bond・Swap・ASW・Total)・Nowのガンマ・Closeのデルタ(Bond・Swap・ASW)・ガンマのBondとSwap(2026-11-23、ユーザー指示)。PLはBB。
- **先物オプションの目印**: 先物オプションの分が入っている行(SetupのI9のピラー)のH・AC・AH・AO列は、数値の後ろに` F`が付き
  太字になる(条件付き書式の表示形式。列は増やさない。2026-11-23、ユーザー指示)。
  軸はセルの中央、プラスは右へ青、マイナスは左へ赤。
- **リスクに入っていない行の表示**: Closeの見出し(AH1)に、BondOpt・JGB・FutOptでリスクに入っていない行(BondOptは
  期限切れを除く)と、計算はしたが警告のある行の数を`>> Not in risk: BondOpt 2, JGB 1, FutOpt 0 | Warnings: 3`の形で赤字で出す
  (無ければ`Close (日付)`だけ。2026-11-23、ユーザー決定。列は増やさない)。どの行かは各シートのMessage列(赤字)で見る。
  Closeの中身の説明はAH39。
- **エラーは隠さない**: ライブ・引け値・日中のUDFがエラーなら、Nowの列もエラーになる。フィードが止まっている間に
  計算したいときはB2を`Fixed`にする(そのシートが最後にLiveで読んだレートで計算する)。

### デルタの分解(Q:AB、2026-11-23、ユーザーと決定)

Nowのデルタから、ASWやFwdとして持っていると見るリスクを取り除いて、残り(Residual)を見る。入力はピラーの行ごとに2列(黄色、
百万円/bp、符号はシートのデルタと同じ = 金利が1bp上がったときのPVの変化)。

- **ASW sep.(Q列)**: ボンド・スワップのうちASWとして分けて管理する量S。ボンド側とスワップ側の両方からそのまま取り除き
  (ボンドに `+S`、スワップに `−S` が載っている分を引く。2で割らない。2026-11-23、ユーザー指示)、ASWに足す。ボンド側と
  スワップ側の和は0なので、Totalは変わらない。PLも同じSで付け替える(下の「PL」)。
- **Fwd nY1Y(R列)**: その行のピラーの年限先から1年のFwd(5Yの行 → 5Y1Y、6Mの行 → 6M1Y、O/Nの行 → スポットの1Y)で取り除く
  リスク。**スワップだけ**から引く。`PosForwardSpotDeltas`(`engine/position/forwards.py`)が、日中の取引と同じ規約・バンプ
  (標準のOIS、`risk.bucketed_deltas`、Nowと同じカーブ)で、ピラーごとのデルタの合計が入れたリスクになる想定元本(S列、
  百万円、Recが正)を決め、ピラーごとのデルタ(スポット換算、T列)を全行で足して返す。スポットに直すと入れたリスクより大きく
  見える(5Y1Yに+1.0 → 6Yに大きくプラス、5Yに大きくマイナス、合計は+1.0)。12Yより先はカーブの補間の比率で近くのピラーに分かれる。
- **Residual(U:AB)**: Bond = `Delta Bond Total − S`、Swap = `Delta Swap Total + S − Fwd spot`、ASW = `ASW held + S`、
  Total = BondとSwapの和(= `Delta Total − Fwd spot`。ASWは平行の動きでは効かないので入れない)。Fwdはガンマ・Close・PLには使わない。

### ASW held(Close AL、2026-11-23、ユーザーと決定)

- 会社のファイルのエキゾチックなASWのポジションのリスク。Raw_IRのdataset `CURVE.JPY.GOVT`(SetupのB23)の
  `delta_derivative_base`(B24)を百万円/bpで、テナー名でピラーに合わせる(OISと違い行は抜けがある)。`rate_base`はASWの水準(小数、0.0016 = 16bp)で、使わない。
- 符号はシートのデルタと同じで、正ならボンドが正・スワップが負のリスク(−7.77 = 現物を買ってスワップをPayするASWのロング)。
- ボンド・スワップ・ASW heldの3つが引けで持っているリスク(ユーザー指示)。ASW heldはCloseのBond・Swapには入れず、
  Close(AL)・残差(Y)・PL(AZ)の別の列に置く。ガンマは入れない。
- チェック: SetupのD24:F24で「生シートの合計」と「表の合計」の差(Positionのピラーに無いテナーの行があると0にならない)。
- 先の予定(ユーザー構想): カーブ・フライでの分解、それぞれのリスクをPCAで見る。

### PL(AU:BB・AU40:AV53、2026-11-10、ユーザーと決定)

基準日の引けのリスクに今の動きを掛けた近似(百万円)。どれもシートの四則演算で、Shift+F9で動く。上段の太線の右に、
見出し`PL (approx. since base-date close, mn JPY)`・`PL Swap / Bond / Bond opt / Total`と太線で囲んで置く(上段の値の
計算には使わない。上段のCloseとMoveから計算する)。

| 列 | 式(ピラーごと) |
|---|---|
| AU・AV `Swap Delta / Gamma` | (Close のデルタ Swap + S)× Swap move、½ × Close のガンマ Swap × Swap move² |
| AW・AX `Bond Delta / Gamma` | (Close のデルタ Bond(現物オプションを含む)− S)× Bond move、½ × Close のガンマ Bond × Bond move² |
| AY `Theta` | 現物オプションのセータ(1営業日、原資産のピラー。BondOptのBD)と先物オプションのセータ(I9のピラー)× 基準日からの営業日数(AV53、`PosBusinessDays`) |
| AZ `ASW Delta` | (Close の ASW held + S)×(Bond move − Swap move)。ピラーのASWの動き = 現物の動き − スワップの動き |
| BA・BB | 合計と棒グラフ |

- **まとめ(AU40:AV50)**: 上の列の合計、スワップションのベガのPL(BT42)、今日のDoneのOIS・現物(R列の行ごとのPLの合計)、合計。
- **今日の取引の行ごとのPL(R列)**: OISは`PosIntradayRisk`のPV(ライブのカーブ、約定の固定金利)。現物は
  `DV01 × m + ½ × ガンマ × m²`、m = 載せたピラーのBond move − Trade move(約定の複利 − 引けの複利)。What-ifの行にも出すが、
  まとめはDoneだけ。
- 今日の取引は引けのリスク(会社のファイル)に入っていないので、ピラーごとのPLの列には入れない。

### ベガとベガのPL(BI:BT、2026-11-09・11-10・11-13、ユーザーと決定)

- **格子**: 満期17本(1W・2W・1M・3M・6M・9M・1Y・2Y・3Y・4Y・5Y・7Y・10Y・15Y・20Y・25Y・30Y)× 原資産11本(6M・1Y・2Y・3Y・
  4Y・5Y・7Y・10Y・15Y・20Y・30Y)。**スワップション(会社の値)だけ**で、現物オプションのベガは入れない(2026-11-10、ユーザー指示。
  約定ごとのベガはBondOptシートに残す)。満期25Y・30Yはスワップションのボラが配信されていない(枠だけ。MoveとPLは空欄)。
  15Yx30Y・20Yx30Yは満期+原資産が40年を超えるので朝バッチ・フィードの格子に入れていない(同じく空欄)。
- **4〜20行目 Vega**: 会社のベガ(`PosVegaGrid`、CY4にスピル)÷ 10^6(百万円 / 暦日の年率のvol 1bp)。会社の満期・原資産の表記を
  年数にし(D = 日/365、W = 7日/365、M = 月/12、Y = 年)、格子の点に年数で線形に分ける。点ちょうどならその点、1Wより短い
  満期は1Wに全部、原資産6Mより短い原資産は6Mに全部、最も長い点より先は最も長い点に全部(例 満期10D → 1Wと2Wに4:3、
  原資産6Y → 5Yと7Yに半分ずつ、原資産9M → 6Mと1Yに半分ずつ)。
  SetupのD22:F22で「生シートの合計」と「表の合計」の差を見る(表記を読めない行があると0にならない)。
- **24〜40行目 Vol move (bp)**: `ライブのvol × 掛け目(今日) − 基準日の引けのvol × 掛け目(基準日)`。どちらのvolも
  TraditionのATMストラドルのプレミアムから`engine/common/option/swaption/convert.py`で換算した営業日ベースのノーマルvol(ライブはフィード、
  引けは朝バッチのDBの`vol_quotes`)で、掛け目 √(営業日数/252) × √(365/暦日数)(`PosVolCalendarFactor`。今日は今の時刻で満期日の15時までの残りを数える。
  `engine/common/option/term.py`)で**暦日ベース**に
  直してから差を取る。会社のベガが暦日の年率のvolで微分した値のため(2026-11-10、ユーザー指示)。1W・2Wの行は1Mの行の
  値を使う(市場のボラが無いので1Mを延ばす)。原資産6Mの列は同じ満期の1Yの値を使う(LSEGのどのブローカーにも原資産6Mの
  クオートが無い。2026-11-13に確認し、ユーザーと決定)。どちらかが空欄なら空欄、フィードが止まればエラー。上書きの列は作らない
  (セルごとの普通の数式なので、手で値に書き換えればPL・まとめたグリッドもその値で動く)。
- **44〜60行目 Vega PL**: `ベガ × Move`(百万円)。合計はBT42で、PLのまとめに入る。
- **62行目〜・82行目〜 まとめたグリッド**: ベガとPLを、Setupの表(G26:K43、黄色=バケットの名前)のバケットごとに
  合計する。表は元と同じ17×11のマスで、バケットのまとまりを罫線で囲み、まとまりの右下のマスに合計を書く(他のマスは
  空欄。ユーザー指示、2026-11-09)。罫線と合計の位置はSetupの表から決める(罫線は条件付き書式)ので、区切りを変えても
  ついてくる。同じバケットは続けて並べる。初期値は満期 1W–6M / 9M–2Y / 3Y–5Y / 7Y–10Y / 15Y–30Y、原資産 6M–2Y / 3Y–5Y / 7Y–10Y / 15Y–30Y。
  Setupの表の並びはPositionのベガの表と同じにする。
- **色**: ベガ・PL・まとめたグリッドにヒートマップ(いちばん小さい値=赤、0=白、いちばん大きい値=青)。
  中の数字は黒(マイナスも赤にしない。2026-11-23、ユーザー指示)。
- 各表の右上に合計。BI1:BN1に現物オプション・先物オプションのセータの合計(百万円/日)。

### 単位(2026-11-04、ユーザー指示)

会社のファイルのデルタ・ガンマ・ベガは円。生シートは円のまま、Positionシートは百万円(10^6で割る。ベガは2026-11-09、ユーザー指示)。日中の取引のUDFは
百万円。デルタ・ガンマ・ベガの表示はカンマ区切りの小数1桁、マイナスは赤(2026-11-08、ユーザー指示。
日本語版Excelなので`NumberFormatLocal`で`[赤]`を使う)。ヒートマップを載せたベガの表だけは`#,##0.0;-#,##0.0`(黒)。

### 会社のファイルからの抽出(Setup)

- 列: Setupの列名(黄色)を生シートの1行目から`XMATCH`で探し、列番号を出す(見つからなければ`#N/A`)。
- 行: 会社のファイルのdatasetの行は、テナーが抜けなく短い順に並ぶ(O/N〜40Yの26行。ユーザー確認、2026-11-15)。
  ピラーの表の「Company row」(datasetの何行目か。空欄=ファイルに無いピラー)で、対象のdatasetの行を`FILTER`で
  抜き出したうちのその番目を取り、生シートの行番号を出す(無ければ`NOT FOUND`)。全ピラーを同じ規則で読み、
  会社のテナーの表記は読み取りに使わない(`35Y`の行が`25Y`と書かれていても、25番目の行なので35Y)。
  「Raw tenor」は読んだ行の会社の表記で、目で確かめるための表示だけ。生シートは書き換えない。
- 行数のチェック: datasetの行数が「Company row」の最大(26)と違えば`Expected 26 rows`(Setup!D7)。テナーが
  増減すると全部の行がずれるので、そのときは「Company row」を振り直す。
- 値: `INDEX(Raw_IR, 行番号, 列番号) / 10^6`。
- チェック: 項目ごとに「生シートの合計(datasetの全行)」と「グリッドの合計」の差を出す。グリッドに入らない行
  (表に無いテナー、重複)があると差が0にならない。
- ベガ: datasetの行を`PosVegaGrid`で格子に振り分ける(上の「ベガとベガのPL」)。

## 現物オプション(BondOptシート、`engine/position/bond_options.py`、2026-11-10、ユーザーと決定)

会社のファイルに入っていないので、`Raw_BondOption`に貼った全約定から基準日の引けの値でグリークスを計算し、Positionの
CloseのBondのデルタ・ガンマ(引け地点のポジション)とセータ(PL)に足す。ベガは約定ごとの表示だけで、Positionのベガの格子には入れない(2026-11-10、ユーザー指示)。リアルタイムでは計算しない(F9で計算)。計算の部品はBondOption.xlsmと同じ
(`engine/common/option/bond/`。式は[bond_option.md](bond_option.md))。分担もBondOptionと同じで、中身のある計算は表UDF
(右の折りたたみのAH:AZにスピル)、表示の列はそれを参照し、符号・額面・単位の換算はシートの数式にする。

- **入力**: `Raw_BondOption`の列を見出しの名前で引く(SetupのH10:J18)。Bond = `JGBCode`、Type = `OptionType`(CALL/PUT)、
  Side = `BuySell`(B/S)、Expiry = `ExerciseDate`、Settle = `EffectiveDate`(行使後の受渡日)、Strike(clean単価)、
  Notional(円、表示は百万円)。行使日が基準日以前の約定は計算しない(Messageに`Expired`)。200行まで。
- **市場(`PosBondOptMarket`)**: 取引日 = 基準日。利率・償還日はDBの銘柄情報、スポット単価は基準日の引けの単利(DB)から
  F1(起点 = 基準日のT+1)、先渡はSetupのレポでF6(I7に入れればその値、空欄なら基準日のO/Nの引け(PositionのF列のO/N = TONAの実績)。2026-11-23、ユーザー決定。見落としで計算されないのを防ぐ)、MD・DDはF7・F8。ボラは基準日のDBのスワップションのvolから
  BondOptionと同じ補間 × Vol ratio(窓はSetupの`HV window`)。T = 営業日数/252。
- **グリークス(`PosBondOptGreeks`)**: Black式(割引なし)のプレミアム・デルタ・ガンマ・ベガ(価格ボラ1%あたり)・
  セータ(1営業日、先渡・ボラはそのまま)。額面100円あたり。
- **ポジション(シートの数式、百万円)**: PV = 符号 × プレミアム/100 × 額面、デルタ換算の額面 = 符号 × デルタ × 額面、
  価格のガンマ = 符号 × ガンマ × 額面、ベガ(スワップションのvol 1bpあたり)= 符号 × ベガ × MD/100 × Vol ratio × 額面/100、
  セータ = 符号 × セータ/100 × 額面。符号はBuy = +1、Sell = −1。
- **ピラーへの分解(`PosBondRiskGrid`、BB3にスピル [Delta, Gamma, Theta]、行はPositionと同じ)**: 原資産の現物の修正デュレーション・
  コンベクシティ(下の「現物」、基準日の引けの単利から)にチェーンルールで写す。デルタ = E × dP / 100、
  ガンマ = (G × dP² + E × d2P) / 100(E = デルタ換算の額面、G = 価格のガンマ、dP・d2P = 額面100円あたりの複利1bpの
  単価の変化とその2階)。銘柄の満期が入る区間の長いほうのピラーに全部載せる。先渡と現物の単価の比
  (1 + レポ × 期間)は1とみなす。
- **セータ**: デルタと同じく原資産の銘柄のピラーに載せる(百万円/営業日。PositionのPLのTheta列)。
- Vol ratioのHVの窓に足りる引け値の履歴が無い銘柄も、リスクから抜かない(2026-11-23、ユーザー決定。`hv.py`)。日次変化が
  5個以上あればある分で計算し、5個未満なら比を1(スワップションのボラのまま)にする。どちらもMessageに警告
  (`HV 12d (< window 20)`・`… → Vol ratio 1`)が出る。朝バッチを流せば`settings.toml`の開始日からの過去分が揃う。

## 国債先物オプション(FutOptシート、`engine/position/futures_options.py`、2026-11-23、ユーザーと決定)

会社のファイルの`Raw_JGB`に、現物と同じ表で長期国債先物オプションの残が入る(Holding Type `OPT+`/`OPT-`、Name
`債先2612C127.50` = 2026年12月限の先物のコール、ストライク127.50)。デルタ・ガンマは入っていないので、基準日の引けの値で
計算し、PositionのCloseのBond(デルタ・ガンマ)とPLのセータに足す。JGBシートはこの行を現物として読まない。

- **入力**: Raw_JGBの列を見出しの名前で引く(SetupのL10:N17)。Holding Typeが`OPT`で始まる行だけをFutOptのA6にFILTERで並べる
  (Name・Holding Type・枚数 = `Yesterday`・取引最終日 = `Maturity`・引けの評価値 = `Closing Mark Pr`)。枚数は符号つきで、
  1枚 = 先物の額面1億円(ユーザー確認)。0枚の行も並ぶ(リスクは0)。
- **計算(`PosFutOptRisk`)**: 先物の価格 = DBの基準日の清算値(`fut_settles`)、T = 取引最終日までの営業日数/252、
  ボラ = 会社の評価値を再現するBlack式(割引なし)の価格ボラ、グリークスは現物オプションと同じ`pricer.black_greeks`。
  アメリカン型をヨーロピアンで近似する(ユーザー了承)。
- **金利のリスクへの換算**: 先物 ≈ CTDの単価 ÷ 交換係数(CF)とみなす。CTD = 基準日の受渡適格銘柄のうちネットベーシス最小
  (`jgb_futures.basis_table`・`cheapest`、レポはSetupのJ7 = 現物オプションと同じ)。CTDの複利1bpあたりの単価の変化 dP・2階 d2P(現物と同じ
  `bonds.bond_risk`)で、シートで
  `CTD換算の額面 = 枚数 × 100 × デルタ / CF`、`デルタ = CTD換算の額面 × dP / 100`、
  `ガンマ = (枚数 × 100 × ガンマ / CF² × dP² + CTD換算の額面 × d2P) / 100`。セータ・ベガ・PV(百万円)= 枚数 × 額面100円あたりの値。
- **載せるピラー**: CTDの満期の区間ではなく、SetupのI9で決めた1つのピラー(初期値7Y)に全部載せる。ロールの近くで2つの
  限月があっても同じピラーにまとめる(ユーザー指示)。ベガは表示だけで、スワップションのベガの格子には入れない。
- 今日の取引の表には先物オプションを入れられない(後回し、ユーザー了承)。

## 現物(`engine/position/bonds.py`、2026-11-10、ユーザーと決定)

会社のファイルの現物(`Raw_JGB`)・日中の現物の取引・現物オプションの原資産で共通。カーブはバンプしない。

- **デルタ・ガンマ**: 単利から利含み単価 P(スポット受渡 T+1)を出し、P に合う半年複利(切り捨てない)での修正デュレーション
  MD・コンベクシティ C から、額面100円あたり dP = −MD × P × 1bp、d2P = C × P × 1bp²。デルタ = dP × 額面/100、
  ガンマ = d2P × 額面/100(百万円/bp、百万円/bp²)。複利が1bp動いたときの単価の変化そのもの(実銘柄で一致を確認)。
  単価を掛けるのは、MDが価格の変化率で円にするには価格が要るため(額面だけだと単価の低い銘柄でずれる。JX68で約2倍)。
- **ピラー**: 按分しない。満期が入る区間の長いほうのピラーに全部載せる(残存2.6年 → 3Y)。ピラーの日付 = スワップの
  スポット日 + テナー(休日調整前)、ピラーの日付ちょうどならそのピラー。最も長いピラーより先は最も長いピラー。O/Nは使わない。
- **Move**: PositionのC列の手入力の`Bond move`(複利のbp、空欄ならMoveなし)を掛ける(JGBのリアルタイムが無いため。
  2026-11-10、ユーザー指示)。
- **ブック(JGBシート)**: A:Dに`Raw_JGB`の列(SetupのL10:N14の列名で引く。500行まで)、F5に`PosJgbRisk`の銘柄ごとの表、S3に`PosJgbGrid`のピラーごとの合計(行はPositionと同じ)。U:Xに会社のbond(Raw_IRの`delta_bond_base`・`gamma_bond_base`)と差。PositionのCloseのBond(V・AA列)はS:Tに現物オプションを足した値なので、Now(Close + ガンマ × Move + 今日のDone)も計算した値で動く。SetupのE13・E15は会社のbondの「生シートの合計」と「グリッドの合計」の比較のまま(JGBシートのU38・V38)。
- **Raw_JGB(`PosJgbRisk`・`PosJgbGrid`)**: ノーショナルは`Yesterday`(百万円)。銘柄は`Instrument ID`
  `0-02-{回号4桁}-000{種別}`(RIC `JP{回号}00{種別}=`と同じ並び、例 `0-02-0023-00068` → JX23)から引き、`Maturity`と
  DBの償還日を照合する(違えばMessage)。利回りは`Closing Mark S Yld`(単利%)、空欄ならDBの基準日の引け値。IDか額面が
  空欄・額面0の行は空欄。償還日だけ・利率+償還日だけでは銘柄が1つに決まらない(DBで7組重なる)のでIDを正にする。

## 日中の取引(`engine/position/intraday.py`)

- OIS: 標準のOIS(`trades.py`の規約)。Start空欄=スポット、Tenor/Endは`10Y`か満期日、SideはPay/Rec、Rate空欄=パー。
- 現物(Bond): 上の「現物」の修正デュレーション・コンベクシティ。SideはBuy/Sell(買い=ロング)、Rateは約定の
  単利(空欄=DBの引け値)。PLのために、載せたピラーと`Trade move`(約定の複利 − DBの引けの複利、bp)も返す。
- OISのデルタ: ピラーを1本ずつ+0.1bp動かしたPVの差を1bpあたりに(PricingSheetのRiskGridと同じ)。
- OISのガンマ: 同じピラーを±0.1bp動かした2階の中心差分(対角のガンマ、百万円/bp²)。

| UDF | 中身 |
|---|---|
| `PosIntradayRisk` | 行ごとの [Rate, PV, DV01, Gamma, Pillar, Trade move, Message]。エラーの行はMessageに理由を出し、他の行は続ける |
| `PosIntradayGrid` | ピラーごとの [Delta Swap, Delta Bond, Gamma Swap, Gamma Bond]。statusでDone / What-if / Allを選ぶ |
| `PosCloseRates` | DBのその日の引け値(ピラーの並びの縦1列) |
| `PosCloseVols` | DBのその日のスワップションのvol(行=満期、列=原資産。無い点は空欄) |
| `PosVolCalendarFactor` | 満期ごとの暦日ベースへの掛け目(ライブは取引日、引けは基準日。取引日が今日なら今の時刻で数えるのでvolatile) |
| `PosVegaGrid` | 会社のベガ(Raw_IRVOLの列)を格子に振り分けた合計(円) |
| `PosBusinessDays` | 基準日から取引日までの営業日数(セータのPL) |
| `PosBaseDate` | 基準日(取引日より前でDBにカーブがある直近の日付) |
| `ValuationDate` / `DbStamp` / `DataStatus` | Controlシートの取引日・DBの朝バッチの実行時刻・状態(全ブック共通。[udf.md](udf.md)の「評価日」) |
| `PosBondOptMarket` / `PosBondOptGreeks` | 現物オプションの行ごとの市場の値・グリークス(上の「現物オプション」) |
| `PosBondRiskGrid` | 現物オプションのピラーごとのデルタ・ガンマ・セータ |
| `PosFutOptRisk` | 国債先物オプションの行ごとの [Contract, Type, Strike, Futures, T, Vol (%), Premium, Delta, Gamma, Vega, Theta, CTD, CF, dP, d2P, Message](下の「国債先物オプション」) |
| `PosJgbRisk` / `PosJgbGrid` | Raw_JGBの行ごとの [Code, Coupon, Maturity, Yield, Compound, MD, Convexity, Pillar, Delta, Gamma, Message] / ピラーごとの [Delta, Gamma] |

同じ取引の表とカーブなら、2つのUDFは1回の計算結果を共有する(`_intraday_cache`)。

## 前提・未確認

- 会社のガンマの定義(対角か、パラレルに1bp動いたときのピラーごとのデルタの変化か)・符号。こちらの日中の
  ガンマは対角。ピラーの間に満期があるスワップは、普通のOISでも対角のガンマが補間の非線形性で大きく出る
  (実DBの100億の22Yで20y −6.87、25y −6.69百万円/bp²。パラレルのガンマではない。2026-11-07の検証)。
- 会社のファイルの日中の取引の列は使わない(ユーザー確認)。
- Raw_JGBのInstrument IDの決まり・`Closing Mark S Yld`の単位(%)・`Holding Type`の意味は見本1行からの推定。
  会社のbondのデルタ(Raw_IRの`delta_bond_base`)がデュレーション基準か、単価を掛けているか、どのテナーに載せているかも未照合。
- 会社のベガの単位(円 / 暦日の年率のvol 1bpを想定)と、満期・原資産の表記(見本は`3M`・`1Y`の形。1W・2Wは`1W`・`2W`を想定)。
- 引けのレート(DBの引け=ONIPE、15時の確定値)とライブ(ONI、Tradition)は系列が違う。相場が動かなくてもMoveが
  少し出る可能性がある(未測定)。
- 現物オプションのガンマ(チェーンルールの対角)が会社のガンマの定義と同じか。先渡と現物の単価の比(1 + レポ × 期間)を
  1とみなしてよいか(1か月で1.0003程度)。

## 満期が近いスワップションの補正(未着手。方針はユーザーと決定、2026-11-08)

現物オプションは全約定をBondOptシートで計算する(上の「現物オプション」)ので、対象はスワップション(`Raw_Swaption`。
今の生シートの形は正しくなく、ユーザーが後で直す)。全体は上の一次式のまま、満期が近いもの(例 残り1か月以内)だけ約定を
入れて計算し直し、
`補正 = 今の再計算のデルタ − (引けのデルタ + 引けのガンマ × Move)`をNowに足す(どちらも自前のモデルなので、会社の値との
モデル差は入らない)。あわせて残り日数・ストライクまでの距離・今のデルタを一覧にする。計算はQuantLibの価格式に、原資産
(Fwd・年金・現物の単価)のピラー感応度を掛ける形(総当たりのバンプより速く、現物のMDの切り捨てによる階段・補間の
非線形性の影響を受けない)。行使して現物・スワップになった分は翌日から会社のSODに入るので、その日に一覧から外す。
