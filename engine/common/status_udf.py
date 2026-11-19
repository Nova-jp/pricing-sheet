"""
評価日(取引日)とDBの状態の表示。全ブック共通で、各ブックの同名の.pyが再エクスポートする
(各ブックの先頭のControlシートに同じ形で置く。docs/decisions.md の「評価日」)。

評価日は値としてセルに入れ(空欄=今日はUDFが今日として計算するが、Excelからは日付の変化が見えない)、
UDFはすべて評価日のセルを引数で受け取る。日付を入れ直したときだけ下流が1回計算される。
DataStatus は、その前提が崩れていないかを1セルで見せる。

  Controlシート:
    B3 Trade date(入力、毎朝Ctrl+;)
    B4 =ValuationDate(B3)        名前 TradeDate。空欄なら今日(非volatile)
    B5 =DbStamp(B3)              評価日を入れたときのDBの朝バッチの実行時刻(非volatile)
    B6 =DataStatus(B3, B5)       volatile。"OK" 以外は条件付き書式で赤

  DbStamp は評価日のセルの下流なので、評価日を入れ直したときだけ計算され、そのときDBにあった最後の実行の時刻を
  覚える。評価日に依存する他のUDFも同じときに計算されるので、DataStatus がDBの今の最後の実行の時刻と比べて
  違えば、評価日を入れた後に朝バッチを流した(=計算が古いDBのまま)と分かる。データの日付で判定しないのは、
  最新であるべき日付を営業日のカレンダーで推し量らずに済み、途中で失敗した実行も見分けられるため。
  実行の記録は朝バッチ(engine/common/morning_batch.run)がDBの batch_runs に書く。

  DataStatus は volatile(F9のたびに今日・DBと比べ直す)。下流を持たせない(このセルを参照する式を作らない)。
  文字列は英語(ブックのセルは英語)。

  旧形式 DataStatus(評価日, 計算に使ったDBの日付, "curve"/"jgb")(BondOption・PositionSheetが使っている)は、
  各ブックがControlシートへ移るまで残す(docs/roadmap.md の 24)。
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

_DATA = ("curve", "jgb")


def _db_last_date(store, data: str, before: datetime.date):
    """評価日より前で、DBにデータがある最後の日付(旧形式用。DBは昨日までの引けなので、評価日=今日ならDBの最新)。"""
    if data == "jgb":
        return store.jgb_closes_last_date(before - datetime.timedelta(days=1))
    dates = [d for d in store.dates() if d < before]
    return dates[-1] if dates else None


def _legacy_status(entered: datetime.date, today: datetime.date, data_date, data: str) -> str:
    data = str(data).strip().lower()
    if data not in _DATA:
        raise ValueError(f"data は {'/'.join(_DATA)} のどれかです: {data}")
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
    return NOT_TODAY if entered < today else OK


def _stamp_status(entered: datetime.date, today: datetime.date, stamp) -> str:
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


def status(valuation_date, stamp=None, data=None) -> str:
    """DataStatus の本体(テスト用に xlwings から切り離す)。data を渡したときは旧形式(stamp は計算に使ったDBの日付)。"""
    entered = udf_common._to_date(valuation_date)
    today = udf_common._today()
    if entered is None:
        return BLANK
    if entered > today:
        return FUTURE
    if not udf_common._is_blank(data):
        return _legacy_status(entered, today, stamp, data)
    return _stamp_status(entered, today, stamp)


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
    """評価日(取引日)。空欄=今日(非volatile。空欄のまま日をまたいでも計算し直されないので、評価日のセルには
    日付を入れる。空欄・今日でない等は DataStatus が赤で知らせる)。ControlシートのB4、名前 TradeDate。"""
    return udf_common._resolve_valuation_date(valuation_date)


@xw.func
def DbStamp(valuation_date=None) -> str:
    """評価日を入れたときの、DBの最後の朝バッチの実行時刻(文字列。記録が無ければ空欄)。
    valuation_date は値を使わず、評価日のセルの下流にするためだけに受け取る(非volatile。入れ直したときだけ計算される)。"""
    return db_stamp()


@xw.func(volatile=True)
def DataStatus(valuation_date=None, stamp=None, data=None) -> str:
    """計算の状態。問題が無ければ "OK"、あれば理由(英語)を返す。
    評価日(入力のセル)が空欄・未来・今日でない、DBに今日の朝バッチの実行が無い・最後の実行が失敗、
    DbStamp(評価日を入れたときの実行時刻)がDBの最後の実行と違う(評価日を入れた後に朝バッチを流した)。
    data("curve"/"jgb")を渡すと旧形式(stamp の位置に計算に使ったDBの日付を渡し、DBにそれより新しいデータがあるかを見る)。
    volatile。このセルを参照する式は作らない(下流があると毎回計算される)。"""
    return status(valuation_date, stamp, data)
