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
| F7 先渡複利 | 先渡dirtyに一致する半年複利(D/365、起点 se)を⌊ ⌋₃ | UDF `JgbForwardRisk`の1列目 | `pricer.forward_duration`(QuantLibの`bondYield`) |
| F8 先渡DD・MD | DD = −dP/dy(y = F7の切り捨て後、起点 se)、MD = DD / 先渡dirty × 100(分母はF6のdirty) | `JgbForwardRisk`の2・3列目(QuantLibの`duration`) | 同上 |
| F9 価格ボラ | σP = σy × √252 × MD / 100 | シートの数式 | |
| F10 プレミアム | Black式(F = 先渡clean、K = ストライク単価、標準偏差 = σP/100 × √T)。割引なし | UDF `BlackPremium`(`ql.blackFormula`) | `pricer.black_premium` |
| F11 ITMバイライトのストライク | K + C(K) = スポット単価 を満たすK(Cはコールのプレミアム、F10)を⌊ ⌋₃ | UDF `ItmBuyWriteStrike`(`ql.Brent`) | `pricer.itm_buy_write_strike` |

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

ストライクの指定は4つの入力列に分け、1行につき1つだけ埋める(プルダウンで方式を選ぶと書式がずれるため、ユーザー決定)。
2つ以上埋めた行、ITMBWなのにストライクの入力がある行は、メッセージを出しストライクを#N/Aにする。

| 入力 | ストライク単価K |
|---|---|
| Strike slide(bp) | F1(y = ATM単利 + スライド/100, 起点 = **te**) |
| Strike yield(%) | F1(y = 入力, 起点 = te) |
| Strike fwd price | 入力そのもの(行使後の受渡のclean単価) |
| Fwd offset(銭) | ⌊先渡clean + 入力/100⌋₃ |
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
- オーバーライド: bp vol(bp/日)> ノーマルvol(bp/年)> 補間値の順に使う。どちらの値も表示する。

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
| `JgbForwardRisk(利率, 償還日, 先渡dirty, 行使後の受渡日)` | [先渡複利, DD, MD](横に) |
| `SwaptionVol(範囲, 取引日, 満期, 償還日)` | ノーマルvol(bp/年)の補間値 |
| `BlackPremium(種類, 先渡, K, 価格ボラ, T)` | プレミアム(額面100円あたりの円) |
| `ItmBuyWriteStrike(先渡clean, 目標単価, 価格ボラ, T)` | F11のK |

銘柄コードを受けるUDF(`JgbInfo`・`JgbClose`)と、Excelの式に移した計算のUDF(`JgbSettle`・`JgbPrice`・`JgbYield`・
`BondForward`・`ForwardDuration`・`OptionTerm`)は2026-10-19に廃止した。日付の引数は日付だけを受け付ける
(スワップ用のIMMコード・テナーの表記は通さない)。計算方法は手動+F9。

## ブック(`BondOption.xlsm`)

- **Bonds**シート: A3 `=JgbMaster(TradeDate)`(名前`BondList` = `Bonds!$A$3#`)。
- **Holidays**シート: A3 `=JapanHolidays(DATE(YEAR(TradeDate)−1,1,1), DATE(YEAR(TradeDate)+21,12,31))`
  (名前`Holidays` = `Holidays!$A$3#`)。
- **Option**シート: B1 取得モード(Live/Fixed)、B3 評価日(空欄=今日)、B4 使う評価日(名前`TradeDate`)、
  B5 換算の日数(名前`VolDays`、252)。8〜27行目が1行1オプション。BA列(折りたたみ)がライブのボラ(`LiveQuotes("vol")`)。

| 列 | 内容 |
|---|---|
| A〜M(黄色、入力) | Bond, Scheme(BW/CC/TB/ITMBW), Expiry, Settle lag(1/2), Notional(億円), Repo(%), ATM slide(bp), Strike slide(bp), Strike yield(%), Strike fwd price, Fwd offset(銭), Normal vol override(bp/年), bp vol override(bp/日) |
| N〜Q | 利率・償還日・引け値・引けの日付(列ごとに`BondList`からXLOOKUP。スピルさせず1セルずつにして、どのセルも上書きできる) |
| R〜V | Type(Schemeから)、ATM単利、スポット受渡日・行使後の受渡日(`WORKDAY`)、T(`NETWORKDAYS`) |
| W〜AB | スポット単価(`JGB_PRICE`)、経過利子(s0・se)、期中の利払(`JGB_CARRY_COUPONS`)、先渡dirty・clean |
| AC〜AE | 先渡複利・DD・MD(`INDEX(JgbForwardRisk(…), 1, k)`で1セルずつ。MDだけ上書きしても#SPILL!にならない) |
| AF〜AI | ノーマルvol(`SwaptionVol`)、使うノーマルvol・bp vol、価格ボラ |
| AJ〜AL | Strike basis(どの入力を使うか、または入力の誤りのメッセージ)、ストライク単価(入れ子のIFで、使う入力の式だけを評価する)、ストライク単利(se起点) |
| AM〜AO | プレミアム(銭・円)、K + プレミアム(ITMBWの確認) |

`INDEX(配列UDF(…), 行, 列)`のセルはExcelが旧式の数式(`cm`属性なし)として保存するが、`.Formula`にも`@`が付かず
結果は同じ。`tools/template.py`の検査はこの形を除いている。

## 未確認・未決定

- 2/29を含む期間の経過利子(F3)・レポの日数(F6)でうるう日を数えるか(今は暦日で数える。サンプルでは確かめられていない)。
- 償還日・利払日が休日の銘柄の扱い。
- 負の利回り・単価の切り捨ての向き(今は0に近い側。[roadmap.md](roadmap.md)の14 #4)。
- 単価の切り捨ての一次資料(日本証券業協会の資料は式が画像で、二次情報のみ)。
- プレミアムの受渡日を選んで割り引くか(後回し。今は割り引かない)。
- 銘柄コードの記号(JS・JL・JX・JU)はユーザー未確認。
- 引け値の履歴はバッチを回した日の分だけ溜まる(過去日の評価日には、その日までに取り込んだ引け値を使う)。
