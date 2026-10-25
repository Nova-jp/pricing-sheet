"""
LSEG(Python API、lseg.data)から JGB の銘柄情報と引け値を取得する専用モジュール。朝のバッチが使う。

■ 接続
  lseg.data のデスクトップセッション(LSEG Workspace が起動・ログイン済みであること。アプリキーは不要)。
  取得したデータ(LSEGコンテンツ)は data/historical.db にだけ保存し、コミットしない。

■ 取得するもの
  - 銘柄一覧: 検索API(GOV_CORP_INSTRUMENTS)で年限ごとに現存銘柄を引く。チェーン 0#JPTSY= は
    10/20/30/40年債しか含まないため使わない。RIC・ISIN・利率・償還日。
  - 引け値: {RIC}DWAT(大和証券)の CF_YIELD(単利%、小数3桁)と VALUE_DT1(引けの日付)。
    ユーザーと決めた配信元(docs/decisions.md)。日本相互証券(=BBCL、BB引け値)・日本証券業協会(=JSDA)は
    契約外で取れない。JCL(MULTI CONTRIBUTORS EOD)・=MZST(みずほ)と同じ値で、=MZSTには40年債が無い。
  - 引け値の過去分(fetch_close_history、先物のベーシスの過去分用): ld.get_history の単利。大和の過去分は
    営業日の約9%が欠けている(値が変わらなかった日ではなく、本当に無い。JCLと照合済み)ので、
    大和 → みずほ(=MZST の B_YLD_1)→ JCL(MID_YLD_1、2021年9月から)の順で、無い日だけを補う。

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
CLOSE_SOURCE = "DWAT"
CLOSE_FIELD = "CF_YIELD"  # 単利。CF_CLOSEは配信元によって単価・単利が混在するため使わない
_RIC = re.compile(r"^JP(\d{4})00(\d{2})=$")
_CHUNK = 100  # get_data 1回あたりのRIC数


# 過去分の配信元と単利のフィールド(先にあるものを優先する)
HISTORY_SOURCES: Tuple[Tuple[str, str], ...] = (("DWAT", "B_YLD_1"), ("MZST", "B_YLD_1"), ("JCL", "MID_YLD_1"))
_HISTORY_CHUNK = 50  # get_history 1回あたりのRIC数


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
        df = ld.get_data(rics[i:i + _CHUNK], [CLOSE_FIELD, "VALUE_DT1"])
        for row in df.to_dict("records"):
            code = by_ric.get(str(row.get("Instrument")))
            value = _as_float(row.get(CLOSE_FIELD))
            as_of = _as_date(row.get("VALUE_DT1"))
            if code is not None and value is not None and as_of is not None:
                closes.append(JgbClose(as_of, code, value))
    return closes


def history_columns(df, rics: List[str], field: str) -> Dict[str, object]:
    """ld.get_history の結果 → {RIC: 列}。RICが1つだと列名がフィールド名になるのをそろえる。"""
    columns = list(df.columns)
    if len(rics) == 1 and columns == [field]:
        return {rics[0]: df[field]}
    return {str(c): df[c] for c in columns}


def _history_values(column) -> Dict[date, float]:
    out: Dict[date, float] = {}
    for d, v in column.items():
        day, value = _as_date(d), _as_float(v)
        if day is not None and value is not None:
            out[day] = value
    return out


def _fetch_close_history(ld, bonds: List[JgbRecord], start: date, end: date) -> List[JgbClose]:
    best: Dict[Tuple[date, str], float] = {}
    for source, field in reversed(HISTORY_SOURCES):  # 優先度の低い順に入れ、高い方で上書きする
        by_ric = {f"{b.ric}{source}": b.code for b in bonds}
        rics = list(by_ric)
        for i in range(0, len(rics), _HISTORY_CHUNK):
            chunk = rics[i:i + _HISTORY_CHUNK]
            try:
                df = ld.get_history(chunk, fields=[field], start=start.isoformat(), end=end.isoformat())
            except Exception:  # 期間内にデータが1件も無いチャンクはエラーになる
                continue
            for ric, column in history_columns(df, chunk, field).items():
                code = by_ric.get(ric)
                if code is None:
                    continue
                for d, value in _history_values(column).items():
                    best[(d, code)] = value
    return [JgbClose(d, code, y) for (d, code), y in sorted(best.items())]


def fetch_close_history(bonds: List[JgbRecord], start: date, end: date) -> List[JgbClose]:
    """bonds の start〜end の引け値の単利(大和、無い日はみずほ・JCLで補う)。"""
    import lseg.data as ld

    ld.open_session()
    try:
        return _fetch_close_history(ld, bonds, start, end)
    finally:
        ld.close_session()


def fetch_jgbs(today: Optional[date] = None) -> Tuple[List[JgbRecord], List[JgbClose]]:
    """現存するJGBの銘柄情報と、各銘柄の直近の引け値(大和、=DWAT)。"""
    import lseg.data as ld

    ld.open_session()
    try:
        bonds = _search_bonds(ld, today or date.today())
        closes = _fetch_closes(ld, bonds)
    finally:
        ld.close_session()
    return bonds, closes
