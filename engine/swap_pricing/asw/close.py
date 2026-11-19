"""
ASWPricer表の引け時点のASW(UDF AswCloseTable の本体)。

AswPricingTable(table.py)とは別のUDFにし、ライブのレートの範囲を引数に取らない(ユーザー判断)。
AswPricingTableはライブのレート(volatileのLiveQuotes)に依存するのでShift+F9のたびに計算し直されるが、
引けの値は銘柄・規約・評価日が変わったときだけ計算すればよいため。

■ 1行の流れ(現物もスワップも引けの日にそろえる。Neonの ASW_data と同じく各日の受渡で計算する)
  銘柄 → DBの銘柄情報と、評価日以前で直近の引け値(単利。AswPricingTableの close・close date と同じ行)
  → 引け日のスポット受渡(T+1)での単価・複利(スライド・yield overrideは使わない)
  → 引け日のスポット(T+2)〜償還日のMMS(規約は行の fix freq 等)を、朝バッチがDBに保存した引け日のカーブで評価
  ASW(bp) = (close compound yield − close swap rate)×100 と、今のASWとの差はシートの式で計算する。
■ カーブ
  朝バッチが保存した日次カーブ(historical_store.load。HistoricalRates等と同じもの)。ライブのカーブの
  キャッシュ(market.curve_set)には入れない(ライブのカーブを追い出さないため)。同じ引け日の行は1本を使い回す。
"""

from datetime import date
from typing import Dict, List, Sequence

from engine.common import asw, bond, udf_common
from engine.common.market import db_fixings
from engine.common.market_data import Market
from engine.common.terms import SwapConvention
from engine.swap_pricing.pricing.table import columns

INPUT_COLUMNS = ("bond", "fix freq", "fix dcf", "float freq", "roll conv")
OUTPUT_HEADER = ("close compound yield", "close swap rate", "close message")


def price_row(row: Sequence, col, valuation_date: date, store, markets: Dict[date, Market]) -> list:
    get = lambda name: row[col[name]]  # noqa: E731
    if udf_common._is_blank(get("bond")):
        return [""] * len(OUTPUT_HEADER)
    result = ["", "", ""]
    try:
        code = str(get("bond")).strip().upper()
        info = store.jgb_bond(code)
        if info is None:
            raise ValueError(f"銘柄 {code} がDBにありません")
        close = store.jgb_close(code, valuation_date)
        if close is None:
            raise ValueError(f"{code} の引け値がDBにありません")
        jgb = bond.Jgb(info.coupon, info.maturity)
        result[0] = asw.spot_bond(jgb, close.simple_yield, close.as_of_date).compound_yield

        close_date = close.as_of_date
        convention = SwapConvention(*(get(c) for c in ("fix freq", "fix dcf", "float freq", "roll conv")))
        if close_date not in markets:
            stored = store.load(close_date)
            markets[close_date] = Market(close_date, stored.curve, stored.index, db_fixings(store))
        result[1] = asw.mms_rate(markets[close_date], info.maturity, convention)
    except Exception as e:
        result[2] = str(e)
    return result


def price_table(table: Sequence[Sequence], valuation_date: date, store) -> List[list]:
    """table: 見出し行+銘柄の行(AswPricingTableと同じ範囲でよい。使う列は見出し名で探す)。
    見出し行+行ごとの結果(行の並びは入力と同じ)を返す。store: ヒストリカルDB(開いたもの)。"""
    rows = [list(r) if r is not None else [] for r in (table or [])]
    if not rows:
        raise ValueError("ASWPricer表が空です")
    col = columns(rows[0], INPUT_COLUMNS)
    width = len(rows[0])
    markets: Dict[date, Market] = {}
    return [list(OUTPUT_HEADER)] + [
        price_row(r + [None] * (width - len(r)), col, valuation_date, store, markets) for r in rows[1:]
    ]
