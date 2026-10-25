"""
LSEG(Python API、lseg.data)から長期国債先物の清算値を取得する専用モジュール。朝のバッチが使う。

■ 接続
  lseg.data のデスクトップセッション(jgb_source と同じ。LSEG Workspace が起動・ログイン済みであること)。
  取得したデータ(LSEGコンテンツ)は data/historical.db にだけ保存し、コミットしない。

■ 取得するもの
  - 取引中の限月: チェーン 0#JGB:(期近から3限月)。受渡日 DELIV_DATE・取引最終日 LSTTRDDATE を、
    jgb_futures の規則で出した日付と照らし合わせる(食い違えば警告を返す。規則の方を使う)。
  - 清算値: ld.get_history の SETTLE(日次)。取引中は JGBZ6、満了後は JGBU6^2(jgb_futures.Contract.ric)。
    満了した限月はリアルタイムのレコードが無い(get_data は Not In Cache)が、ヒストリカルは取れる。
  CTD・交換係数・インプライドレポはLSEGから取れない(TR.ConversionFactor 等はPython APIの権限なし)ので、
  engine/common/jgb_futures.py で計算する。
"""

from datetime import date
from typing import Dict, List, NamedTuple, Optional, Tuple

from engine.common.jgb_futures import Contract, delivery_date, last_trade_date
from engine.common.jgb_source import _as_date, history_columns, _history_values

CHAIN = "0#JGB:"
SETTLE_FIELD = "SETTLE"


class ListedContract(NamedTuple):
    contract: Contract
    ric: str
    delivery: Optional[date]  # LSEGの値(照合用)
    last_trade: Optional[date]


class FuturesFetch(NamedTuple):
    settles: List[Tuple[date, str, float]]  # (日付, 限月 "2026-12", 清算値)
    listed: List[ListedContract]
    warnings: List[str]


def _listed(ld) -> List[ListedContract]:
    df = ld.get_data([CHAIN], ["DELIV_DATE", "LSTTRDDATE"])
    out = []
    for row in df.to_dict("records"):
        ric = str(row.get("Instrument") or "")
        try:
            contract = Contract.parse(ric)
        except ValueError:
            continue
        out.append(ListedContract(contract, ric, _as_date(row.get("DELIV_DATE")), _as_date(row.get("LSTTRDDATE"))))
    return out


def _check_dates(listed: List[ListedContract]) -> List[str]:
    warnings = []
    for x in listed:
        for name, lseg, rule in (
            ("受渡日", x.delivery, delivery_date(x.contract)), ("取引最終日", x.last_trade, last_trade_date(x.contract)),
        ):
            if lseg is not None and lseg != rule:
                warnings.append(f"{x.contract.label}限の{name}: LSEG {lseg} / 規則 {rule}(規則の方を使います)")
    return warnings


def _fetch(ld, contracts: Dict[Contract, date], end: date) -> FuturesFetch:
    listed = _listed(ld)
    listed_set = {x.contract for x in listed}
    settles: List[Tuple[date, str, float]] = []
    for contract, start in sorted(contracts.items()):
        if start > end:
            continue
        ric = contract.ric(listed=contract in listed_set)
        try:
            df = ld.get_history(ric, fields=[SETTLE_FIELD], start=start.isoformat(), end=end.isoformat())
        except Exception:  # 期間内にデータが無い(まだ上場前・取得済み)
            continue
        for column in history_columns(df, [ric], SETTLE_FIELD).values():
            for d, value in _history_values(column).items():
                settles.append((d, contract.label, value))
    return FuturesFetch(settles, listed, _check_dates(listed))


def fetch_settles(contracts: Dict[Contract, date], end: date) -> FuturesFetch:
    """contracts: {限月: その限月を取り始める日}。end までの清算値と、取引中の限月の一覧。"""
    import lseg.data as ld

    ld.open_session()
    try:
        return _fetch(ld, contracts, end)
    finally:
        ld.close_session()
