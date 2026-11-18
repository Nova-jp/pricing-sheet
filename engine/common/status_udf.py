"""
計算の状態の表示(DataStatus)。全ブック共通で、各ブックの同名の.pyが再エクスポートする。

評価日は値としてセルに入れ(空欄=今日はUDFが今日として計算するが、Excelからは日付の変化が見えない)、
UDFはすべて評価日のセルを引数で受け取る。日付を入れ直したときだけ下流が1回計算される
(docs/decisions.md の「評価日」)。このUDFは、その前提が崩れていないかを1セルで見せる。

  DataStatus(評価日, 計算に使ったDBの日付, データ):
    - volatile(F9のたびに今日・DBと比べ直す)。下流を持たせない(このセルを参照する式を作らない)。
      下流が無いので、毎回呼ばれても文字列を1つ返すだけで済む
    - "OK" 以外はシートの条件付き書式で赤くする。文字列は英語(ブックのセルは英語)
"""

import datetime

import xlwings as xw

from engine.common import udf_common

OK = "OK"
BLANK = "Enter trade date (Ctrl+;)"
NOT_TODAY = "Trade date is not today"
FUTURE = "Trade date is in the future"
NEWER = "DB has newer data: re-enter trade date"
NO_DB = "No historical DB"

_DATA = ("curve", "jgb")


def _db_last_date(store, data: str, before: datetime.date):
    """評価日より前で、DBにデータがある最後の日付(DBは昨日までの引けなので、評価日=今日ならDBの最新)。
    評価日の当日の引けを使う計算(過去の評価日のJgbMaster等)は、使った日付がこれ以上になるので赤くならない。"""
    if data == "jgb":
        return store.jgb_closes_last_date(before - datetime.timedelta(days=1))
    dates = [d for d in store.dates() if d < before]
    return dates[-1] if dates else None


def status(valuation_date, data_date=None, data: str = "curve") -> str:
    """DataStatus の本体(テスト用に xlwings から切り離す)。"""
    data = str(data or "curve").strip().lower()
    if data not in _DATA:
        raise ValueError(f"data は {'/'.join(_DATA)} のどれかです: {data}")
    entered = udf_common._to_date(valuation_date)
    today = udf_common._today()
    if entered is None:
        return BLANK
    if entered > today:
        return FUTURE
    if not udf_common._is_blank(data_date):
        used = udf_common._to_date(data_date)
        try:
            store = udf_common._historical_store()
        except ValueError:
            return NO_DB
        with store:
            latest = _db_last_date(store, data, entered)
        if latest is not None and (used is None or used < latest):
            return NEWER
    if entered < today:
        return NOT_TODAY
    return OK


@xw.func(volatile=True)
def DataStatus(valuation_date=None, data_date=None, data="curve") -> str:
    """計算の状態。評価日(入力のセル)が空欄・今日でない・未来、または評価日より前で計算に使ったDBの日付(data_date)より
    新しい日付のデータがDBにある(評価日を入れた後に朝バッチを流した)なら、その理由を返す。問題が無ければ "OK"。
    data: data_date と比べるDBのデータ。"curve"(日次カーブ、既定)/ "jgb"(JGBの引け値)。
    volatile。このセルを参照する式は作らない(下流があると毎回計算される)。"""
    return status(valuation_date, data_date, data)
