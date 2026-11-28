"""
長期国債先物オプションの板のボラ(LiveScreen.xlsm のFutOptシート)。

気配(Bid・Ask、RtGetで読むLSEGの BID・ASK)をストライクごとに、Black式(割引なし)の価格ボラ(%/年)に直す。
定義はPositionSheetの国債先物オプションと同じ(pricer.implied_price_vol。アメリカン型をヨーロピアンで近似):
  - 先物の価格 F = シートで決めた先物の値(既定は先物のMid)。
  - 期間 T = 取引最終日の15時までの残りの営業日数 / 252(engine/common/option/term.py。日中も減る)。
限月の規則(原資産・取引最終日)は engine/common/jgb_futures.py。RICの組み立てはシートの数式。
"""

from datetime import date
from typing import List, NamedTuple, Optional, Sequence

from engine.common import jgb_futures
from engine.common.option.bond import pricer


class Contract(NamedTuple):
    underlying: str  # 原資産の先物のRIC(JGBZ6)
    expiry: date  # 取引最終日


def contract(option_month: date) -> Contract:
    """限月(その月の日付)→ 原資産の先物のRICと取引最終日(暦だけで決まる)。"""
    y, m = option_month.year, option_month.month
    return Contract(jgb_futures.option_underlying(y, m).ric(True), jgb_futures.option_last_trade_date(y, m))


def _quote(value) -> Optional[float]:
    """気配の値。数値でない(RtGetの空欄・エラー表示)・0以下は None。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value > 0 else None


def _vol(option_type, forward: float, strike: float, premium: Optional[float], t: float) -> Optional[float]:
    """気配が無い・本源的価値以下(ボラが無い)ときは None。"""
    if premium is None:
        return None
    try:
        return pricer.implied_price_vol(option_type, forward, strike, premium, t)
    except (ValueError, RuntimeError):
        return None


def vol_rows(option_type, forward: float, strikes: Sequence, bids: Sequence, asks: Sequence,
             t: float) -> List[List[Optional[float]]]:
    """ストライクごとの [Bid のボラ, Ask のボラ](%/年)。ストライクが空欄の行・ボラが無いセルは None。"""
    pricer.parse_option_type(option_type)
    if not (len(strikes) == len(bids) == len(asks)):
        raise ValueError(f"ストライク・Bid・Askの行数をそろえてください: {len(strikes)}, {len(bids)}, {len(asks)}")
    if t <= 0:
        raise ValueError("取引最終日を過ぎています")
    out = []
    for k, bid, ask in zip(strikes, bids, asks):
        strike = _quote(k)
        if strike is None:
            out.append([None, None])
            continue
        out.append([_vol(option_type, forward, strike, _quote(bid), t),
                    _vol(option_type, forward, strike, _quote(ask), t)])
    return out
