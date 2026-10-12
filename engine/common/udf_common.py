"""
UDFの各機能(pricing_udf.py・historical/udf.py・boj/udf.py)が共有する、Excel境界の
型変換と評価日時点のマーケット解決。機能固有のUDF定義はここには置かない。

■ テストでの差し替え方
  各feature udfモジュールはここの関数を`udf_common.xxx(...)`の形(モジュール越しの属性参照)
  で呼ぶ。`from engine.common.udf_common import xxx`のように名前をコピーすると、コピー先の
  モジュールの中では別の束縛になり、`mock.patch.object(udf_common, "xxx", ...)`で
  差し替えても反映されない(パッチが効くのは参照した側、ではなく定義側のモジュール
  オブジェクトの属性のため)。`udf_common.xxx(...)`の形にしておけば、テストはこの
  モジュールだけパッチすればどのfeatureのUDFからの呼び出しにも効く。
"""

import datetime
from typing import Dict, Optional

from engine.common.conventions import imm_code_to_date, is_imm_code
from engine.common.historical_store import HistoricalStore
from engine.common.market import CurveSet, curve_set, db_fixings, db_snapshot, realtime_snapshot

_EXCEL_EPOCH = datetime.date(1899, 12, 30)  # Excelのシリアル値1900-01-01=1、1900うるう年バグ込み


def _to_date(value) -> Optional[datetime.date]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    # Excelがセル参照でなく TODAY() や日付書式セルを引数で渡すと、xlwings経由で
    # datetimeではなくシリアル値(数値)で届くことがある。その場合はここで日付に変換する。
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _EXCEL_EPOCH + datetime.timedelta(days=int(value))
    # start/end列にはIMMコード(M27等)も入力できる。ロール(STD/IMM等)はroll conv列の
    # 指定にそのまま従う(両方IMMコードなら既定でIMMになるのはExcel側のP列数式の役割)。
    if is_imm_code(value):
        return imm_code_to_date(value)
    raise ValueError(
        f"日付として解釈できません: {value!r}(IMMコードは月記号H/M/U/Z+2桁の年、例: M27。"
        "startにはテナー(例: 10y)も入力できる)"
    )


def _is_blank(value) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def _flag_excludes(flag) -> bool:
    """RealTime B列の採否フラグ判定。空欄・None・非数値は「使う」、明示的な 0 のみ除外。"""
    if flag is None or flag == "":
        return False
    try:
        return float(flag) == 0.0
    except (TypeError, ValueError):
        return False


def _realtime_rows(real_time_range):
    """RealTimeレンジ → [(Ticker, Value, 使う?)]。3列=[フラグ, Ticker, Value]、
    2列=[Ticker, Value](後方互換。フラグなし=全行使う)。Tickerが空の行は除く。"""
    rows = []
    for row in real_time_range or []:
        if row is None:
            continue
        flag, ticker, value = (row[0], row[1], row[2]) if len(row) >= 3 else (None, row[0], row[1])
        if _is_blank(ticker):
            continue
        rows.append((str(ticker).strip(), value, not _flag_excludes(flag)))
    return rows


def _rates_from_range(real_time_range) -> Dict[str, float]:
    """RealTimeレンジ → ブートストラップ入力辞書(フラグ≠0で値のあるテナー、ラベルは小文字)。
    「どのスポットレートをカーブに使うか」を決めるのはこの関数(過去日は _market で同じフラグを使う)。"""
    return {
        ticker.lower(): float(value)
        for ticker, value, use in _realtime_rows(real_time_range)
        if use and not _is_blank(value)
    }


def _today() -> datetime.date:
    """今日の日付(テストで差し替えられるように1か所にまとめる)。"""
    return datetime.date.today()


def _resolve_valuation_date(valuation_date) -> datetime.date:
    d = _to_date(valuation_date)
    return d if d is not None else _today()


def _historical_store() -> HistoricalStore:
    """ヒストリカルの日次カーブDB(朝バッチ engine/common/morning_batch.py が作成)。
    with 文で使うと、計算中はDB接続を1本だけ開いて使い回す。"""
    store = HistoricalStore()
    if not store.exists():
        raise ValueError(
            "ヒストリカルカーブDBがありません。historical_data.xlsxを更新・保存してから "
            "python -m engine.common.morning_batch を実行してください"
        )
    return store


def _db_fixings() -> Dict[datetime.date, float]:
    """TONA実績(過去起算の計算用)。ヒストリカルDBから読み、DBが無ければ空にする(スポット起点の
    計算はDBなしでも動き、過去起算だけが「実績が不足」のエラーになる)。"""
    try:
        store = _historical_store()
    except ValueError:
        return {}
    return db_fixings(store)


def _market(real_time_range, valuation_date) -> CurveSet:
    """評価日のカーブ一式。今日ならRealTime、過去日ならヒストリカルDB(同じフラグでテナーを選ぶ)。"""
    valuation_date = _resolve_valuation_date(valuation_date)
    today = _today()
    if valuation_date > today:
        raise ValueError(f"評価日が未来日です: {valuation_date}")
    if valuation_date == today:
        snapshot = realtime_snapshot(valuation_date, _rates_from_range(real_time_range), _db_fixings())
    else:
        labels = [t for t, _, use in _realtime_rows(real_time_range) if use]
        with _historical_store() as store:
            snapshot = db_snapshot(valuation_date, labels, store)
    return curve_set(snapshot)
