"""
日中の取引(OIS・現物)のピラーごとのデルタ・ガンマ。会社のリスク(前日の引けの値)に、今日の取引の分を
ライブのカーブで足すためのもの(Delta・Gammaシートの Intraday の列)。

■ 計算
  - OIS: 規約・入力は trades.py(標準のOIS、起算日は空欄=スポット)。
  - 現物: bonds.py の修正デュレーション・コンベクシティ(会社のファイルの現物と同じ方法、カーブはバンプしない)。
    単利は約定の単利(空欄=DBの引け値)。満期が入る区間の長いほうのピラーに全部載せる。
  - OISのデルタ: ピラーを1本ずつ +BUMP_BP(0.1bp)動かしたPVの差を1bpあたりに(スワップのRiskGridと同じ)。
  - OISのガンマ: 同じピラーを ±BUMP_BP 動かした2階の中心差分 (PV₊ − 2PV₀ + PV₋) / BUMP_BP²。
    「そのピラーが1bp動いたときの、そのピラーのデルタの変化」(対角のガンマ)。スワップのガンマは0.1bpでも
    1bpでも一致する。0.01bpは数値誤差が出る。
  ピラーごとにカーブを1回だけ動かし、その間に全OISを評価し直す(取引の数だけカーブを作り直さない)。

■ 単位・符号
  想定元本・額面は百万円、デルタは百万円/bp、ガンマは百万円/bp²(金利が上がったときのPVの変化。
  レシーブ・現物のロングはデルタがマイナス)。
"""

from typing import Callable, Dict, List, NamedTuple, Optional, Sequence

from engine.common.market import BUMP_BP, CurveSet
from engine.common.risk import per_bp, price
from engine.position import bonds, trades

OIS = "OIS"
BOND = "Bond"
PRODUCTS = (OIS, BOND)

DONE = "Done"
WHAT_IF = "What-if"
STATUSES = (DONE, WHAT_IF)

_BUY_SELL = {"BUY": 1.0, "B": 1.0, "SELL": -1.0, "S": -1.0}


class IntradayTrade(NamedTuple):
    status: str  # DONE / WHAT_IF
    product: str  # OIS / BOND
    swap: Optional[trades.Trade] = None  # product が OIS のとき
    bond: Optional[bonds.BondPosition] = None  # product が BOND のとき(額面はロング=プラス)


class IntradayRisk(NamedTuple):
    status: str
    product: str
    rate: float  # 計算に使った金利(%)。OISは固定金利、現物は単利
    pv: Optional[float]  # 百万円(OISのPV。現物は約定の価格で評価するのでNone)
    deltas: Dict[str, float]  # ピラーのラベル → 百万円/bp
    gammas: Dict[str, float]  # ピラーのラベル → 百万円/bp²
    pillar: Optional[str] = None  # 現物: 載せたピラーのラベル(OISはNone)
    trade_move: Optional[float] = None  # 現物: 約定の複利 − DBの引けの複利(bp)。PL = デルタ × (今のMove − これ)


def parse_status(value) -> str:
    key = str(value or "").strip().upper().replace(" ", "").replace("-", "").replace("_", "")
    if key == "DONE":
        return DONE
    if key == "WHATIF":
        return WHAT_IF
    raise ValueError(f"Statusは {' / '.join(STATUSES)} のどちらかです: {value!r}")


def parse_product(value) -> str:
    key = str(value or "").strip().upper()
    for p in PRODUCTS:
        if key == p.upper():
            return p
    raise ValueError(f"Productは {' / '.join(PRODUCTS)} のどちらかです: {value!r}")


def bond_sign(value) -> float:
    """現物のSide(Buy/Sell)→ 額面の符号(買い=ロング=プラス)。"""
    key = str(value or "").strip().upper()
    if key not in _BUY_SELL:
        raise ValueError(f"現物のSideは Buy か Sell です: {value!r}")
    return _BUY_SELL[key]


class _Priced(NamedTuple):
    trade: IntradayTrade
    rate: float
    pv: float
    value: Callable[[], float]  # 今の基準カーブ(バンプ中ならバンプした状態)での価値(百万円)


def _price_swap(curves: CurveSet, trade: IntradayTrade) -> _Priced:
    params = trades.trade_params(curves, trade.swap)
    return _Priced(trade, params.fix_rate, price(curves.base, params).pv, lambda: price(curves.base, params).pv)


def _bond_risk(curves: CurveSet, store, trade: IntradayTrade, pillars) -> IntradayRisk:
    r = bonds.bond_risk(store, trade.bond, curves.valuation_date)
    label = bonds.bucket(r.maturity, pillars)
    if trade.bond.yield_override is None:  # 約定の利回りが空欄 = 引け値で約定
        move: Optional[float] = 0.0
    else:
        try:
            close = bonds.bond_risk(store, trade.bond._replace(yield_override=None), curves.valuation_date)
            move = (r.compound_yield - close.compound_yield) * 100.0
        except ValueError:  # 引け値がDBに無い(デルタ・ガンマは約定の利回りで出せる)
            move = None
    return IntradayRisk(trade.status, trade.product, r.simple_yield, None,
                        bonds.on_pillar(r.delta, label), bonds.on_pillar(r.gamma, label), label.upper(), move)


def intraday_risks(curves: CurveSet, store, trades_: Sequence[Optional[IntradayTrade]]) -> List[object]:
    """行ごとの IntradayRisk(エラーの行は例外オブジェクト、空行はNone)。
    store: 現物の銘柄情報・引け値を読むヒストリカルDB(開いたもの)。現物の行が無ければNoneでよい。"""
    pillars = bonds.pillar_dates(curves.bump_labels, curves.spot_date)
    results: List[object] = []
    for t in trades_:
        if t is None:
            results.append(None)
            continue
        try:
            results.append(_price_swap(curves, t) if t.product == OIS else _bond_risk(curves, store, t, pillars))
        except Exception as e:  # 行ごとにメッセージを出して他の行は続ける
            results.append(e)
    priced = [r for r in results if isinstance(r, _Priced)]
    base = [r.value() for r in priced]
    deltas: List[Dict[str, float]] = [{} for _ in priced]
    gammas: List[Dict[str, float]] = [{} for _ in priced]
    if priced:
        for label in curves.bump_labels:
            with curves.bumped(label, BUMP_BP):
                up = [r.value() for r in priced]
            with curves.bumped(label, -BUMP_BP):
                down = [r.value() for r in priced]
            for n in range(len(priced)):
                deltas[n][label] = per_bp(up[n] - base[n])  # スワップのRiskGridと同じ片側の差分
                gammas[n][label] = (up[n] - 2.0 * base[n] + down[n]) / (BUMP_BP * BUMP_BP)
    out: List[object] = []
    k = 0
    for r in results:
        if isinstance(r, _Priced):
            out.append(IntradayRisk(r.trade.status, r.trade.product, r.rate, r.pv, deltas[k], gammas[k]))
            k += 1
        else:
            out.append(r)
    return out
