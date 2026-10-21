# 現物オプション・プライサー(`BondOption.xlsm`、`engine/bond_option/`)

国債店頭オプション(ヨーロピアン、現物受渡)のプレミアム・価格ボラ・先渡価格・先渡利回りを、1行1オプションで
計算するブック。先物オプションは対象外。2026-10-18にたたき台を作成。

2026-10-19に構成を見直した(ユーザー指示)。LSEGの銘柄データはセルに出し、QuantLibが必要な計算だけをUDFにし、
単利⇔単価・経過利子・受渡日・先渡価格のような四則演算程度の計算はExcelの式にする。UDFは銘柄コードではなく
利率・償還日のセルの値を受け取るので、不具合のときも途中のセルに値を手で入れれば残りが計算される。

計算規則は、ユーザーのプライサー(正)の計算結果(4サンプル。取引の実データを含むのでコミットしない。
`data/bond_option_samples.toml`がローカルにあるときだけ`tests/test_bond_option.py`が照合する)と
照合して特定した。4サンプルとも、入力(ATM単利・スライド・レポ・swap vol・日付)からプレミアムまで小数4桁で一致する。

## 記号と単位

| 記号 | 意味 | 単位 |
|---|---|---|
| c | 表面利率 | % |
| y | 単利 / 複利 | %。市場で取引される単利は5糸(0.005%)刻みの正確な値 |
| P | 単価 | 額面100円あたりの円 |
| r | レポ | %、単利・暦日/365 |
| σy | bp vol(利回りボラ) | bp/日(年率 = ×√252) |
| σP | 価格ボラ | %/年 |
| プレミアム | | 銭(額面100円あたり。= 円 × 100) |
| D(a, b) | うるう日除きの日数: 暦日(a→b) − (a, b]に含まれる2/29の数 | `ql.Actual365Fixed(NoLeap)` |
| ⌊x⌋₃ | 小数3桁で切り捨て(0に近い側、ExcelのROUNDDOWN。負の値の慣行は未確認で暫定) | |

## 日付

| 日付 | 規則 |
|---|---|
| 取引日 t0 | シートの評価日(空欄=今日) |
| スポット受渡日 s0 | t0のT+1(営業日、`ql.Japan()`) |
| 満期日(行使日) te | 入力 |
| 行使後の受渡日 se | teのT+lag(lag = 1または2、行ごとの入力) |
| 利払日 | 償還日から半年ごとに遡る。休日調整なし。初回が変則の銘柄も名目の利払日から経過利子を数える |
| 期間T | 営業日数(t0→te、`ql.Japan()`)/ 252 |

シートでは、営業日は`WORKDAY`・`NETWORKDAYS`にHolidaysシートの祝日(`JapanHolidays`、`ql.Japan()`の祝日から
土日を除いたもの)を渡して数える。s0 = `WORKDAY(t0, 1, Holidays)`、se = `WORKDAY(te, lag, Holidays)`、
T = `NETWORKDAYS(t0, te − 1, Holidays)/252`。QuantLibの`businessDaysBetween`・`advance`と2万通りの日付で一致を確認した。

## 計算式

| | 式 | 実装(シート) | 照合用のPython |
|---|---|---|---|
| F1 単利→単価 | n = D(起点, 償還日)/365、P = ⌊100 × (c + 100/n) / (y + 100/n)⌋₃ | 名前付きLAMBDA `JGB_PRICE(c, y, 起点, 償還日)` | `bond.price_from_simple_yield` |
| F2 単価→単利 | y = ⌊(c + (100 − P)/n) / P × 100⌋₃ | `JGB_YIELD(c, P, 起点, 償還日)` | `bond.simple_yield_from_price` |
| F3 経過利子 | c × 暦日(前回利払日→d)/365 | `JGB_AI(c, d, 償還日)`(前回利払日 = `EDATE(償還日, −6k)` ≤ d の最大) | `bond.accrued_interest`(QuantLibの`accruedAmount`) |
| F4 スポット単価 | F1(y = ATM単利, 起点 = s0) | `JGB_PRICE` | |
| F5 ストライク単価 | 「スキームとストライク」の節 | | |
| F6 先渡価格 | dirty = (P0 + AI(s0)) × (1 + r × 暦日(s0→se)/365) − Σ[s0 < 利払日 ≤ se] c/2 × (1 + r × 暦日(利払日→se)/365)、clean = dirty − AI(se)。丸めない | シートの数式(期中の利払日は`EDATE(償還日, −6k)`で列挙) | `pricer.forward_price` |
| F7 先渡複利 | 先渡dirtyに一致する半年複利(D/365、起点 se)を⌊ ⌋₃ | 表UDF `OptionMarketTable`の1列目 | `pricer.forward_duration`(QuantLibの`bondYield`) |
| F8 先渡DD・MD | DD = −dP/dy(y = F7の切り捨て後、起点 se)、MD = DD / 先渡dirty × 100(分母はF6のdirty) | `OptionMarketTable`の2・3列目(QuantLibの`duration`) | 同上 |
| F9 価格ボラ | σP = σy × √252 × MD / 100 | シートの数式 | |
| F10 プレミアム | Black式(F = 先渡clean、K = ストライク単価、標準偏差 = σP/100 × √T)。割引なし | 表UDF `PremiumTable`(`ql.blackFormula`) | `pricer.black_premium` |
| F11 ITMバイライトのストライク | K + C(K) = スポット単価 を満たすK(Cはコールのプレミアム、F10)を⌊ ⌋₃ | 表UDF `ItmBuyWriteTable`(`ql.Brent`) | `pricer.itm_buy_write_strike` |

