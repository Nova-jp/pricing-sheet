"""
LSEG(Python API、lseg.data)から Tradition のスワップションATMプレミアムのヒストリカルを取得する。朝のバッチが使う。

■ 銘柄
  RIC は JPY{満期}{原資産}TOATM=TRDT(例 JPY1Y10YTOATM=TRDT。満期が先、"X"は入らない)。
  ヒストリカルがあるのは 満期 1M,3M,6M,9M,1Y,2Y,3Y,4Y,5Y,7Y,10Y,15Y,20Y × 原資産 1Y〜10Y,15Y,20Y,30Y
  (2022-01-12から。原資産30Yは2026-11-09に追加。満期25Y・30Yは配信が無い)。
  満期+原資産が40年を超える銘柄(15Yx30Y・20Yx30Y)は格子に入れない(取引が無く、カーブ(40Yまで)の外挿に
  頼るため。ユーザー指示、2026-11-09)。フィード(live_feed.toml の "vol")も同じ規則で除く。満期2M・6Y・8Y・9Yは市場にクオートが無いので使わない(2Mのリアルタイムは2021-07-28で
  止まった値、6Y・8Y・9Yは値が無い。2026-10-26に確認)。間の満期は読む側が補間する(ユーザー了承)。

■ 項目(engine/common/swaption.py の「プレミアムの定義」)
  FWPM_OCPHM = ATMストラドルのフォワード・プレミアム(bp)、ATM_STRIKE = ATMのストライク(%、記録用)。
  ExcelのRDP.HistoricalPricingはこの項目名を受け付けないため、Python APIで取る(JGBの引け値と同じ)。

■ 接続
  lseg.data のデスクトップセッション(LSEG Workspace が起動・ログイン済みであること)。
  取得したデータ(LSEGコンテンツ)は data/historical.db にだけ保存し、コミットしない。
"""

import math
from datetime import date, datetime
from typing import Dict, List, NamedTuple, Optional, Sequence

EXPIRIES = ("1M", "3M", "6M", "9M", "1Y", "2Y", "3Y", "4Y", "5Y", "7Y", "10Y", "15Y", "20Y")
TENORS = ("1Y", "2Y", "3Y", "4Y", "5Y", "6Y", "7Y", "8Y", "9Y", "10Y", "15Y", "20Y", "30Y")
PREMIUM_FIELD = "FWPM_OCPHM"
STRIKE_FIELD = "ATM_STRIKE"
FIRST_DATE = date(2022, 1, 12)  # Traditionのヒストリカルの最初の日
_CHUNK = 20  # get_history 1回あたりのRIC数


class PremiumQuote(NamedTuple):
    as_of_date: date
    label: str  # 満期x原資産(例 1Yx10Y)
    premium: float  # bp
    atm_strike: Optional[float]  # %


def ric(expiry: str, tenor: str) -> str:
    return f"JPY{expiry.upper()}{tenor.upper()}TOATM=TRDT"


MAX_TOTAL_YEARS = 40  # 満期+原資産の上限(カーブの最長テナー)


def _years(period: str) -> float:
    p = period.strip().upper()
    n = float(p[:-1])
    return n / 12 if p.endswith("M") else n


def in_grid(expiry: str, tenor: str) -> bool:
    """格子に入れる銘柄か(満期+原資産が MAX_TOTAL_YEARS 以内)。"""
    return _years(expiry) + _years(tenor) <= MAX_TOTAL_YEARS


def labels() -> List[str]:
    return [f"{e}x{t}" for e in EXPIRIES for t in TENORS if in_grid(e, t)]


def _as_float(value) -> Optional[float]:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _as_date(value) -> date:
    return value.date() if isinstance(value, datetime) else date.fromisoformat(str(value)[:10])


def _parse(df, by_ric: Dict[str, str]) -> List[PremiumQuote]:
    """get_history の表(列 = (RIC, 項目)、1銘柄だけなら列 = 項目)→ 値のある行。"""
    quotes: List[PremiumQuote] = []
    if df is None or df.empty:
        return quotes
    multi = getattr(df.columns, "nlevels", 1) > 1
    for r, label in by_ric.items():
        if multi:
            if r not in df.columns.get_level_values(0):
                continue
            sub = df[r]
        else:
            sub = df
        if PREMIUM_FIELD not in sub.columns:
            continue
        strikes = sub[STRIKE_FIELD] if STRIKE_FIELD in sub.columns else None
        for idx, value in sub[PREMIUM_FIELD].items():
            premium = _as_float(value)
            if premium is None or premium <= 0:
                continue
            strike = _as_float(strikes[idx]) if strikes is not None else None
            quotes.append(PremiumQuote(_as_date(idx), label, premium, strike))
    return quotes


def fetch_premiums(start: date, end: date, names: Optional[Sequence[str]] = None) -> List[PremiumQuote]:
    """start〜end の日次のプレミアム(値のある日・銘柄だけ)。names: 「満期x原資産」の一覧(既定は全格子)。"""
    import lseg.data as ld

    by_ric = {}
    for name in names or labels():
        e, t = name.upper().split("X")
        by_ric[ric(e, t)] = f"{e}x{t}"
    rics = list(by_ric)
    quotes: List[PremiumQuote] = []
    ld.open_session()
    try:
        for i in range(0, len(rics), _CHUNK):
            chunk = rics[i:i + _CHUNK]
            df = ld.get_history(chunk, fields=[PREMIUM_FIELD, STRIKE_FIELD],
                                start=start.isoformat(), end=end.isoformat())
            quotes += _parse(df, {r: by_ric[r] for r in chunk})
    finally:
        ld.close_session()
    return quotes
