"""
分析ブック(SwapAnalysis.xlsm)のFutBasisシートの材料: 長期国債先物のつなぎ足とCTDのベーシスのヒストリカル、
限月を重ねたベーシス、1日分の銘柄ごとのベーシス。規則(限月・交換係数・ベーシス・CTD・レポの階段)は
engine/common/jgb_futures.py。ここはDB(朝バッチが取り込んだ清算値・引け値・TONA実績)を読み、日付ごとに
当てはめるだけ。差分・銭への換算・段差の調整・σはExcelの数式で行う。

■ レポ
  ヒストリカル(つなぎ足・重ね合わせ)は階段(jgb_futures.repo_steps)。シートで段を手で上書きできる
  (UDFに [Start, Rate] の表を渡すと、その表だけを使う。空ならTONA実績から自動で作る)。
  最初の段より前の日はレポが無いので、キャリー・ネットベーシス・CTDを空欄にする(TONA実績はDBに2024-10-28から。
  それより前を見たいときはシートの段の表に手で足す)。グロスベーシスとIRRはレポに依らないが、どの銘柄の値かは
  CTDで決まるので、これらも空欄にする。
  1日分の表(basis_on)は、レポの指定が無ければその日以前で直近のTONA(O/N)。
■ 引け値
  ヒストリカルはその日の引け値がある銘柄だけを使う。1日分の表は、その日以前で直近の引け値(その日付も返す)。
■ 重ね合わせ
  限月ごとに、清算値のある日(期近になる前も含む)のCTDの値を「取引最終日までの営業日数」に並べる。
  CTDはその日・その限月の受渡適格銘柄のうちネットベーシス最小。
"""

from dataclasses import dataclass
from datetime import date
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

import QuantLib as ql

from engine.common import jgb_futures as jf
from engine.common.conventions import CALENDAR
from engine.common.jgb_futures import BasisRow, BondQuote, Contract, RepoStep

# 重ね合わせで並べられる値(CTDの行の項目)
OVERLAY_FIELDS = {
    "NET": lambda r: r.net_basis,
    "GROSS": lambda r: r.gross_basis,
    "CARRY": lambda r: r.carry,
    "IRR": lambda r: r.implied_repo,
}


@dataclass
class FutData:
    settles: Dict[Contract, Dict[date, float]]  # 限月 → {日付: 清算値}
    closes: Dict[date, Dict[str, float]]  # 日付 → {銘柄コード: 引け値の単利}(10年債だけ)
    bonds: Dict[str, Tuple[float, date]]  # 銘柄コード → (利率, 償還日)
    fixings: Dict[date, float]  # TONA実績(%)

    def quotes(self, as_of: date) -> List[BondQuote]:
        return [
            BondQuote(code, *self.bonds[code], y)
            for code, y in self.closes.get(as_of, {}).items() if code in self.bonds
        ]


def load(store) -> FutData:
    settles = {Contract.parse(k): v for k, v in store.fut_settles().items()}
    if not settles:
        raise ValueError("先物の清算値がDBにありません。python -m engine.common.morning_batch で取り込んでください")
    first = min(d for prices in settles.values() for d in prices)
    bonds = {
        b.code: (b.coupon, b.maturity)
        for b in store.jgb_master(date.max) if b.code.upper().startswith(jf.DELIVERABLE_PREFIX)
    }
    closes = store.jgb_closes_between(first, date.max, jf.DELIVERABLE_PREFIX)
    return FutData(settles, closes, bonds, store.fixings())


def ctd_on(data: FutData, contract: Contract, as_of: date, repo: Optional[float]) -> Optional[BasisRow]:
    """その日・その限月のCTD(清算値で)。清算値・レポ・適格銘柄の引け値のどれかが無ければ None。"""
    price = data.settles.get(contract, {}).get(as_of)
    if price is None or repo is None:
        return None
    return jf.cheapest(jf.basis_table(contract, price, as_of, data.quotes(as_of), repo))


class FrontDay(NamedTuple):
    quote: jf.FrontQuote
    repo: Optional[float]
    ctd: Optional[BasisRow]


def front_history(data: FutData, steps: Sequence[RepoStep]) -> List[FrontDay]:
    """期近のつなぎ足(取引最終日の翌日に乗り換え)と、その日のCTDのベーシス。"""
    out = []
    for q in jf.front_series(data.settles):
        repo = jf.repo_on(steps, q.as_of)
        out.append(FrontDay(q, repo, ctd_on(data, q.contract, q.as_of, repo)))
    return out


def business_days_to(as_of: date, until: date) -> int:
    """as_of → until の営業日数(as_of が until なら0)。"""
    return CALENDAR.businessDaysBetween(ql.Date.from_date(as_of), ql.Date.from_date(until), False, True)


def overlay(
    data: FutData, contracts: Sequence[Contract], steps: Sequence[RepoStep], field: str,
) -> Dict[Contract, Dict[int, float]]:
    """限月ごとの {取引最終日までの営業日数: CTDの値}。取引最終日より後の日は含めない。"""
    value = OVERLAY_FIELDS[field]
    out: Dict[Contract, Dict[int, float]] = {}
    for c in contracts:
        last = jf.last_trade_date(c)
        points: Dict[int, float] = {}
        for d in sorted(data.settles.get(c, {})):
            if d > last:
                continue
            row = ctd_on(data, c, d, jf.repo_on(steps, d))
            if row is not None:
                points[business_days_to(d, last)] = value(row)
        out[c] = points
    return out


class DayBasis(NamedTuple):
    futures_price: float
    repo: float
    rows: List[Tuple[BasisRow, date]]  # (ベーシス, 引け値の日付)。償還日の順


def basis_on(
    data: FutData, contract: Contract, as_of: date, futures_price: Optional[float], repo: Optional[float],
    yields: Optional[Dict[str, float]] = None,
) -> DayBasis:
    """1日分の銘柄ごとのベーシス。futures_price・repo の指定が無ければDBの値(その日の清算値、その日以前で
    直近のTONA)。引け値はその日以前で直近の値で、yields({銘柄コード: 単利})があればそれで上書きする。"""
    if futures_price is None:
        futures_price = data.settles.get(contract, {}).get(as_of)
        if futures_price is None:
            raise ValueError(f"{contract.label}限の {as_of} の清算値がDBにありません(先物価格を指定してください)")
    if repo is None:
        earlier = [d for d in data.fixings if d <= as_of]
        if not earlier:
            raise ValueError(f"{as_of} 以前のTONA実績がDBにありません(レポを指定してください)")
        repo = data.fixings[max(earlier)]
    latest: Dict[str, Tuple[float, date]] = {}
    for d in sorted(x for x in data.closes if x <= as_of):
        for code, y in data.closes[d].items():
            latest[code] = (y, d)
    for code, y in (yields or {}).items():
        latest[code] = (y, as_of)
    quotes = [BondQuote(code, *data.bonds[code], y) for code, (y, _) in latest.items() if code in data.bonds]
    table = jf.basis_table(contract, futures_price, as_of, quotes, repo)
    return DayBasis(futures_price, repo, [(r, latest[r.code][1]) for r in table])