ATM単利 = 引け値の単利(JCL)+ ATMのスライド(bp)/100。

名前付きLAMBDA(ブックの名前の定義):
- `JGB_YEARS(a, m)` = (m − a − (a, m]に含まれる2/29の数)/365(n、F1・F2の年数)
- `JGB_PRICE(c, y, a, m)` = `ROUNDDOWN(ROUND(100*(c+100/n)/(y+100/n), 9), 3)`
- `JGB_YIELD(c, p, a, m)` = `ROUNDDOWN(ROUND((c+(100−p)/n)/p*100, 9), 3)`
- `JGB_PREV_COUPON(d, m)` = d以前で直近の利払日(`EDATE(m, −6k)`の列挙から)
- `JGB_AI(c, d, m)` = c × (d − `JGB_PREV_COUPON(d, m)`)/365
- `JGB_CARRY_COUPONS(c, m, s0, se, r)` = Σ[s0 < 利払日 ≤ se] c/2 × (1 + r/100 × (se − 利払日)/365)(F6の期中の利払)

LAMBDAとF6のシートの式は、Pythonの関数と2,759通り(負の利回り、月末の償還日、レポ20%まで)で一致を確認した
(単価・単利は完全一致、経過利子・先渡価格は1e-13未満の差)。

切り捨ての前の`ROUND(…, 9)`は、浮動小数点の誤差(91.30099999…)で1つ下がらないため(Pythonの`truncate3`と同じ)。
Pythonの`bond.py`・`pricer.forward_price`・`option_term`は、サンプルとの照合(テスト)とシートの式の確認に残している。

## スキームとストライク

| Scheme | 内容 | 種類 |
|---|---|---|
| BW | バイライト(債券を持ってコールを売る) | Call |
| CC | カバードコール | Call |
| TB | ターゲットバイイング(プットを売る) | Put |
| ITMBW | ITMバイライト(ストライク + プレミアム = 購入単価になるストライクで、コールを売る) | Call |

ストライクの指定は5つの入力列に分け、1行につき1つだけ埋める(プルダウンで方式を選ぶと書式がずれるため、ユーザー決定)。
2つ以上埋めた行、ITMBWなのにストライクの入力がある行は、メッセージを出しストライクを#N/Aにする。

| 入力 | ストライク単価K |
|---|---|
| Spot +sen(銭) | ⌊スポット単価 + 入力/100⌋₃(2026-10-21に追加) |
| Fwd +sen(銭) | ⌊先渡clean + 入力/100⌋₃ |
| Fwd price | 入力そのもの(行使後の受渡のclean単価) |
| ATM +bp | F1(y = ATM単利 + 入力/100, 起点 = **te**) |
| Yield(%) | F1(y = 入力, 起点 = te) |
| (ITMBW、入力なし) | F11(F = 先渡clean、目標 = スポット単価) |

表示のストライク単利は F2(P = K, 起点 = se)。

**F11(ITMBW)**: f(K) = K + C(K) はKについて単調増加(傾き 1 − N(d2) > 0)で、K → 0 で先渡価格、K = 目標で目標以上
になる。そのため解があるのは「スポット単価 > 先渡clean(キャリーが正)」のときだけで、そうでなければエラーにする。
解はふつう K < 先渡(ITM)。0.001円で切り捨てる(ユーザー確認済み)。シートでは確認のため K + プレミアム も表示する。

### QuantLibを使わない式(F1・F2・F6)の理由
- **単利**: `ql.Simple`の利回りは各キャッシュフローを 1/(1+y·t) で割り引く別の定義で、JGBの単利
  (c + (100−P)/n)/P に当たるものはQuantLibに無い(1.43で確認)。
- **先渡価格**: `ql.BondForward`は期中の利払を曲線のDFで割り引き(c(1+rT)/(1+r·tc))、JGBのレポの慣行
  c(1+r(T−tc))と合わない(レポ20%・6Mで約0.0011円)。スポット価格も曲線から計算する作りで、市場の単価を入れられない。
