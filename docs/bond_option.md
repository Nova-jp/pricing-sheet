# 現物オプション・プライサー(`BondOption.xlsm`、`engine/bond_option/`)

国債店頭オプション(ヨーロピアン、現物受渡)のプレミアム・価格ボラ・先渡価格・先渡利回りを、1行1オプションで
計算するブック。先物オプションは対象外。2026-10-18にたたき台を作成。

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
| ⌊x⌋₃ | 小数3桁で切り捨て | |

## 日付

| 日付 | 規則 |
|---|---|
| 取引日 t0 | シートの評価日(空欄=今日) |
| スポット受渡日 s0 | t0のT+1(営業日、`ql.Japan()`) |
| 満期日(行使日) te | 入力 |
| 行使後の受渡日 se | teのT+lag(lag = 1または2、行ごとの入力) |
| 利払日 | 償還日から半年ごとに遡る。休日調整なし。初回が変則の銘柄も名目の利払日から経過利子を数える |
| 期間T | 営業日数(t0→te、`ql.Japan()`)/ 252 |

## 計算式

| | 式 | 実装 |
|---|---|---|
| F1 単利→単価 | n = D(起点, 償還日)/365、P = ⌊100 × (c + 100/n) / (y + 100/n)⌋₃ | `bond.price_from_simple_yield`(自前の式) |
| F2 単価→単利 | y = ⌊(c + (100 − P)/n) / P × 100⌋₃ | `bond.simple_yield_from_price`(自前の式) |
| F3 経過利子 | c × 暦日(前回利払日→d)/365 | `bond.accrued_interest`(QuantLibの`accruedAmount`) |
| F4 スポット単価 | F1(y = ATM単利, 起点 = s0) | シートの数式 |
| F5 ストライク単価 | F1(y = ATM単利 + ストライクのスライド(bp)/100, 起点 = **te**)。表示のストライク単利は F2(P = K, 起点 = se) | シートの数式 |
| F6 先渡価格 | dirty = (P0 + AI(s0)) × (1 + r × 暦日(s0→se)/365) − Σ[s0 < 利払日 ≤ se] c/2 × (1 + r × 暦日(利払日→se)/365)、clean = dirty − AI(se)。丸めない | `pricer.forward_price`(自前の式) |
| F7 先渡複利 | 先渡dirtyに一致する半年複利(D/365、起点 se)を⌊ ⌋₃ | `pricer.forward_duration`(QuantLibの`bondYield`) |
| F8 先渡DD・MD | DD = −dP/dy(y = F7の切り捨て後、起点 se)、MD = DD / 先渡dirty × 100(分母はF6のdirty) | 同上(QuantLibの`duration`) |
| F9 価格ボラ | σP = σy × √252 × MD / 100 | シートの数式 |
| F10 プレミアム | Black式(F = 先渡clean、K = ストライク単価、標準偏差 = σP/100 × √T)。割引なし | `pricer.black_premium`(`ql.blackFormula`) |

ATM単利 = 引け値の単利(JCL)+ ATMのスライド(bp)/100。

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

## ブック(`BondOption.xlsm`、Optionシート)

- 1行目: 取得モード(B1 Live/Fixed、D1 Updated)。B3 評価日(空欄=今日)、B4 使う評価日(名前`TradeDate`)、
  B5 換算の日数(名前`VolDays`、252)。
- 8〜27行目が1行1オプション。黄色の列が入力: Bond・Type(Call/Put)・Expiry・Settle lag(1/2)・Notional(億円)・
  Repo・ATM slide(bp)・Strike slide(bp)・Normal vol override・bp vol override。
- 右の列が計算: 利率・償還日(`JgbInfo`)、引け値・日付(`JgbClose`)、ATM単利、ストライク単利(表示)、受渡日(`JgbSettle`)、
  スポット単価・ストライク単価(`JgbPrice`)、先渡clean/dirty(`BondForward`)、先渡複利・DD・MD(`ForwardDuration`)、
  ノーマルvol(`SwaptionVol`)、使うノーマルvol・bp vol、価格ボラ、T(`OptionTerm`)、プレミアム(`BlackPremium`、銭と円)。
- BA:BC列(折りたたみ)がライブのボラ(`LiveQuotes("vol")`)。
- UDFは`engine/bond_option/udf.py`(`BondOption.py`が再エクスポート)。計算方法は手動+F9。

## 未確認・未決定

- 2/29を含む期間の経過利子(F3)・レポの日数(F6)でうるう日を数えるか(今は暦日で数える。サンプルでは確かめられていない)。
- 償還日・利払日が休日の銘柄の扱い。
- 単価の切り捨ての一次資料(日本証券業協会の資料は式が画像で、二次情報のみ)。
- プレミアムの受渡日を選んで割り引くか(後回し。今は割り引かない)。
- 銘柄コードの記号(JS・JL・JX・JU)はユーザー未確認。
- 引け値の履歴はバッチを回した日の分だけ溜まる(過去日の評価日には、その日までに取り込んだ引け値を使う)。
