"""満期・原資産の格子(DBのvol_quotesと同じ16満期x12原資産)と、データシートの列の並び。

銘柄の番号 j(1始まり)= (満期の番号-1)*12 + 原資産の番号。データシートではB列が j=1。
"""

from typing import Dict, List

EXPIRIES: List[str] = ["1M", "2M", "3M", "6M", "9M", "1Y", "2Y", "3Y", "4Y", "5Y", "6Y", "7Y", "9Y", "10Y", "15Y", "20Y"]
TAILS: List[str] = ["1Y", "2Y", "3Y", "4Y", "5Y", "6Y", "7Y", "8Y", "9Y", "10Y", "15Y", "20Y"]

# オプション追跡型(A)は満期1Y以下だけ(それより長いと、満期まで追える取引日がほとんど無い)
TRACK_EXPIRIES: List[str] = EXPIRIES[:6]


def months(tenor: str) -> int:
    n, unit = int(tenor[:-1]), tenor[-1].upper()
    return n * 12 if unit == "Y" else n


def label(expiry: str, tail: str) -> str:
    """DBのvol_quotesの銘柄名(例 1Yx10Y)。"""
    return f"{expiry}x{tail}"


LABELS: List[str] = [label(e, t) for e in EXPIRIES for t in TAILS]
TRACK_LABELS: List[str] = [label(e, t) for e in TRACK_EXPIRIES for t in TAILS]
INDEX: Dict[str, int] = {name: n for n, name in enumerate(LABELS)}  # 0始まり