- 日数・経過利子・利払日・営業日・複利・デュレーション・Black式はQuantLib。

### 否定した候補(検算の記録)
- 先渡MDを30/360やActualActual、うるう日込みのActual365Fixedの半年複利で計算する方法(わずかにずれる)。
- ストライク単価の日数の起点を行使後の受渡日にする方法(合わない)。

## ボラ

- 供給元: フィード(`live_feed.toml`の`"vol"`、`JPYTO{満期}X{原資産}ATM=R`の`MID_IV_NOR`、ノーマルvol bp/年)。
  `LiveQuotes("vol")`で読む(Live/Fixedの切り替えは他のフロントシートと同じ)。格子は満期1M〜20Y × 原資産1Y〜20Y。
- 補間(`engine/bond_option/vol.py`): 満期 = 取引日→オプション満期、テナー = オプション満期→償還日(行使時点の残存)。
  年数はどちらもD/365。テナー方向はvolで線形、満期方向は分散(σ²t)で線形、格子の外は端の値で平らに延ばす。
  値の無い点(#N/A等)は使わない。
- bp/日への換算: ノーマルvol / √252(シートの`VolDays`セル。あとで変える可能性がある)。
  T(営業日/252)と価格ボラ(×√252)の252は打ち消し合い、プレミアムには効かない(標準偏差 = σy × MD × √営業日数)。
  効くのはノーマルvol→bp/日の換算だけ。
- オーバーライド: Price vol(%)> bp vol(bp/日)> ノーマルvol(bp/年)> 補間値の順に使う(2つ以上入れたら優先の高い方)。
  3つとも「override列 │ 使う値」の組で、上書きした値から残りの2つを逆算して表示する
  (bp vol = 価格ボラ × 100 /(√252 × MD)、ノーマルvol = bp vol × √VolDays)。

## データ(朝のバッチ)

- `python -m engine.common.morning_batch`の④で、LSEG(Python API、`engine/common/jgb_source.py`)から
  現存するJGB全銘柄(2/5/10/20/30/40年)の銘柄情報と、引け値(`{RIC}JCL`のCF_CLOSE、単利)を
  `data/historical.db`の`jgb_bonds`・`jgb_closes`に保存する。シートはDBを読むだけ(理由は[decisions.md](decisions.md))。
- 銘柄コード: 年限の記号 + 回号(2年 JN、5年 JS、10年 JB、20年 JL、30年 JX、40年 JU。例 JB380)。
  RICは`JP{回号4桁}00{種別}=`(種別 42/45/67/69/68/54)。
- 引け値は評価日以前で直近のもの(日付も表示する)。

## UDF(`engine/bond_option/udf.py`、`BondOption.py`が再エクスポート)

| UDF | 返す値 |
|---|---|
| `ValuationDate(評価日)` | 評価日(空欄=今日) |
| `JgbMaster(評価日)` | 全銘柄(償還日の順)の [銘柄コード, 利率, 償還日, 引け値の単利, 引けの日付, RIC, ISIN]。引けが無い銘柄は空欄 |
| `JapanHolidays(開始日, 終了日)` | 日本の祝日(土日を除く)を縦に |
| `OptionMarketTable(銘柄, 利率, 償還日, 先渡dirty, 行使後の受渡日, 満期, ボラの範囲, 取引日)` | 全行の [先渡複利, DD, MD, ノーマルvol(補間), message] |
| `ItmBuyWriteTable(Strike basis, 先渡clean, スポット単価, 価格ボラ, T)` | 全行の [F11のK, message](Strike basisが"ITMBW"の行だけ) |
| `PremiumTable(種類, 先渡clean, K, 価格ボラ, T)` | 全行の [プレミアム(額面100円あたりの円), message] |
| `JgbForwardRisk`・`SwaptionVol`・`BlackPremium`・`ItmBuyWriteStrike` | 上の表UDFの1行用(単独の確認用。シートでは使わない) |

**表UDF(2026-10-21、ユーザー指示)**: 1行ごとにUDFを置くと、再計算の時間がUDFのセルの数で決まる(1セルごとに
Excelとの往復がかかる。PricingTableと同じ理由、[decisions.md](decisions.md))。行ごとに5〜6個(100行で500超)
あったUDFを、全行を1回で計算する3つの表UDF(本体は`engine/bond_option/table.py`)にまとめた。四則演算程度の計算は
Excelの式のまま残す(手で上書きしたときに対応しやすいため、ユーザー指示)。表UDFは右の計算用の領域(AV〜BD)に
スピルさせ、表示の列(Fwd yield・DD・MD・ボラの補間値・ITMBWのストライク・プレミアム)はそのセルを参照するだけに
する。表示のセルを手で上書きすれば、下流の式とその先の表UDFはその値で計算される(スピルの中を上書きして
#SPILL!になることもない)。1行の入力の誤りで表全体をエラーにせず、計算できなかった値は空欄(表示の列は#N/A)、
理由はmessage列(表示はMessage列にまとめる)。ITMBWのストライクとプレミアムを別の表UDFにしているのは、
ストライクを手で上書きしてもプレミアムに効くようにするため。

銘柄コードを受けるUDF(`JgbInfo`・`JgbClose`)と、Excelの式に移した計算のUDF(`JgbSettle`・`JgbPrice`・`JgbYield`・
`BondForward`・`ForwardDuration`・`OptionTerm`)は2026-10-19に廃止した。日付の引数は日付だけを受け付ける
(スワップ用のIMMコード・テナーの表記は通さない)。計算方法は手動+F9。

## ブック(`BondOption.xlsm`)

- **Bonds**シート: A3 `=JgbMaster(TradeDate)`(名前`BondList` = `Bonds!$A$3#`)。
- **Holidays**シート: A3 `=JapanHolidays(DATE(YEAR(TradeDate)−1,1,1), DATE(YEAR(TradeDate)+21,12,31))`
  (名前`Holidays` = `Holidays!$A$3#`)。
- **Option**シート(2026-10-21に組み替え): 上段は B1 取得モード(Live/Fixed)、B3 評価日(空欄=今日)、
  B4 使う評価日(名前`TradeDate`)、B5 換算の日数(名前`VolDays`、252)、E3・E4 Repo bid・offer(名前`RepoBid`・
  `RepoOffer`)、G1:I5 スキームの対応表(名前`SchemeTable` = G2:I5。Scheme │ Type(Call/Put)│ Repo side(Bid/Offer、
  黄色。ユーザーが後日入れる))。6行目がブロック名の帯、7行目が見出し、8〜107行目(100行)が1行1オプション。
  入力は黄色。横線は5行ごと、縦線はブロックの境目(太線)とボラのoverride│使う値の組の間(細線)。
  明細(AE〜AT)と計算用(AV〜BF)は折りたたみ(AUは2つのグループを分ける隙間)。

| ブロック | 列 |
|---|---|
| Trade | A Bond, B Scheme(対応表から選ぶ), C Expiry, D Settle lag(1/2), E Notional(億円) |
| Market | F Repo override(%), G Repo(使う値。overrideが空ならSchemeの対応表のBid/Offer。未設定ならメッセージ), H ATM slide(bp), I ATM yield, J Spot price(`JGB_PRICE`) |
| Strike input(1つだけ) | K Spot +sen, L Fwd +sen, M Fwd price, N ATM +bp, O Yield(%) |
| Forward | P Fwd clean |
| Strike | Q Strike basis(どの入力を使うか、または入力の誤りのメッセージ), R Strike price, S Strike yield(se起点) |
| Vol | T・U Normal vol(override │ 使う値), V・W bp vol, X・Y Price vol |
| Premium | Z Premium(銭), AA 円, AB Strike + premium(ITMBWの確認), AC Quote text(客先に返す文面。中身は後日決める), AD Message |
| Details(折りたたみ) | AE〜AH 利率・償還日・引け値・引けの日付(列ごとに`BondList`からXLOOKUP), AI Type(対応表から), AJ〜AL スポット受渡日・行使後の受渡日・T, AM〜AP 経過利子×2・期中の利払・Fwd dirty, AQ〜AT Fwd yield・DD・MD・ノーマルvolの補間値(計算用のセルを参照) |
| Calc(折りたたみ) | AV `OptionMarketTable`, BA `ItmBuyWriteTable`, BC `PremiumTable`(8行目にスピル), BF3 ライブのボラ(`LiveQuotes("vol")`) |

旧構成(2026-10-19〜21)の`INDEX(配列UDF(…), 行, 列)`のセルはExcelが旧式の数式(`cm`属性なし)として保存していた
(`tools/template.py`の検査はこの形を除いている。今のシートでは使っていない)。

## 未確認・未決定

- 2/29を含む期間の経過利子(F3)・レポの日数(F6)でうるう日を数えるか(今は暦日で数える。サンプルでは確かめられていない)。
- 償還日・利払日が休日の銘柄の扱い。
- 負の利回り・単価の切り捨ての向き(今は0に近い側。[roadmap.md](roadmap.md)の14 #4)。
- 単価の切り捨ての一次資料(日本証券業協会の資料は式が画像で、二次情報のみ)。
- プレミアムの受渡日を選んで割り引くか(後回し。今は割り引かない)。
- 銘柄コードの記号(JS・JL・JX・JU)はユーザー未確認。
- 引け値の履歴はバッチを回した日の分だけ溜まる(過去日の評価日には、その日までに取り込んだ引け値を使う)。
