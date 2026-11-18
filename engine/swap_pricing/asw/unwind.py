"""
約定済みのMMS(ASWのスワップの側)のアンワインドPV(UDF AswUnwindTable の本体)。

AswPricingTable(table.py)とは別のUDFにする。スワップの額面の既定値は「現物の額面×hedge ratio」で、
hedge ratioはAswPricingTableの出力なので、同じUDFの入力にすると循環参照になるため。

■ 1行の流れ
  約定当初の起算日(trade start)・償還日(maturity。AswPricingTableが返した値のセル)・行の規約・pay/rec
  → 約定時のMMSのレート(traded MMS rate)・スワップの額面(swap notional used、億円。既定はAswPricingTableのhedge ratio×現物の額面、手で上書きできる。
    どちらを使うかはシートの式)で組んだOIS
  → pricing表のPVと同じ measures.pv(評価日時点の価値。起算日が過去なら評価日前日までのTONA実績を使う)
  満期日はAswPricingTableのMMSと同じく償還日(Modified Following)。エンド日の入力は持たない(ユーザー判断)。
■ 計算する行
  trade start・traded MMS rate・swap notional usedがそろった行だけ。どれかが空欄なら全部空欄
  (まだ約定していない行)。入力の誤り・TONA実績の欠損はmessage列に閉じ込める。
"""

import re
from typing import Dict, List, Sequence

from engine.common import udf_common
from engine.common.swap_pricer import swap_dates
from engine.common.terms import SwapConvention, SwapParams, SwapTerms
from engine.swap_pricing.asw.pricer import OKU
from engine.swap_pricing.pricing import measures

INPUT_COLUMNS = (
    "maturity", "fix freq", "fix dcf", "float freq", "roll conv", "pay/rec",
    "trade start", "traded mms rate", "swap notional used",
)
OUTPUT_HEADER = ("unwind start", "unwind end", "unwind pv", "unwind message")
_TRADE_INPUTS = ("trade start", "traded mms rate", "swap notional used")


_UNIT = re.compile(r"\s+\([^()]*\)$")  # 見出しの末尾の単位「 (100M)」「 (%)」


def columns(header: Sequence) -> Dict[str, int]:
    """見出し行 → {見出し名: 列番号}。見出しの末尾の単位(空白+括弧)は除いて探す。"""
    labels = [_UNIT.sub("", str(h).strip().lower()) if h is not None else "" for h in header]
    missing = [c for c in INPUT_COLUMNS if c not in labels]
    if missing:
        raise ValueError(f"アンワインドの表に見出しがありません: {', '.join(missing)}")
    return {c: labels.index(c) for c in INPUT_COLUMNS}


def _number(value, name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name}が数値ではありません: {value!r}") from None


def _date(value, name: str):
    d = udf_common._to_date(value)
    if d is None:
        raise ValueError(f"{name}が日付ではありません: {value!r}")
    return d


def price_row(row: Sequence, col, curves) -> list:
    get = lambda name: row[col[name]]  # noqa: E731
    if any(udf_common._is_blank(get(c)) for c in _TRADE_INPUTS):
        return [""] * len(OUTPUT_HEADER)
    try:
        if udf_common._is_blank(get("pay/rec")):
            raise ValueError("pay/recが空欄です(現物を買ったASWならPAY)")
        terms = SwapTerms(
            _date(get("trade start"), "trade start"),
            SwapConvention(*(get(c) for c in ("fix freq", "fix dcf", "float freq", "roll conv"))),
            end=_date(get("maturity"), "maturity"),
        )
        params = SwapParams(
            terms, _number(get("swap notional used"), "swap notional used") * OKU, str(get("pay/rec")).strip().upper(),
            fix_rate=_number(get("traded mms rate"), "traded MMS rate"),
        )
        start, end = swap_dates(terms)
        return [start, end, measures.pv(curves, params), ""]
    except Exception as e:
        return ["", "", "", str(e)]


def price_table(table: Sequence[Sequence], curves) -> List[list]:
    """table: 見出し行+銘柄の行。見出し行+行ごとの結果(行の並びは入力と同じ)を返す。"""
    rows = [list(r) if r is not None else [] for r in (table or [])]
    if not rows:
        raise ValueError("アンワインドの表が空です")
    col = columns(rows[0])
    width = len(rows[0])
    return [list(OUTPUT_HEADER)] + [price_row(r + [None] * (width - len(r)), col, curves) for r in rows[1:]]
