# UDFの作り方(全ブック共通)

新しいUDFを作るとき・既存のUDFの引数を変えるときに守ること。ブックに固有の決まりは各ブックのドキュメントに、
理由と採らなかった案は[decisions.md](decisions.md)に書く(ここには決まりだけを書く)。

## UDFにするもの・しないもの

- UDFにするのはQuantLibが要る計算とDBの読み出しだけ。スプレッド・フライ・合計・並べ替え・窓の集計・回帰のような四則演算と
  表の操作はExcelの数式に任せ、スピル参照(`B1#`)でUDFの出力の大きさに追随する。高度な計算が要りそうなら、
  勝手にUDF化・複雑化せず先に相談する。
- 表全体・複数ケースは1回のUDFで返す(再計算の時間はほぼUDFのセル数で決まる。例 `PricingTable`・`PcaGrid`)。
- VBA・RunPythonでUDFのセルを静的な値で上書きする実装は作らない(例外はPositionSheetの操作のボタンだけ。[position.md](position.md))。
- 価格の値はQuantLibを正とし、手計算の再実装で検証しない。

## 評価日(Controlシートの`TradeDate`)

各ブックの先頭のControlシート: B3 `Trade date`(入力、毎朝Ctrl+;)・B4 `=ValuationDate(B3)`(名前`TradeDate`)・
B5 `=DbStamp(B3)`・B6 `=DataStatus(B3,B5)`(`OK`以外は赤)。UDFは`engine/common/status_udf.py`。新しいブックにも同じ形で置く。

- **今日・今の時刻・DB・ライブのレートで結果が変わるUDFは、日付を引数で受け取り、シートでは`TradeDate`(またはそれを寄せた名前)を
  渡す**。UDFの中で黙って`_today()`やDBを見ない(日付を入れ直したときに計算し直されなくなる)。日付の引数は末尾に置く。
  - 日付で結果が変わらないUDF(日付の表の計算、入力だけで決まる計算等)は受け取らなくてよい。例外として作るときは、
    docstringに「日付に依存しない」理由を書く。
- **日付の引数は必須で、空欄・未来日はエラー**(`udf_common._required_date`)。UDFの中で空欄を今日やDBの最新日で補わない。
  - シートの任意の日付の入力欄は、入力セルの名前を`…In`にし、UDFには`IF(…In="",TradeDate,…In)`の名前を渡す。
- **ライブのレートを使うUDFは`udf_common._market(rates, TradeDate)`**。今日ならライブ、過去日ならDBのその日の引け。
  例外: LiveScreenは今日だけ(過去日はエラー。[live_screen.md](live_screen.md))。
- **ヒストリカルを返すUDFは評価日より後の日付を返さない**。期間は`udf_common._history_range(from, to, TradeDate)`
  (終了日の空欄・評価日より後は評価日まで、開始日の空欄はDBの最初から)。
- **オプションの残りの期間は`udf_common._valuation_moment(TradeDate)`**(今日なら今の時刻、過去日はその日の15時)。
- **volatileにするのは、`DataStatus`、フィードを読む`LiveQuotes`、今日の時刻で値が変わるもの
  (`OptionTermTable`・`OptionMarketTable`・`PosVolCalendarFactor`)だけ**。評価日や日付のセルをvolatileにしない。
  `TODAY()`・`NOW()`も式に使わない(LiveScreenの「Calculated」の表示だけは例外)。

## エラーの出し方

- 取引の表(行ごとに独立した取引。pricing表等)は、1行の誤りで表全体をエラーにしない。その行を空欄にし、message列に理由を出す。
- 分析ブック(SwapAnalysis)は、計算できないところがあれば全体をエラーにする。理由にケース番号・テナー等を付ける。
  シートの`IFERROR`・`ISNUMBER`でエラーを隠さない(グラフで点を飛ばす`IFERROR(…,NA())`だけは可)。
- エラーの文字列や空欄を値の代わりに返さない。空欄を返すのは、入力の無い行と値がもともと無い日だけ。

## Pythonの書き方

- ブックごとのパッケージ(`engine/<ブック>/udf.py`)に書き、ワークブックと同名の`.py`(`PricingSheet.py`等)は再エクスポートだけ。
  分析ブック等は`common/`だけに依存させ、ブック同士を参照させない([decisions.md](decisions.md)の「パッケージ構成」)。
  新しいUDFのモジュールは`tools/template.py`の`UDF_MODULES`に加える(配列UDFの検査と引数名のテストが読む)。
- 共有の部品は`engine/common/udf_common.py`を`udf_common.xxx(...)`の属性参照で呼ぶ(`from … import`で名前をコピーしない。
  テストの`mock.patch.object`が効かなくなる)。
- 配列は`List[list]`をそのまま返す。`@xw.ret(expand="table")`を使わない。
- 引数名にVBAの予約語(`end`・`date`等)を使わない(`tests/test_udf_arg_names.py`)。
- ブックに渡す・ブックから受け取る文字列(選択肢・見出し・定義名・エラー以外の返り値)は英語。docstring・コメントは日本語。

## シートに置くとき

- 数式をCOMで書くときは`.Formula2`(`.Formula`だと旧式の配列数式になり、配列UDFが1セルしか返らない)。
- 引数を増やした・名前を変えたら、Excelで「Restart UDF Server」→「Import Functions」(VBAのラッパーが作り直される)。
- ブックの構成を変えたら`python -m tools.template`で雛形を作り、`--diff`で意図した変更だけかを確かめる(CLAUDE.mdの手順)。
