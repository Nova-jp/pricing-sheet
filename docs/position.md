# ポジション・リスク管理(PositionSheet.xlsm)

会社のリスク(前日の引けの値)と、今日の取引のリスク(ライブのカーブでこちらが計算)を並べて、デルタ・ガンマ・
ベガを管理するブック。2026-11-01に一から作り直した(2026-10-25の叩き台のGrid・PCA・Forward・Live等のシートは
削除した。下の「前の叩き台」)。

コード: `engine/position/`(`common/`だけに依存)、`PositionSheet.py`(再エクスポート)。
テスト: `tests/test_position.py`。

## 方針(ユーザーと決定、2026-11-01)

- **会社のデータは生シートに丸ごと貼る**: 会社のファイル(デルタ・ガンマのシートと、ベガのシート)を
  `Raw_IR`・`Raw_IRVOL`のA1にそのまま貼る。値は前営業日(ファイルを作った日)の引けと、その前の営業日の2組。
  会社の列名・dataset名・基準日(SODの黄色のセル)は雛形では消す(`tools/template.py`の`RECEIVE_AREAS`の範囲指定。
  会社のデータの一部なのでPublicのリポジトリに入れない)。
- **項目の名前・順番が変わっても直すのはSODシートだけ**: SODシートの対応表で「こちらの項目名(固定)→生シートの
  列名(黄色の入力セル、プルダウン)」を持ち、列は見出しの名前で探す。管理シート(Delta・Gamma・Vega)は
  SODの項目名だけを見るので、会社のファイルの形が変わっても触らない。
- **デルタの合計 = derivative + bond**(XVAは含まない。ユーザー確認)。Delta・GammaのCheck列で会社の
  `Total`との差を出す。
- **グリッドのピラーはマーケットの基準(ライブのカーブの34本)**。日中の取引を同じピラーで測るため。会社のピラーに
  無い月(4M・5M・15M等)の`Original`の列は空欄。テナーは年数に直して突き合わせる(会社の`1Y`=こちらの`12M`、
  `1.5Y`=`18M`)。
- **VBAは貼り付けのような操作だけ**。四則演算を超える処理(ピラー名の変換・集計・リスク計算)はExcelの数式か
  Python。叩き台では貼り付けは手で行う(VBAのボタンは必要になったら作る)。
- 日中の取引は再計算の負担を抑えるため、Tradesシートで表全体を1回のUDFで計算する(同じ入力なら前の結果を返す)。
  計算するのはF9のとき(手動計算)。

## ブックの構成

| シート | 中身 |
|---|---|
| Delta | ピラー × Original(Swap・Bond・Total)/ Intraday(Done)/ Total / What-if / Check。単位は百万円/bp |
| Gamma | 同じ形(百万円/bp²) |
| Vega | 満期 × 原資産のOriginalのグリッド(行・列の合計)。日中のスワップションは未対応 |
| Trades | 今日の取引の入力(Status・Product・Start・Tenor/End・Code・Notional・Side・Rate)と、行ごとの要約、ピラーごとの合計(Done・What-if) |
| SOD | 基準日、dataset・キーの列名、項目の対応表(状態・生の合計・グリッドの合計・差)、チェック、抽出の表(ピラー × 項目) |
| Raw_IR / Raw_IRVOL | 会社のシートの貼り付け先(全行が雛形で消える受信領域) |
| Setup | 評価日、Live/Fixed、ピラー(34本)、ライブのレート・vol、カーブの範囲 |

### SODシートのチェック

- 列名が見つからない → `NOT FOUND`(赤)。黙って空欄・0にはしない。
- グリッドの外の行(こちらのピラーに無いテナー、読めないテナー)と、重複したテナーを一覧で出す
  (サンプルでは`25Y`が2行あった。XLOOKUP・XMATCHは最初の行だけを使う)。
- 項目ごとに「生シートの合計」と「グリッドの合計」の差を出す(0でなければ赤)。

### 名前付きの数式(LAMBDA)

| 名前 | 中身 |
|---|---|
| `POS_Years` | テナーの表記 → 年数(O/N=1/365、W・M・Y・D)。突き合わせのキー |
| `POS_Col` | 生シートの列を見出しの名前で取る |
| `POS_IRKeys` / `POS_VolKeys` | 生シートの行ごとのキー(対象のdatasetの行だけ。ベガは「満期の年数x原資産の年数」) |
| `POS_PickIR` / `POS_PickVol` | ピラー × 列名、満期 × 原資産の値(無ければ空欄) |
| `POS_RawName` | こちらの項目名 → 生シートの列名(SODの対応表) |
| `POS_Sod` | SODの抽出の表から項目の列を取る(Delta・Gammaが使う) |
| `POS_IntradayDone` / `POS_IntradayWhatIf` | Tradesシートのピラーごとの合計 |

## 日中の取引(`engine/position/intraday.py`)

- OIS: 標準のOIS(`trades.py`の規約)。Start空欄=スポット、Tenor/Endは`10Y`か満期日、SideはPay/Rec、Rate空欄=パー。
- 現物(Bond): `bonds.py`と同じZスプレッド固定のキーレート・デルタ。SideはBuy/Sell(買い=ロング)、Rateは約定の
  単利(空欄=DBの引け値)。
- デルタ: ピラーを1本ずつ+0.1bp動かしたPVの差を1bpあたりに(PricingSheetのRiskGridと同じ)。
- ガンマ: 同じピラーを±0.1bp動かした2階の中心差分(対角のガンマ、百万円/bp²)。1bpで測ると、Convex Monotone補間の
  単調性の調整がバンプで効いたり外れたりして、ピラーの間にキャッシュフローがある現物のガンマが桁違いに崩れる
  (実DBの残存8年の現物で、0.1bpなら8Yのガンマ−0.0004、1bpなら−1.3)。スワップは0.1bpでも1bpでも一致する。

| UDF | 中身 |
|---|---|
| `PosIntradayRisk` | 行ごとの [Rate, PV, DV01, Gamma, Message]。エラーの行はMessageに理由を出し、他の行は続ける |
| `PosIntradayGrid` | ピラーごとの [Delta Swap, Delta Bond, Gamma Swap, Gamma Bond]。statusでDone / What-if / Allを選ぶ |

## 前提・未確認

- 会社のガンマの定義(対角か、パラレルに1bp動いたときのピラーごとのデルタの変化か)・単位・符号。こちらの日中の
  ガンマは対角(現物ではパラレルとかなり違う。実DBの残存8年の現物で、対角の和+0.004、パラレル+0.006)。
- 会社のファイルの日中の取引の列(前営業日の日中の取引)が、合計の列に含まれているか。今は合計・derivative・bondの列だけを使う。
- 会社のベガの単位と、満期・原資産の範囲(サンプルは5Yx10Yの1行だけ。グリッドは満期13本×原資産12本)。
- 生シートのレート(前営業日の引け、SODの`Rate`)とライブのレートの差から、SODのリスクの今の推定(デルタ+ガンマ×Δr)と
  損益の目安を出す列は、レートの単位を確かめてから入れる。

## 前の叩き台(2026-10-25)

Grid・PCA・Forward・Live・Bonds・Othersのシートは作り直しで削除した。UDF(`PosBondRisk`・`PosTradeRisk`・
`PosForwardJacobian`・`PosPca*`・`PosClose*`)とその計算(`bonds.py`・`trades.py`・`forward.py`)は残してあり、
現物・OISの計算は`intraday.py`が使う。PCAのファクター・リスク、フォワードのリスク、損益の説明の考え方は
[decisions.md](decisions.md)の「ポジション・リスク管理」に残っている。必要になったらシートを戻す。
