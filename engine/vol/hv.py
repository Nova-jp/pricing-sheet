"""
インプライド(スワップションATMノーマルvol)と比べるヒストリカル・ボラの材料(分析ブックのVolHVシート)と、
ボラの水準とFwdの水準の散布図(VolLevelシート)の系列。Fwdは朝バッチが計算してDBに置いた
swaption_forwards(engine/common/swaption_forward.py)を読むだけで、ここではQuantLibを使わない。
窓の集計・年率換算・ボラの差(カーブ・フライ)・回帰はExcelの数式で行う。

■ 何と比べるか
  Tm x U(例 1Yx10Y)のIVは「満期日の2営業日後スタート・U年」のFwdスワップレートのボラなので、HVも同じFwdの
  変化で測る(スポットのU年で代用すると、短い原資産・長い満期ほどずれる)。契約はvolの換算と同じ
  engine/common/swaption.underlying(満期日=取引日+満期、起算日=満期日+2営業日)。

■ 日次変化(bp)
  日付Dの変化 = 「Dの契約」を、Dのカーブと前の日付のカーブで評価した差(DBの forward − forward_prev)。
  満期一定のFwd(1Yx10Yなど)を日ごとに比べると、日がたつだけで動く分(ロールダウン)が混ざるため、
  同じ起算日・満期日の契約どうしで比べる。前の日付はDBでカーブのある直前の日
  (休日・欠損をまたぐ日は2日以上の変化になる)。
"""

from dataclasses import dataclass, field
from datetime import date
from typing import List, Mapping, Optional, Sequence

from engine.common.historical_store import SwaptionForwardRow
from engine.common.swaption import parse_label, underlying
from engine.common.terms import SwapTerms

__all__ = ["VolSeries", "compute", "live_terms", "normalize", "parse_label"]


def normalize(label) -> str:
    """銘柄の表記をDBと同じ「1Yx10Y」にそろえる。"""
    e, t = parse_label(str(label).strip())
    return f"{e}x{t}"


def live_terms(labels: Sequence[str], as_of_date: date) -> List[SwapTerms]:
    """ライブ(今日のカーブ)で評価する、今日の契約の原資産。DBの各日の契約と同じ決め方(swaption.underlying)。"""
    return [underlying(as_of_date, label).terms for label in labels]


@dataclass
class VolSeries:
    """行=Fwdのある日付(最初の日付は前の日付が無く変化が無いので含めない)。列=銘柄。"""

    labels: List[str]
    dates: List[date] = field(default_factory=list)
    iv: List[List[Optional[float]]] = field(default_factory=list)  # bp/年。DBに無い日は None
    fwd: List[List[float]] = field(default_factory=list)  # その日のFwd(%)
    dfwd: List[List[float]] = field(default_factory=list)  # 日次変化(bp)


def compute(
    forwards: Mapping[date, Mapping[str, SwaptionForwardRow]],
    vol_quotes: Mapping[date, Mapping[str, float]],
    labels: Sequence[str],
) -> VolSeries:
    """forwards: DBの swaption_forwards({日付: {銘柄: 行}})。
    vol_quotes: {日付: {銘柄: IV}}(DBのvol_quotes。銘柄名の大文字小文字は問わない)。"""
    series = VolSeries(labels=[normalize(name) for name in labels])
    if not forwards:
        raise ValueError("DBに原資産のFwd(swaption_forwards)がありません。朝バッチを実行してください")
    present = {name for rows in forwards.values() for name in rows}
    missing = [name for name in series.labels if name not in present]
    if missing:
        raise ValueError(f"DBの原資産のFwdに無い銘柄です(朝バッチの格子の外): {', '.join(missing)}")
    for d in sorted(forwards):
        rows = forwards[d]
        picked = [rows.get(name) for name in series.labels]
        if any(r is None or r.forward_prev is None for r in picked):
            continue  # 最初の日付(前の日付が無い)・その日の計算に失敗した銘柄がある日
        ivs = {k.upper(): v for k, v in vol_quotes.get(d, {}).items()}
        series.dates.append(d)
        series.iv.append([ivs.get(name.upper()) for name in series.labels])
        series.fwd.append([r.forward for r in picked])
        series.dfwd.append([(r.forward - r.forward_prev) * 100.0 for r in picked])
    return series
