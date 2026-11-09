"""
現物(JGB)のポジションのデルタ・ガンマ(修正デュレーション・コンベクシティ。カーブはバンプしない)。

■ 方法(ユーザーと決定、2026-11-10)
  1. 銘柄の単利(会社の引け値・約定の単利、空欄ならDBの引け値)から、スポット受渡(T+1)の利含み単価 P を出す。
  2. P に合う半年複利 y(切り捨てない。ASW・現物オプションと同じ複利の規約)での修正デュレーション MD と
     コンベクシティ C(QuantLibの BondFunctions)を出す。
  3. 額面100円あたりの複利1bpの単価の変化 dP = −MD × P × 1bp、2階 d2P = C × P × 1bp²。
     デルタ = dP × 額面 / 100、ガンマ = d2P × 額面 / 100。単価を掛けるのは、MDが「価格の何%動くか」の比率で
     円にするには価格が要るため(額面だけだと単価の低い銘柄で大きくずれる。JX68で約2倍)。
  4. 按分はしない。満期が入る区間の長いほうのピラーに全部載せる(残存2.6年 → 2Yと3Yの間なので3Y)。
     ピラーの日付 = スワップのスポット日 + テナー(休日調整前)、満期がピラーの日付と同じ日ならそのピラー。
     最も長いピラーより先の満期は最も長いピラーに載せる。O/Nは区間に使わない。
  Moveはスワップのピラーの動き(Positionシート)をそのまま掛ける(JGBのリアルタイムが無いため。ユーザー了承)。

■ 単位・符号
  額面は百万円(ロング=プラス)、デルタは百万円/bp、ガンマは百万円/bp²(金利が上がったときのPVの変化。
  現物のロングはデルタがマイナス、ガンマがプラス)。
"""

import re
from datetime import date
from typing import Dict, List, NamedTuple, Optional, Sequence

import QuantLib as ql

from engine.common import bond
from engine.common.jgb_source import KINDS

SPOT_SETTLEMENT_DAYS = 1
BP = 1e-4
ON_LABEL = "o/n"

# 会社のファイル(Raw_JGB)の Instrument ID。0-02-{回号4桁}-000{種別}(RIC JP{回号}00{種別}= と同じ並び、
# 種別はjgb_source.KINDS。例 0-02-0023-00068 → JX23)
_INSTRUMENT_ID = re.compile(r"^\d-\d{2}-(\d{4})-000(\d{2})$")


class BondPosition(NamedTuple):
    code: str
    face: float  # 百万円
    yield_override: Optional[float] = None  # 単利(%)


class BondRisk(NamedTuple):
    code: str
    coupon: float
    maturity: date
    simple_yield: float
    close_date: Optional[date]  # 単利がDBの引け値ならその日(overrideならNone)
    clean: float
    dirty: float
    compound_yield: float  # 半年複利(%、切り捨てない)
    modified_duration: float
    convexity: float
    dp: float  # 額面100円あたりの複利1bpの単価の変化(円/bp)
    d2p: float  # 同じく2階(円/bp²)
    face: float  # 百万円
    delta: float  # 百万円/bp
    gamma: float  # 百万円/bp²


def jgb_code(instrument_id) -> str:
    """会社の Instrument ID → 銘柄コード(0-02-0023-00068 → JX23)。形が違う・知らない種別ならValueError。"""
    text = str(instrument_id or "").strip()
    m = _INSTRUMENT_ID.match(text)
    if m is None or m.group(2) not in KINDS:
        raise ValueError(f"Instrument IDの形が違います(0-02-回号4桁-000種別): {instrument_id!r}")
    return f"{KINDS[m.group(2)][1]}{int(m.group(1))}"


def _yield_risk(jgb: bond.Jgb, dirty: float, settle: date):
    """利含み単価 dirty での (半年複利%, MD, コンベクシティ)。日数はうるう日除き/365(bond.compound_yieldと同じ)。"""
    y = bond.compound_yield(jgb, dirty, settle, truncate=False)
    qb = bond.ql_bond(jgb, "no_leap")
    rate = ql.InterestRate(y / 100, bond.NO_LEAP, ql.Compounded, ql.Semiannual)
    qd = ql.Date.from_date(settle)
    md = ql.BondFunctions.duration(qb, rate, ql.Duration.Modified, qd)
    cx = ql.BondFunctions.convexity(qb, rate, qd)
    return y, md, cx


def bond_risk(store, position: BondPosition, valuation_date: date) -> BondRisk:
    """1銘柄のデルタ・ガンマ。store: 銘柄情報・引け値を読むヒストリカルDB(開いたもの)。"""
    code = position.code.strip().upper()
    info = store.jgb_bond(code)
    if info is None:
        raise ValueError(f"銘柄 {code} がDBにありません")
    jgb = bond.Jgb(info.coupon, info.maturity)
    if position.yield_override is not None:
        simple_yield, close_date = position.yield_override, None
    else:
        close = store.jgb_close(code, valuation_date)
        if close is None:
            raise ValueError(f"{code}の引け値がDBにありません(利回りを入れてください)")
        simple_yield, close_date = close.simple_yield, close.as_of_date
    settle = bond.settlement_date(valuation_date, SPOT_SETTLEMENT_DAYS)
    if settle >= jgb.maturity:
        raise ValueError(f"{code}は受渡日{settle}までに償還します")
    clean = bond.price_from_simple_yield(jgb, simple_yield, settle)
    dirty = clean + bond.accrued_interest(jgb, settle)
    y, md, cx = _yield_risk(jgb, dirty, settle)
    dp = -md * dirty * BP
    d2p = cx * dirty * BP * BP
    face = position.face
    return BondRisk(code, info.coupon, info.maturity, simple_yield, close_date, clean, dirty, y, md, cx,
                    dp, d2p, face, dp * face / 100.0, d2p * face / 100.0)


def pillar_dates(labels: Sequence[str], spot: date) -> List[tuple]:
    """区間に使うピラー [(ピラーの日付, ラベル)](日付の短い順)。テナーとして読めないラベル・O/Nは除く。"""
    out = []
    for label in labels:
        key = str(label).strip()
        if key.lower() == ON_LABEL:
            continue
        try:
            period = ql.PeriodParser.parse(key.upper())
        except RuntimeError:
            continue
        out.append(((ql.Date.from_date(spot) + period).to_date(), key))
    return sorted(out)


def bucket(maturity: date, pillars: Sequence[tuple]) -> str:
    """満期が入る区間の長いほうのピラーのラベル(pillars は pillar_dates の結果)。"""
    if not pillars:
        raise ValueError("区間に使うピラーがありません")
    for d, label in pillars:
        if maturity <= d:
            return label
    return pillars[-1][1]


def bond_risks(store, positions: Sequence[Optional[BondPosition]], valuation_date: date) -> List[object]:
    """行ごとの BondRisk(エラーの行は例外オブジェクト、空行はNone)。"""
    results: List[object] = []
    for p in positions:
        if p is None:
            results.append(None)
            continue
        try:
            results.append(bond_risk(store, p, valuation_date))
        except Exception as e:  # 行ごとにメッセージを出して他の行は続ける
            results.append(e)
    return results


def on_pillar(value: float, label: str) -> Dict[str, float]:
    """1つのピラーだけに載せたデルタ・ガンマの辞書(キーは小文字のラベル。udf._deltas_rowと同じ)。"""
    return {label.lower(): value}
