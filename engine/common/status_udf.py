"""
評価日(取引日)とDBの状態の表示。全ブック共通で、各ブックの同名の.pyが再エクスポートする
(各ブックの先頭のControlシートに同じ形で置く。docs/decisions.md の「評価日」)。

評価日は値としてセルに入れ(必須。空欄・未来日は ValuationDate がエラーになり、DataStatus が理由を出す)、
UDFはすべて評価日のセルを引数で受け取る。日付を入れ直したときだけ下流が1回計算される。
DataStatus は、その前提が崩れていないかを1セルで見せる。

  Controlシート:
    B3 Trade date(入力、毎朝Ctrl+;)
    B4 =ValuationDate(B3)        名前 TradeDate。空欄・未来日はエラー(非volatile)
    B5 =DbStamp(B3)              評価日を入れたときのDBの朝バッチの実行時刻(非volatile)
    B6 =DataStatus(B3, B5)       volatile。"OK" 以外は条件付き書式で赤

  DbStamp は評価日のセルの下流なので、評価日を入れ直したときだけ計算され、そのときDBにあった最後の実行の時刻を
  覚える。評価日に依存する他のUDFも同じときに計算されるので、DataStatus がDBの今の最後の実行の時刻と比べて
  違えば、評価日を入れた後に朝バッチを流した(=計算が古いDBのまま)と分かる。データの日付で判定しないのは、
  最新であるべき日付を営業日のカレンダーで推し量らずに済み、途中で失敗した実行も見分けられるため。
  実行の記録は朝バッチ(engine/common/morning_batch.run)がDBの batch_runs に書く。

  DataStatus は volatile(F9のたびに今日・DBと比べ直す)。下流を持たせない(このセルを参照する式を作らない)。
  文字列は英語(ブックのセルは英語)。
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
NO_RUN_TODAY = "Run morning batch (no run today)"
BATCH_FAILED = "Morning batch failed: re-run"

def status(valuation_date, stamp=None) -> str:
    """DataStatus の本体(テスト用に xlwings から切り離す)。"""
    entered = udf_common._to_date(valuation_date)
    today = udf_common._today()
    if entered is None:
        return BLANK
    if entered > today:
        return FUTURE
    try:
        store = udf_common._historical_store()
    except ValueError:
        return NO_DB
    with store:
        last = store.last_batch_run()
    if last is None:
        return NO_RUN_TODAY
    if not last.ok:
        return BATCH_FAILED
    if datetime.date.fromisoformat(last.finished_at[:10]) < today:
        return NO_RUN_TODAY
    if not udf_common._is_blank(stamp) and str(stamp).strip() != last.finished_at:
        return NEWER
    return NOT_TODAY if entered < today else OK


def db_stamp() -> str:
    """DBの最後の朝バッチの実行時刻(YYYY-MM-DD HH:MM:SS)。DB・記録が無ければ空文字。"""
    try:
        store = udf_common._historical_store()
    except ValueError:
        return ""
    with store:
        last = store.last_batch_run()
    return last.finished_at if last else ""


@xw.func
def ValuationDate(valuation_date=None):
    """評価日(取引日)。ControlシートのB4、名前 TradeDate(非volatile)。空欄・未来日はエラー(空欄を今日で補わない。
    空欄=今日だと日をまたいでも計算し直されず、前の日付が残る)。理由は DataStatus が赤で知らせる。
    xlwingsはエラーのセルを None で渡すので、TradeDate を受け取る下流のUDFも空欄のエラーになる。"""
    return udf_common._required_date(valuation_date)


@xw.func
def DbStamp(valuation_date=None) -> str:
    """評価日を入れたときの、DBの最後の朝バッチの実行時刻(文字列。記録が無ければ空欄)。
    valuation_date は値を使わず、評価日のセルの下流にするためだけに受け取る(非volatile。入れ直したときだけ計算される)。"""
    return db_stamp()


@xw.func(volatile=True)
def DataStatus(valuation_date=None, stamp=None) -> str:
    """計算の状態。問題が無ければ "OK"、あれば理由(英語)を返す。
    評価日(入力のセル)が空欄・未来・今日でない、DBに今日の朝バッチの実行が無い・最後の実行が失敗、
    DbStamp(評価日を入れたときの実行時刻)がDBの最後の実行と違う(評価日を入れた後に朝バッチを流した)。
    volatile。このセルを参照する式は作らない(下流があると毎回計算される)。"""
    return status(valuation_date, stamp)
