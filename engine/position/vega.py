"""
会社のベガ(Raw_IRVOL)を、Positionシートのベガの格子(行=満期 × 列=原資産)に振り分ける。

■ 振り分け(ユーザー指示、2026-11-10)
  満期・原資産の表記(1W・3M・1Y・1.5Y等)を年数にし、格子の点に年数で線形に分ける。
  格子の点ちょうどならその点、最も短い点(満期は1W)より短ければ最も短い点、最も長い点より先は最も長い点に全部。
  例: 満期3D → 1W、10D → 1Wと2Wに按分、原資産6Y → 5Yと7Yに半分ずつ。
  合計は変わらない(SetupのRaw total と Grid total の差で確かめる)。

■ 年数
  D = 日/365、W = 7日/365、M = 月/12、Y = 年。ボラの格子の満期の点の並びと同じ考え方(暦日の年数)。
"""

import re
from typing import List, Optional, Sequence, Tuple

_LABEL = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([DWMY])\s*$", re.I)


def years(label) -> Optional[float]:
    """「3M」「1.5Y」「2W」等 → 年数。読めなければNone。"""
    m = _LABEL.match(str(label or ""))
    if not m:
        return None
    n, unit = float(m.group(1)), m.group(2).upper()
    return {"D": n / 365.0, "W": 7.0 * n / 365.0, "M": n / 12.0, "Y": n}[unit]


def split(x: float, axis: Sequence[float]) -> List[Tuple[int, float]]:
    """x を軸の点に線形に分ける(範囲外は端の点に全部)。"""
    if x <= axis[0]:
        return [(0, 1.0)]
    if x >= axis[-1]:
        return [(len(axis) - 1, 1.0)]
    for i in range(1, len(axis)):
        if x <= axis[i]:
            if x == axis[i]:
                return [(i, 1.0)]
            w = (x - axis[i - 1]) / (axis[i] - axis[i - 1])
            return [(i - 1, 1.0 - w), (i, w)]
    return [(len(axis) - 1, 1.0)]


def _axis(labels: Sequence[str], name: str) -> List[float]:
    out = []
    for label in labels:
        y = years(label)
        if y is None:
            raise ValueError(f"{name}の表記を読めません: {label!r}")
        out.append(y)
    if any(b <= a for a, b in zip(out, out[1:])):
        raise ValueError(f"{name}は短い順に並べてください: {list(labels)}")
    return out


def company_grid(datasets: Sequence, expiries: Sequence, tails: Sequence, vegas: Sequence, dataset: str,
                 expiry_labels: Sequence[str], tail_labels: Sequence[str]) -> List[List[object]]:
    """生シートの列(dataset・満期・原資産・ベガ)のうち dataset の行のベガを、格子に振り分けて足したもの。
    単位は生シートのまま。どの行も載らなかったマスは空欄("")。満期・原資産を読めない行・ベガが数値でない行は
    足さない(Setupの合計の差に出る)。"""
    e_axis = _axis(expiry_labels, "満期")
    t_axis = _axis(tail_labels, "原資産")
    out: List[List[object]] = [[""] * len(t_axis) for _ in e_axis]
    want = str(dataset or "").strip()
    n = max(len(datasets), len(expiries), len(tails), len(vegas)) if datasets else 0
    for k in range(n):
        row = [c[k] if k < len(c) else None for c in (datasets, expiries, tails, vegas)]
        if str(row[0] or "").strip() != want:
            continue
        ey, ty = years(row[1]), years(row[2])
        try:
            v = float(row[3])
        except (TypeError, ValueError):
            continue
        if ey is None or ty is None:
            continue
        for i, wi in split(ey, e_axis):
            for j, wj in split(ty, t_axis):
                cell = out[i][j]
                out[i][j] = (0.0 if cell == "" else cell) + v * wi * wj
    return out
