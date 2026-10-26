"""
分析ブック(SwapAnalysis.xlsm)のVolHV・VolLevelシートのUDF。インプライドとヒストリカルのボラの比較(VolHV)、
ボラの水準とFwdの水準の散布図による割高・割安(VolLevel。回帰・残差はExcelの数式)。

■ 設計方針(PCAのUDFと同じ)
  - QuantLibが要る計算(Fwdの日次変化)とDBの読み出しだけをUDFにし、窓の集計(2乗の累積の差)・
    年率換算・ボラの差(カーブ・フライ)・パーセンタイルはExcelの数式で計算する。
    窓・年率の日数を変えてもUDFは再計算されない。
  - 計算方法は「手動」+F9。UDFは非volatile。配列はそのまま返してスピルに任せる。
  - 結果は引数とDBの更新時刻ごとにプロセス内で覚えておく(朝のバッチでDBが変われば計算し直す)。
"""

from functools import lru_cache
from typing import List, Optional, Tuple

import xlwings as xw

from engine.common import udf_common
from engine.common.historical_store import HistoricalStore
from engine.common.swap_pricer import par_rate
from engine.common.udf_common import _is_blank
from engine.vol import hv


def _labels(values) -> Tuple[str, ...]:
    return tuple(str(v).strip() for v in (values or []) if not _is_blank(v))


@lru_cache(maxsize=8)
def _compute_cached(labels: Tuple[str, ...], db_path: str, db_key: int) -> hv.VolSeries:
    store = HistoricalStore(db_path)
    with store:
        return hv.compute(store, store.vol_quotes(), labels)


@xw.func
@xw.arg("labels", ndim=1)
def VolHistory(labels) -> List[list]:
    """
    銘柄(「満期x原資産」、例 1Yx10Y)ごとのIVとFwdの日次変化。
    見出し行 [Date, 銘柄1 IV, 銘柄1 Fwd, 銘柄1 dF, 銘柄2 IV, …] + 日付ごとの行(昇順)。
      IV : DBのスワップションATMノーマルvol(bp/年)。DBに無い日は空文字(Excelで="" と判定できる)
      Fwd: その日の起算日(スポット日+満期)・原資産のFwdスワップレート(%)
      dF : 同じFwdの、前の日付のカーブからの変化(bp)。ロールダウンを含まない
    行はカーブのある日付(最初の日付は変化が無いので含めない)。
    """
    names = _labels(labels)
    if not names:
        raise ValueError("銘柄を1つ以上指定してください(例 1Yx10Y)")
    store = udf_common._historical_store()
    series = _compute_cached(names, str(store.db_path), store.db_path.stat().st_mtime_ns)
    header = ["Date"] + [f"{name} {col}" for name in series.labels for col in ("IV", "Fwd", "dF")]
    rows: List[list] = [header]
    for d, iv, fwd, dfwd in zip(series.dates, series.iv, series.fwd, series.dfwd):
        row: list = [d]
        for k in range(len(series.labels)):
            row += ["" if iv[k] is None else iv[k], fwd[k], dfwd[k]]
        rows.append(row)
    return rows


@xw.func
@xw.arg("labels", ndim=1)
@xw.arg("real_time_range", ndim=2)
def VolFwdLive(labels, real_time_range) -> List[list]:
    """
    ライブのカーブ(今日)での、銘柄(「満期x原資産」)の原資産のFwdスワップレート(%、labels と同じ並びの縦1列。
    空欄の行は空欄を返す)。契約は今日のスポット日+満期からの原資産で、VolHistoryのFwdの各日の決め方と同じ。
    real_time_range: フラグ/Ticker/Value の範囲(シートのカーブ用の範囲の TAKE(…#,,3)。pricingと同じ引き方)。
    """
    cells = list(labels or [])
    names = _labels(cells)
    if not names:
        raise ValueError("銘柄を1つ以上指定してください(例 1Yx10Y)")
    market = udf_common._market(real_time_range, None).base
    rates = iter([par_rate(market, t).target_fixrate for t in hv.live_terms(names, market.valuation_date)])
    return [[""] if _is_blank(v) else [next(rates)] for v in cells]
