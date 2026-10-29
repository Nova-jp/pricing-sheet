"""
pricing表(見出し行+取引の行)を1回でまとめて計算する(UDF PricingTable の本体)。

xlwingsのUDFは1セルごとにExcelとの往復(1回約170ms)がかかり、行ごとに4〜5個のUDFを置くと
再計算の時間がセルの数で決まる(roadmapの4)。表全体を1回で受け取り、行ごとの結果を1つの配列で返す。
各列の計算はmeasures.pyの関数を組み合わせるだけで、ここでは表の読み方と行ごとのエラーの扱いだけを持つ。

■ 計算する行と列
  - flag=1の行だけ計算する(それ以外の行は全部空欄)
  - target fixrate: 常に
  - PV・delta(bump)・delta(annuity): notionalがある行だけ
  - adj_notional: delta(目標delta)がある行だけ
■ カーブ
  index列(JSCC/LCH、空欄=JSCC)で行ごとに清算機関のカーブを選ぶ(udf_common._curves_by_index)。
  LCHのカーブが組めない(評価日が過去・LCHのレートが無い等)ときは、LCHの行だけをmessageのエラーにする。
■ エラー
  1行の入力の誤りで表全体をエラーにしない。計算できなかった列は空欄にし、理由をmessage列に出す。
  評価日のカーブが組めない等、全行に関わるエラーは呼び出し元(UDF)でそのままエラーにする。
"""

from typing import Callable, Dict, List, Sequence

from engine.common import udf_common
from engine.swap_pricing.pricing import measures
from engine.swap_pricing.pricing.row import swap_params, swap_terms

# 表から読む列(見出し名、小文字)。列の位置ではなく見出しで探すので、列を移動しても壊れない
INPUT_COLUMNS = (
    "flag", "notional", "delta", "start", "tenor", "end", "fix rate", "fix freq", "fix dcf",
    "float freq", "roll conv", "pay/rec", "index",
)
# 返す列(1行目は見出し)
OUTPUT_HEADER = ("target fixrate", "PV", "delta(bump)", "delta(annuity)", "adj_notional", "message")


def is_flagged(value) -> bool:
    """flag/risk_flag列の判定。1だけを「計算する」とする(空欄・文字は計算しない)。"""
    try:
        return float(value) == 1.0
    except (TypeError, ValueError):
        return False


def columns(header: Sequence, names: Sequence[str]) -> Dict[str, int]:
    """見出し行 → {見出し名: 列番号}。足りない見出しはエラーにする。"""
    labels = [str(h).strip().lower() if h is not None else "" for h in header]
    missing = [c for c in names if c not in labels]
    if missing:
        raise ValueError(f"pricing表に見出しがありません: {', '.join(missing)}")
    return {c: labels.index(c) for c in names}


def price_row(row: Sequence, col: Dict[str, int], curves_for: Callable) -> list:
    """1行分の結果 [target fixrate, PV, delta(bump), delta(annuity), adj_notional, message]。
    curves_for: index列の値 → その清算機関のカーブ一式。"""
    get = lambda name: row[col[name]]  # noqa: E731
    result = [""] * len(OUTPUT_HEADER)
    if not is_flagged(get("flag")):
        return result
    try:
        curves = curves_for(get("index"))
        terms = swap_terms(
            *(get(c) for c in ("start", "tenor", "end", "fix freq", "fix dcf", "float freq", "roll conv")),
            curves,
        )
        par = measures.par(curves, terms)
    except Exception as e:
        result[5] = str(e)
        return result
    result[0] = measures.fair_rate(curves, terms, par)

    errors = []
    if not udf_common._is_blank(get("notional")):
        try:
            params = swap_params(terms, get("notional"), get("pay/rec"), get("fix rate"))
            pv = measures.pv(curves, params, par)
            result[1:4] = [
                pv,
                measures.parallel_delta_mm(curves, params, par, base_pv=pv),
                measures.annuity_delta_mm(curves, params, par),
            ]
        except Exception as e:
            errors.append(f"PV/delta: {e}")
    if not udf_common._is_blank(get("delta")):
        try:
            result[4] = measures.notional_for_delta(curves, terms, get("delta"), get("pay/rec"), par)
        except Exception as e:
            errors.append(f"adj_notional: {e}")
    result[5] = " / ".join(errors)
    return result


def price_table(table: Sequence[Sequence], curves_for: Callable) -> List[list]:
    """table: 見出し行+取引の行。curves_for: index列の値 → カーブ一式(udf_common._curves_by_index)。見出し行+行ごとの結果(行の並びは入力と同じ)を返す。"""
    rows = [list(r) if r is not None else [] for r in (table or [])]
    if not rows:
        raise ValueError("pricing表が空です")
    col = columns(rows[0], INPUT_COLUMNS)
    width = len(rows[0])
    return [list(OUTPUT_HEADER)] + [price_row(r + [None] * (width - len(r)), col, curves_for) for r in rows[1:]]
