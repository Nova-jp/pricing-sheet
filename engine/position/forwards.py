"""
Fwd(n年先1年)で持つと決めたリスクを、スポットのピラーのデルタに直す(Positionシートの分解の列)。

■ 方法(ユーザーと決定、2026-11-23)
  ピラーの行ごとに「そのピラーの年限先から1年」のFwdのOISを1本組む(5Yの行 → 5Y1Y、6Mの行 → 6M1Y、
  O/Nの行は先日付なし = スポットの1Y)。規約は日中の取引と同じ標準のOIS(trades.trade_params)、
  デルタは risk.bucketed_deltas(同じバンプ)。
  デルタは想定元本に比例するので、想定元本1(百万円)で出したピラーごとのデルタを、その合計が入れたリスクに
  なるように拡大・縮小する(リスク = ピラーごとのデルタの合計)。全行の分を足してピラーの並びで返す。
  入れたリスクの符号はそのまま使う(シートのデルタと同じ、金利が1bp上がったときのPVの変化)。

■ 例: 5Y1Yにリスク+1.0 → 6Yに大きくプラス、5Yに大きくマイナス(合計は+1.0)。12Yより先のFwdは
  カーブの補間の比率で近くのピラーに分かれて載る。
"""

import re
from typing import Dict, List, NamedTuple, Optional, Sequence

from engine.common.market import CurveSet
from engine.common.risk import bucketed_deltas
from engine.position import trades

FORWARD_TENOR = "1Y"
_HALF_YEAR = re.compile(r"^(\d+)\.5Y$")


class ForwardRisk(NamedTuple):
    pillar: str  # 起点のピラー(「5Y」→ 5Y1Y)
    risk: float  # 入れたリスク(百万円/bp)
    notional: float  # リスクに合う想定元本(百万円、Recを正)
    deltas: Dict[str, float]  # カーブのラベル(小文字)→ 百万円/bp


def forward_start(pillar: str) -> Optional[str]:
    """ピラーのラベル → スポットからの先日付(None = スポット)。「1.5Y」は「18M」に直す。"""
    label = str(pillar).strip().upper()
    if label in ("O/N", "ON", "T/N", "TN", "S/N", "SN"):
        return None
    m = _HALF_YEAR.match(label)
    if m:
        return f"{int(m.group(1)) * 12 + 6}M"
    return label


def forward_risks(curves: CurveSet, pillars: Sequence[str], risks: Sequence[Optional[float]]) -> List[ForwardRisk]:
    """リスクの入った行(0・空欄は飛ばす)ごとの ForwardRisk。"""
    if len(pillars) != len(risks):
        raise ValueError(f"ピラーとFwdのリスクの行数が違います({len(pillars)}行と{len(risks)}行)")
    rows = [(p, r) for p, r in zip(pillars, risks) if r]
    params = [trades.trade_params(curves, trades.Trade(forward_start(p), FORWARD_TENOR, 1.0, "REC")) for p, _ in rows]
    unit = bucketed_deltas(curves, params) if params else []
    out: List[ForwardRisk] = []
    for (pillar, risk), deltas in zip(rows, unit):
        total = sum(deltas.values())
        if total == 0:
            raise ValueError(f"{pillar}1Yのデルタが0なので、リスクから想定元本を決められません")
        scale = risk / total
        out.append(ForwardRisk(pillar, risk, scale, {k: v * scale for k, v in deltas.items()}))
    return out


def total_deltas(rows: Sequence[ForwardRisk]) -> Dict[str, float]:
    """全行のFwdのスポットのデルタの合計(カーブのラベル(小文字)→ 百万円/bp)。"""
    total: Dict[str, float] = {}
    for r in rows:
        for k, v in r.deltas.items():
            total[k] = total.get(k, 0.0) + v
    return total
