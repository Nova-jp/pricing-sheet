"""
分析ブック(SwapAnalysis.xlsm)のFwdMonitorシートのUDF。Fwd(NyMy)の監視とヒストリカルの比較。

■ 設計方針(VolHV・PCAのUDFと同じ)
  - QuantLibが要る計算(各日のFwdの水準と同じ契約の日次変化、ライブのFwd)とDBの読み出しだけをUDFにし、
    カーブ・フライ・変化・σ・Z値はExcelの数式で計算する。窓や比較日を変えてもUDFは再計算されない。
  - 計算方法は「手動」+F9。FwdHistoryは非volatile。配列はそのまま返してスピルに任せる。
  - 3方式は1回の計算でまとめて求め(カーブの読み出しを共有)、引数と基準日・DBの更新時刻ごとに
    プロセス内で覚えておく。FwdHistoryを方式ごとに3回呼んでも計算は1回。
"""

from functools import lru_cache
from typing import List, Tuple

import xlwings as xw

from engine.common import udf_common
from engine.common.historical_store import HistoricalStore
from engine.common.market import release_curves
from engine.common.swap_pricer import par_rate
from engine.common.udf_common import _is_blank
from engine.fwd import history


def _labels(values) -> Tuple[str, ...]:
    names = tuple(history.label_name(v) for v in (values or []) if not _is_blank(v))
    if not names:
        raise ValueError("銘柄を1つ以上指定してください(例 5y5y)")
    return names


@lru_cache(maxsize=4)
def _compute_cached(labels: Tuple[str, ...], base_date, db_path: str, db_key: int) -> history.FwdHistory:
    release_curves()  # 評価日を日付ごとに切り替えるので、生きているカーブを手放す(market.release_curves)
    store = HistoricalStore(db_path)
    with store:
        return history.compute(store, labels, base_date)


@xw.func
@xw.arg("labels", ndim=1)
def FwdHistory(labels, mode, base_date=None) -> List[list]:
    """
    銘柄(NyMy、例 5y5y)ごとのFwdのヒストリカル。
    見出し行 [Date, 銘柄1, …, 銘柄n, 銘柄1 dF, …, 銘柄n dF] + 日付ごとの行(昇順)。
      銘柄: その日の取引のFwdスワップレート(%)。取引の決め方は mode(CMT/FIXED/ROLLING。engine/fwd/history.py)
      dF  : 同じ取引の、前の日付のカーブからの変化(bp)。ロールダウンを含まない(σ・Z値に使う)
    base_date: FIXED・ROLLINGの基準日(空欄=今日)。この日より後の日付は返さない。
    行はカーブのある日付(最初の日付は変化が無いので含めない)。
    """
    names = _labels(labels)
    mode = history.parse_mode(mode)
    base = udf_common._resolve_valuation_date(base_date)
    store = udf_common._historical_store()
    series = _compute_cached(names, base, str(store.db_path), store.db_path.stat().st_mtime_ns)
    rows: List[list] = [["Date"] + list(series.labels) + [f"{name} dF" for name in series.labels]]
    for d, level, change in zip(series.dates, series.level[mode], series.change[mode]):
        rows.append([d] + level + change)
    return rows


@xw.func
@xw.arg("labels", ndim=1)
@xw.arg("real_time_range", ndim=2)
def FwdLive(labels, real_time_range) -> List[list]:
    """
    ライブのカーブ(今日)でのFwdスワップレート(%、labels と同じ並びの縦1列。空欄の行は空欄を返すので、
    銘柄の表に区切りの空行があっても行がずれない)。取引は今日のスポット日+N年からM年で、
    FwdHistoryの基準日を空欄(=今日)にしたときの3方式の取引と同じ。
    real_time_range: フラグ/Ticker/Value の範囲(シートのカーブ用の範囲の TAKE(…#,,3)。pricingと同じ引き方)。
    """
    cells = list(labels or [])
    names = _labels(cells)
    market = udf_common._market(real_time_range, None).base
    rates = iter([par_rate(market, t).target_fixrate for t in history.live_terms(names, market.valuation_date)])
    return [[""] if _is_blank(v) else [next(rates)] for v in cells]
