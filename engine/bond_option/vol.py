"""
スワップションのATMノーマルvol(満期×原資産の格子)から、債券オプションのボラを補間する。

格子はフィード(live_feed.toml の "vol"、JPYTO{満期}X{原資産}ATM=R の MID_IV_NOR、bp/年)で、
LiveQuotes("vol") が返す行 [銘柄(例 1Mx10Y), RIC, 値] の銘柄と値を使う。

■ 補間(ユーザーと決めた方式)
  - 軸: 満期 = 取引日→オプション満期、テナー = オプション満期→償還日(行使時点の債券の残存)。
    年数はどちらも暦日/365。格子の点も取引日から期間を足した日付で暦日/365にする。
  - テナー方向: 満期ごとに vol を線形補間。
  - 満期方向: 分散(σ²t)を線形補間。
  - 格子の外(1M未満の満期、20Y超の原資産等)は端の値で平らに延ばす。
  - 値の無い点(#N/A・空欄・0以下)は使わない。満期の行に値が1つも無ければその満期を使わない。
bp/日への換算(ノーマルvol/√252)はExcelの数式で行う(ここはbp/年のまま返す)。
"""

import math
import re
from bisect import bisect_left
from datetime import date
from typing import Dict, Iterable, List, Sequence, Tuple

import QuantLib as ql

from engine.common.bond import NO_LEAP

_LABEL = re.compile(r"^\s*(\d+[MY])\s*X\s*(\d+[MY])\s*$", re.I)


def parse_label(label) -> Tuple[ql.Period, ql.Period]:
    """"1Mx10Y" → (満期, 原資産)。"""
    m = _LABEL.match(str(label))
    if not m:
        raise ValueError(f"満期x原資産の表記ではありません: {label!r}")
    return ql.Period(m.group(1).upper()), ql.Period(m.group(2).upper())


def _years(start: date, end: date) -> float:
    return NO_LEAP.dayCount(ql.Date.from_date(start), ql.Date.from_date(end)) / 365


def _expiry_years(trade_date: date, period: ql.Period) -> float:
    """満期の点: 取引日に期間を足した日付までの年数(オプション満期と同じ物差し)。"""
    return _years(trade_date, (ql.Date.from_date(trade_date) + period).to_date())


def _tenor_years(period: ql.Period) -> float:
    """原資産の点: 期間の年数(10Y=10、6M=0.5)。"""
    return period.length() / 12 if period.units() == ql.Months else float(period.length())


def _as_vol(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v > 0 and math.isfinite(v) else None


def _linear(xs: Sequence[float], ys: Sequence[float], x: float) -> float:
    """線形補間、範囲外は端の値。xsは昇順。"""
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    i = bisect_left(xs, x)
    x0, x1, y0, y1 = xs[i - 1], xs[i], ys[i - 1], ys[i]
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def build_grid(rows: Iterable[Sequence], trade_date: date) -> Dict[float, List[Tuple[float, float]]]:
    """[銘柄, …, 値] の行 → {満期の年数: [(原資産の年数, vol)](原資産の昇順)}。"""
    grid: Dict[float, Dict[float, float]] = {}
    for row in rows:
        if not row or row[0] in (None, ""):
            continue
        vol = _as_vol(row[-1])
        if vol is None:
            continue
        expiry, tenor = parse_label(row[0])
        grid.setdefault(_expiry_years(trade_date, expiry), {})[_tenor_years(tenor)] = vol
    return {t: sorted(points.items()) for t, points in grid.items() if points}


def interpolate(
    grid: Dict[float, List[Tuple[float, float]]], expiry_years: float, tenor_years: float,
) -> float:
    if not grid:
        raise ValueError("ボラの格子に値がありません(フィードの \"vol\" と live_feed.toml を確認してください)")
    expiries = sorted(grid)
    vols = []
    for t in expiries:
        tenors, values = zip(*grid[t])
        vols.append(_linear(tenors, values, tenor_years))
    if expiry_years <= expiries[0]:
        return vols[0]
    if expiry_years >= expiries[-1]:
        return vols[-1]
    variance = _linear(expiries, [v * v * t for v, t in zip(vols, expiries)], expiry_years)
    return math.sqrt(variance / expiry_years)


def bond_option_vol(rows: Iterable[Sequence], trade_date: date, expiry: date, maturity: date) -> float:
    """取引日・オプション満期・債券の償還日 → 補間したノーマルvol(bp/年)。"""
    return grid_vol(build_grid(rows, trade_date), trade_date, expiry, maturity)


def grid_vol(
    grid: Dict[float, List[Tuple[float, float]]], trade_date: date, expiry: date, maturity: date,
) -> float:
    """作り済みの格子から補間する(表UDFは格子を表全体で1回だけ作る)。"""
    if expiry <= trade_date:
        raise ValueError(f"満期 {expiry} が取引日 {trade_date} 以前です")
    if maturity <= expiry:
        raise ValueError(f"償還日 {maturity} がオプション満期 {expiry} 以前です")
    return interpolate(grid, _years(trade_date, expiry), _years(expiry, maturity))
