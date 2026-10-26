"""
現物(JGB)のポジションを、スワップ(OIS)のカーブのピラーごとのデルタに写す。

■ 方法(Zスプレッド固定のキーレート・デルタ)
  1. 銘柄の単利(DBの引け値。yield overrideがあればそれ)から、スポット受渡(T+1)の単価・経過利子を出す。
  2. OISの基準カーブに対するZスプレッド(連続複利の一定のスプレッド)を、その単価に合うように解く。
  3. カーブのピラーを1本ずつ動かし(market.CurveSet.bumped、スワップのRiskGridと同じバンプ)、
     Zスプレッドを固定したまま単価を出し直す。単価の変化×額面がそのピラーのデルタ。
  スワップのグリッドと同じピラー・同じバンプで測るので、現物とスワップのグリッドをそのまま足せる
  (足したものが現物とスワップのネット。ASW(ベーシス)の変化は入らない)。

■ 単位・符号
  額面は百万円、デルタは百万円/bp(金利が1bp上がったときのPVの変化。現物のロングはマイナス)。
"""

from datetime import date
from typing import Dict, List, NamedTuple, Optional, Sequence

import QuantLib as ql

from engine.common import bond
from engine.common.market import CurveSet
from engine.common.risk import per_bp

SPOT_SETTLEMENT_DAYS = 1
_COMPOUNDING = ql.Continuous
_FREQUENCY = ql.Annual


class BondPosition(NamedTuple):
    code: str
    face: float  # 百万円
    yield_override: Optional[float] = None  # 単利(%)


class BondRisk(NamedTuple):
    code: str
    coupon: float
    maturity: date
    simple_yield: float
    close_date: Optional[date]  # 単利が引け値ならその日(overrideならNone)
    clean: float
    z_spread_bp: float
    deltas: Dict[str, float]  # ピラーのラベル → 百万円/bp


def _clean_price(qb: ql.Bond, curve: ql.YieldTermStructure, z: float, settle: ql.Date) -> float:
    return ql.BondFunctions.cleanPrice(qb, curve, z, bond.ACCRUAL, _COMPOUNDING, _FREQUENCY, settle)


class _Prepared(NamedTuple):
    risk: BondRisk
    qb: ql.Bond
    settle: ql.Date
    z: float
    base: float
    face: float


def _prepare(curves: CurveSet, store, position: BondPosition) -> _Prepared:
    code = position.code.strip().upper()
    info = store.jgb_bond(code)
    if info is None:
        raise ValueError(f"銘柄 {code} がDBにありません")
    jgb = bond.Jgb(info.coupon, info.maturity)
    if position.yield_override is not None:
        simple_yield, close_date = position.yield_override, None
    else:
        close = store.jgb_close(code, curves.valuation_date)
        if close is None:
            raise ValueError(f"{code}の引け値がDBにありません(yield overrideに単利を入れてください)")
        simple_yield, close_date = close.simple_yield, close.as_of_date
    settle = bond.settlement_date(curves.valuation_date, SPOT_SETTLEMENT_DAYS)
    if settle >= jgb.maturity:
        raise ValueError(f"{code}は受渡日{settle}までに償還します")
    clean = bond.price_from_simple_yield(jgb, simple_yield, settle)

    qb = bond.ql_bond(jgb)
    qsettle = ql.Date.from_date(settle)
    curve = curves.base.curve.currentLink()
    z = ql.BondFunctions.zSpread(
        qb, ql.BondPrice(clean, ql.BondPrice.Clean), curve, bond.ACCRUAL, _COMPOUNDING, _FREQUENCY, qsettle,
    )
    risk = BondRisk(code, info.coupon, info.maturity, simple_yield, close_date, clean, z * 1e4, {})
    return _Prepared(risk, qb, qsettle, z, _clean_price(qb, curve, z, qsettle), position.face)


def bond_risks(curves: CurveSet, store, positions: Sequence[Optional[BondPosition]]) -> List[object]:
    """行ごとの BondRisk(エラーの行は例外オブジェクト、空行はNone)。store: 銘柄情報・引け値を読む
    ヒストリカルDB(開いたもの)。ピラーごとにカーブを1回だけ動かし、その間に全銘柄を評価し直す。"""
    results: List[object] = []
    for p in positions:
        if p is None:
            results.append(None)
            continue
        try:
            results.append(_prepare(curves, store, p))
        except Exception as e:  # 行ごとにメッセージを出して他の行は続ける
            results.append(e)
    prepared = [r for r in results if isinstance(r, _Prepared)]
    curve = curves.base.curve.currentLink()
    for label in curves.bump_labels:
        with curves.bumped(label):
            for r in prepared:
                bumped = _clean_price(r.qb, curve, r.z, r.settle)
                r.risk.deltas[label] = per_bp((bumped - r.base) / 100.0 * r.face)
    return [r.risk if isinstance(r, _Prepared) else r for r in results]
