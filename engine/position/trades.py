"""
日中の新規取引(スワップ)のピラーごとのデルタ。会社のリスク(前日引け)に入っていない取引だけを足すためのもの。

規約は標準のOIS(terms.STANDARD。RealTimeのスポットのOISと同じ)。起算日は空欄=スポット、日付、
または「1Y」のような期間(スポットからの先日付)。期間はtenor(「5Y」)か満期日。デルタは
risk.bucketed_deltas(スワップのRiskGridと同じバンプ)で、百万円/bp(想定元本を百万円で入れるので
PV・デルタもそのまま百万円)。
"""

from datetime import date
from typing import List, NamedTuple, Optional, Sequence, Union

import QuantLib as ql

from engine.common.market import CurveSet
from engine.common.risk import bucketed_deltas, price, resolve_params
from engine.common.terms import PAY, REC, STANDARD, SwapParams, SwapTerms

_PAY_REC = {"PAY": PAY, "P": PAY, "REC": REC, "R": REC, "RECEIVE": REC}


class Trade(NamedTuple):
    start: Union[None, date, str]  # None=スポット、日付、または期間(スポットからの先日付)
    tenor: Union[date, str]  # 期間(「5Y」)か満期日
    notional: float  # 百万円
    pay_rec: str
    fix_rate: Optional[float] = None  # %(None=パーレート)


class TradeRisk(NamedTuple):
    fix_rate: float  # 計算に使った固定金利(%)
    par_rate: float
    pv: float  # 百万円
    deltas: dict  # ピラーのラベル → 百万円/bp


def parse_pay_rec(value) -> str:
    key = str(value or "").strip().upper()
    if key not in _PAY_REC:
        raise ValueError(f"Pay/Recは PAY か REC です: {value!r}")
    return _PAY_REC[key]


def _start_date(start, spot: date) -> date:
    if start is None:
        return spot
    if isinstance(start, date):
        return start
    period = ql.PeriodParser.parse(str(start).strip().upper())
    return (ql.Date.from_date(spot) + period).to_date()


def trade_params(curves: CurveSet, trade: Trade) -> SwapParams:
    start = _start_date(trade.start, curves.spot_date)
    if isinstance(trade.tenor, date):
        terms = SwapTerms(start, STANDARD, end=trade.tenor)
    else:
        terms = SwapTerms(start, STANDARD, tenor=str(trade.tenor).strip().lower())
    return resolve_params(curves, SwapParams(terms, trade.notional, parse_pay_rec(trade.pay_rec), trade.fix_rate))


def trade_risks(curves: CurveSet, trades: Sequence[Optional[Trade]]) -> List[object]:
    """行ごとの TradeRisk(エラーの行は例外オブジェクト、空行はNone)。"""
    results: List[object] = []
    params = []
    for t in trades:
        if t is None:
            results.append(None)
            continue
        try:
            p = trade_params(curves, t)
            base = price(curves.base, p)
            results.append((p, base))
            params.append(p)
        except Exception as e:  # 行ごとにメッセージを出して他の行は続ける
            results.append(e)
    deltas = iter(bucketed_deltas(curves, params) if params else [])
    out: List[object] = []
    for r in results:
        if isinstance(r, tuple):
            p, base = r
            out.append(TradeRisk(p.fix_rate, base.target_fixrate, base.pv, next(deltas)))
        else:
            out.append(r)
    return out
