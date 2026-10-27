"""
LSEG(Python API、lseg.data)から JGB の銘柄情報と引け値を取得する専用モジュール。朝のバッチが使う。

■ 接続
  lseg.data のデスクトップセッション(LSEG Workspace が起動・ログイン済みであること。アプリキーは不要)。
  取得したデータ(LSEGコンテンツ)は data/historical.db にだけ保存し、コミットしない。

■ 取得するもの
  - 銘柄一覧: 検索API(GOV_CORP_INSTRUMENTS)で年限ごとに現存銘柄を引く。チェーン 0#JPTSY= は
    10/20/30/40年債しか含まないため使わない。RIC・ISIN・利率・償還日。
  - 引け値: ld.get_history の単利(小数3桁)。{RIC}JCL(MULTI CONTRIBUTORS EOD)の MID_YLD_1 を使い、
    JCLに無い日だけ 大和(=DWAT の B_YLD_1)→ みずほ(=MZST の B_YLD_1)の順で補う。JCLはユーザーと決めた配信元
    (docs/decisions.md。2026-10-25〜26の間だけ大和 =DWAT にしていた)。JCLの過去分は2021年9月からしか無く、
    大和の過去分は営業日の約9%が本当に欠けている(JCLと照合済み)。
    日本相互証券(=BBCL、BB引け値)・日本証券業協会(=JSDA)は契約外で取れない。
  - 毎朝の取得(fetch_jgbs)も過去分(fetch_close_history、先物のベーシスの過去分用)も同じ取り方で、期間だけが違う。
    スナップショットの CF_CLOSE は使わない(2026-10-27の朝、CF_CLOSE は前の営業日の引け 10/23 の値なのに
    VALUE_DT1 は10/26で、値と日付が食い違っていた)。履歴なら値と日付が必ず対応する。

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
_RIC = re.compile(r"^JP(\d{4})00(\d{2})=$")

# 引け値の配信元と単利のフィールド(先にあるものを優先する)
HISTORY_SOURCES: Tuple[Tuple[str, str], ...] = (("JCL", "MID_YLD_1"), ("DWAT", "B_YLD_1"), ("MZST", "B_YLD_1"))
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
                    if start <= d <= end:
                        best[(d, code)] = value
    return [JgbClose(d, code, y) for (d, code), y in sorted(best.items())]


def fetch_close_history(bonds: List[JgbRecord], start: date, end: date) -> List[JgbClose]:
    """bonds の start〜end の引け値の単利(JCL、無い日は大和・みずほで補う)。"""
    import lseg.data as ld

    ld.open_session()
    try:
        return _fetch_close_history(ld, bonds, start, end)
    finally:
        ld.close_session()


def fetch_jgbs(start: date, end: date, today: Optional[date] = None) -> Tuple[List[JgbRecord], List[JgbClose]]:
    """現存するJGBの銘柄情報と、全銘柄の start〜end の引け値の単利(fetch_close_history と同じ取り方)。"""
    import lseg.data as ld

    ld.open_session()
    try:
        bonds = _search_bonds(ld, today or date.today())
        closes = _fetch_close_history(ld, bonds, start, end)
    finally:
        ld.close_session()
    return bonds, closes
