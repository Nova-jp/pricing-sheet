"""
スワップションのATMプレミアム(RtGetで読むTraditionの PRIMACT_1)→ 満期までの標準偏差 σ√T(bp)と、満期までの日数
(LiveScreen.xlsm のVolシート)。

換算はフィード・朝バッチと同じ engine/common/option/swaption/convert.PremiumConverter(契約・年金・
ql.bachelierBlackFormulaImpliedVol)。UDFが返すのは σ√T と日数だけで、年率・1日あたりへの直し方は
Excelの数式にする(四則演算):
  ノーマルvol(営業日) = σ√T / √営業日数 × √252     BP vol(営業日) = σ√T / √営業日数   (bp/日)
  ノーマルvol(暦日)   = σ√T / √暦日数 × √365       BP vol(暦日)   = σ√T / √暦日数     (bp/日)
  - 営業日数は満期までの残り(engine/common/option/term.py。ライブは日中の残りも減らす)で、営業日ベースの値は
    フィード(LiveQuotes("vol"))のvolと同じになる。
  - 暦日数 = 満期日 − 取引日(Traditionのvolと同じ T = 暦日/365。convert.calendar_factor と同じ数え方)。
"""

from datetime import date, datetime
from typing import List, NamedTuple, Optional, Sequence

import QuantLib as ql

from engine.common.option import term
from engine.common.option.swaption.convert import PremiumConverter, expiry_date


class ExpiryDays(NamedTuple):
    expiry: date  # 満期日
    business_days: float  # 満期までの残りの営業日数(now 時点)
    calendar_days: int  # 満期日 − 取引日(暦日)


def expiry_days(trade_date: date, now: term.When, expiry: str) -> ExpiryDays:
    end = expiry_date(trade_date, str(expiry).strip().upper())
    return ExpiryDays(end, term.remaining_days(now, end), (end - trade_date).days)


def stdev_grid(
    curve: ql.YieldTermStructureHandle,
    trade_date: date,
    now: term.When,
    expiries: Sequence[str],
    tenors: Sequence[str],
    premiums: Sequence[Sequence],
) -> List[List[Optional[float]]]:
    """満期(行)×原資産(列)のプレミアム(bp)→ σ√T(bp)。プレミアムが無い・0以下・満期済みのセルは None。"""
    if len(premiums) != len(expiries) or any(len(row) != len(tenors) for row in premiums):
        raise ValueError(f"プレミアムの範囲は満期{len(expiries)}行 × 原資産{len(tenors)}列にしてください")
    ql.Settings.instance().evaluationDate = ql.Date.from_date(trade_date)
    converter = PremiumConverter(curve, trade_date, when=now)
    out = []
    for e, row in zip(expiries, premiums):
        line = []
        for t, premium in zip(tenors, row):
            v = converter.convert(f"{e}x{t}", premium if isinstance(premium, (int, float)) else None)
            line.append(None if v is None else v.stdev)
        out.append(line)
    return out
