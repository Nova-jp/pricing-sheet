"""
LSEG(Python API、lseg.data)から JGB の銘柄情報と引け値を取得する専用モジュール。朝のバッチが使う。

■ 接続
  lseg.data のデスクトップセッション(LSEG Workspace が起動・ログイン済みであること。アプリキーは不要)。
  取得したデータ(LSEGコンテンツ)は data/historical.db にだけ保存し、コミットしない。

■ 取得するもの
  - 銘柄一覧: 検索API(GOV_CORP_INSTRUMENTS)で年限ごとに現存銘柄を引く。チェーン 0#JPTSY= は
    10/20/30/40年債しか含まないため使わない。RIC・ISIN・利率・償還日。
  - 引け値: {RIC}JCL(MULTI CONTRIBUTORS EOD)の CF_CLOSE(単利%、小数3桁)と VALUE_DT1(引けの日付)。
    ユーザーと決めた配信元(docs/bond_option.md)。日本証券業協会の売買参考統計値(=JSDA)は契約外で取れない。

■ 銘柄コード
  RIC は JP{回号4桁}00{種別}=(種別 42=2年、45=5年、67=10年、69=20年、68=30年、54=40年)。
  銘柄コードは 年限の記号 + 回号(例 JP03800067= → JB380、JP04870042= → JN487)。
"""

import re
from datetime import date, datetime
from typing import Dict, List, NamedTuple, Optional, Tuple

# 種別 → (年限, 銘柄コードの記号)
KINDS: Dict[str, Tuple[str, str]] = {
    "42": ("2Y", "JN"),
    "45": ("5Y", "JS"),
    "67": ("10Y", "JB"),
    "69": ("20Y", "JL"),
    "68": ("30Y", "JX"),
    "54": ("40Y", "JU"),
}
CLOSE_SOURCE = "JCL"
_RIC = re.compile(r"^JP(\d{4})00(\d{2})=$")
_CHUNK = 100  # get_data 1回あたりのRIC数


class JgbRecord(NamedTuple):
    code: str  # 銘柄コード(JB380)
    ric: str
    isin: str
    coupon: float  # %
    maturity: date


class JgbClose(NamedTuple):
    as_of_date: date  # 引けの日付
    code: str
    simple_yield: float  # 単利%


def code_from_ric(ric: str) -> Optional[str]:
    """JP03800067= → JB380。JGB(固定利付)のRICでなければ None。"""
    m = _RIC.match(str(ric).strip())
    if not m or m.group(2) not in KINDS:
        return None
    return f"{KINDS[m.group(2)][1]}{int(m.group(1))}"


def _as_date(value) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        ts = value.to_pydatetime()  # pandas.Timestamp
    except AttributeError:
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return None
    return None if ts != ts else ts.date()  # NaT


def _as_float(value) -> Optional[float]:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v == v else None  # NaN


def _search_bonds(ld, today: date) -> List[JgbRecord]:
    from lseg.data import discovery

    records: Dict[str, JgbRecord] = {}
    for kind, (tenor, _) in KINDS.items():
        df = discovery.search(
            view=discovery.Views.GOV_CORP_INSTRUMENTS,
            query=f"Japan Government Fixed Coupon JGB{tenor}",
            filter="IsActive eq true",
            select="RIC,ISIN,DocumentTitle,MaturityDate,CouponRate",
            top=1000,
        )
        for row in df.to_dict("records"):
            ric = str(row.get("RIC") or "")
            code = code_from_ric(ric)
            maturity = _as_date(row.get("MaturityDate"))
            coupon = _as_float(row.get("CouponRate"))
            if code is None or not ric.endswith(f"{kind}=") or maturity is None or coupon is None:
                continue
            if maturity <= today:
                continue
            records[code] = JgbRecord(code, ric, str(row.get("ISIN") or ""), coupon, maturity)
    return sorted(records.values(), key=lambda r: (r.maturity, r.code))


def _fetch_closes(ld, bonds: List[JgbRecord]) -> List[JgbClose]:
    closes: List[JgbClose] = []
    by_ric = {f"{b.ric}{CLOSE_SOURCE}": b.code for b in bonds}
    rics = list(by_ric)
    for i in range(0, len(rics), _CHUNK):
        df = ld.get_data(rics[i:i + _CHUNK], ["CF_CLOSE", "VALUE_DT1"])
        for row in df.to_dict("records"):
            code = by_ric.get(str(row.get("Instrument")))
            value = _as_float(row.get("CF_CLOSE"))
            as_of = _as_date(row.get("VALUE_DT1"))
            if code is not None and value is not None and as_of is not None:
                closes.append(JgbClose(as_of, code, value))
    return closes


def fetch_jgbs(today: Optional[date] = None) -> Tuple[List[JgbRecord], List[JgbClose]]:
    """現存するJGBの銘柄情報と、各銘柄の直近の引け値(JCL)。"""
    import lseg.data as ld

    ld.open_session()
    try:
        bonds = _search_bonds(ld, today or date.today())
        closes = _fetch_closes(ld, bonds)
    finally:
        ld.close_session()
    return bonds, closes
