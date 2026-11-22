"""
分析ブック(SwapAnalysis.xlsm)のVolHV・VolLevelシートのUDF。インプライドとヒストリカルのボラの比較(VolHV)、
ボラの水準とFwdの水準の散布図による割高・割安(VolLevel。回帰・残差はExcelの数式)。

■ 設計方針(PCAのUDFと同じ)
  - DBの読み出し(原資産のFwdとその日次変化は朝バッチが swaption_forwards に計算済み)と、ライブのFwd
    (QuantLib)だけをUDFにし、窓の集計(2乗の累積の差)・
    年率換算・ボラの差(カーブ・フライ)・パーセンタイルはExcelの数式で計算する。
    窓・年率の日数を変えてもUDFは再計算されない。
  - 計算方法は「手動」+F9。UDFは非volatile。配列はそのまま返してスピルに任せる。
  - 結果は引数とDBの版ごとにプロセス内で覚えておく(朝のバッチでDBが変われば計算し直す)。
  - 日付は引数で受け取り、空欄はエラー(UDFの中で今日を決めない。docs/decisions.md の「評価日」)。
"""

from functools import lru_cache
from typing import List, Tuple

import xlwings as xw

from engine.common import udf_common
from engine.common.historical_store import HistoricalStore
from engine.common.swap_pricer import par_rate
from engine.common.udf_common import _is_blank
from engine.vol import hv


@lru_cache(maxsize=8)
def _compute_cached(labels: Tuple[str, ...], db_key: tuple) -> hv.VolSeries:
    store = HistoricalStore(db_key[0])
    with store:
        return hv.compute(store.swaption_forwards(hv.normalize(n) for n in labels), store.vol_quotes(), labels)


@xw.func
@xw.arg("labels", ndim=1)
def VolHistory(labels, trade_date) -> List[list]:
    """
    銘柄(「満期x原資産」、例 1Yx10Y)ごとのIVとFwdの日次変化。
    見出し行 [Date, 銘柄1 IV, 銘柄1 Fwd, 銘柄1 dF, 銘柄2 IV, …] + 日付ごとの行(昇順)。
      IV : DBのスワップションATMノーマルvol(bp/年)。DBに無い日は空文字(Excelで="" と判定できる)
      Fwd: その日の契約(満期日=その日+満期、起算日=満期日+2営業日)の原資産のFwdスワップレート(%)
      dF : 同じFwdの、前の日付のカーブからの変化(bp)。ロールダウンを含まない
    行は朝バッチが原資産のFwdを計算した日付(最初の日付は変化が無いので含めない)のうち評価日以前。
    格子の外の銘柄はエラー。trade_date: 評価日(必須。TradeDate)。
    """
    names = udf_common._labels(labels)
    if not names:
        raise ValueError("銘柄を1つ以上指定してください(例 1Yx10Y)")
    until = udf_common._required_date(trade_date)
    series = _compute_cached(names, udf_common._db_key())
    header = ["Date"] + [f"{name} {col}" for name in series.labels for col in ("IV", "Fwd", "dF")]
    rows: List[list] = [header]
    for d, iv, fwd, dfwd in zip(series.dates, series.iv, series.fwd, series.dfwd):
        if d > until:
            break  # 日付の昇順
        row: list = [d]
        for k in range(len(series.labels)):
            row += ["" if iv[k] is None else iv[k], fwd[k], dfwd[k]]
        rows.append(row)
    return rows


@xw.func
@xw.arg("labels", ndim=1)
@xw.arg("real_time_range", ndim=2)
def VolFwdLive(labels, real_time_range, trade_date) -> List[list]:
    """
    評価日のカーブでの、銘柄(「満期x原資産」)の原資産のFwdスワップレート(%、labels と同じ並びの縦1列。
    空欄の行は空欄を返す)。評価日が今日ならライブのレート、過去の日付ならDBのその日のカーブ(udf_common._market)。
    契約はVolHistoryのFwdの各日の決め方と同じ(option/swaption/convert.underlying)。
    real_time_range: フラグ/Ticker/Value の範囲(シートのカーブ用の範囲の TAKE(…#,,3)。pricingと同じ引き方)。
    trade_date: 評価日(必須。TradeDate)。
    """
    cells = list(labels or [])
    names = udf_common._labels(cells)
    if not names:
        raise ValueError("銘柄を1つ以上指定してください(例 1Yx10Y)")
    market = udf_common._market(real_time_range, udf_common._required_date(trade_date)).base
    rates = iter([par_rate(market, t).target_fixrate for t in hv.live_terms(names, market.valuation_date)])
    return [[""] if _is_blank(v) else [next(rates)] for v in cells]
