"""
分析ブック(SwapAnalysis.xlsm)のFutBasisシートのUDF。長期国債先物のつなぎ足・CTDのベーシスのヒストリカル、
限月を重ねたベーシス、1日分の銘柄ごとのベーシス(計算は engine/fut/history.py)。

■ 設計方針(FwdMonitor・VolHVのUDFと同じ)
  - QuantLibが要る計算(単価・先渡単価・交換係数・IRR・CTDの選択)とDBの読み出しだけをUDFにし、
    日次の差分・銭への換算(×100)・ロールや利上げの段差の調整・σはExcelの数式で計算する。
  - 単価・ベーシスは額面100円あたりの円、利回り・レポ・IRRは%(jgb_futures と同じ)。
  - 計算方法は「手動」+F9。非volatile。配列はそのまま返してスピルに任せる。値の無いところは空欄("")。
  - DBの読み出しと計算は、引数とDBの更新時刻ごとにプロセス内で覚えておく。
■ レポの段(steps)
  [Start, Rate] の2列の範囲。空欄(または範囲の全部が空欄)ならTONA実績から自動で作った段(FutRepoSteps)を使う。
  シートで段を手で上書きするときは、FutRepoSteps の結果を見ながら手入力の表を作り、その範囲を渡す。
"""

from functools import lru_cache
from typing import List, Optional, Tuple

import xlwings as xw

from engine.common import jgb_futures as jf
from engine.common import udf_common
from engine.common.historical_store import HistoricalStore
from engine.common.jgb_futures import Contract, RepoStep
from engine.common.udf_common import _is_blank
from engine.fut import history

Steps = Optional[Tuple[RepoStep, ...]]  # None = TONA実績から自動で


def _db_key() -> Tuple[str, int]:
    store = udf_common._historical_store()
    return str(store.db_path), store.db_path.stat().st_mtime_ns


@lru_cache(maxsize=2)
def _data_cached(db_path: str, db_key: int) -> history.FutData:
    store = HistoricalStore(db_path)
    with store:
        return history.load(store)


def _data() -> history.FutData:
    return _data_cached(*_db_key())


def _steps(rows) -> Steps:
    """シートの [Start, Rate] の範囲 → 段。全部空欄なら None(自動)。片方だけ入った行はエラー。"""
    steps = []
    for row in rows or []:
        cells = list(row or []) + [None, None]
        start, rate = cells[0], cells[1]
        if _is_blank(start) and _is_blank(rate):
            continue
        if _is_blank(start) or _is_blank(rate):
            raise ValueError(f"レポの段は Start と Rate の両方を入れてください: {row!r}")
        steps.append(RepoStep(udf_common._to_date(start), float(rate)))
    return tuple(sorted(steps)) if steps else None


def _resolve_steps(steps: Steps, data: history.FutData) -> Tuple[RepoStep, ...]:
    return steps if steps is not None else tuple(jf.repo_steps(data.fixings))


def _contracts(values) -> Tuple[Contract, ...]:
    contracts = tuple(Contract.parse(v) for v in (values or []) if not _is_blank(v))
    if not contracts:
        raise ValueError("限月を1つ以上指定してください(例 2026-12)")
    return contracts


def _blank(x):
    return "" if x is None else x


@lru_cache(maxsize=4)
def _front_cached(steps: Steps, db_path: str, db_key: int) -> List[history.FrontDay]:
    data = _data_cached(db_path, db_key)
    return history.front_history(data, _resolve_steps(steps, data))


@lru_cache(maxsize=8)
def _overlay_cached(contracts, steps: Steps, field: str, db_path: str, db_key: int):
    data = _data_cached(db_path, db_key)
    return history.overlay(data, contracts, _resolve_steps(steps, data), field)


@xw.func
def FutRepoSteps() -> List[list]:
    """
    TONA実績(DB)から作ったレポの段。見出し行 [Start, Rate] + 段ごとの行。
    利上げ・利下げ(水準の変化)のところだけ段が変わる(規則は engine/common/jgb_futures.repo_steps)。
    Rate はその段の中のTONAの中央値(%)。
    """
    return [["Start", "Rate"]] + [[s.start, s.rate] for s in jf.repo_steps(_data().fixings)]


FRONT_HEADER = [
    "Date", "Contract", "Settle", "dSettle", "Roll", "Repo",
    "CTD", "CF", "Yield", "Price", "Gross", "Carry", "Net", "IRR",
]


@xw.func
@xw.arg("steps", ndim=2)
def FutHistory(steps=None) -> List[list]:
    """
    期近のつなぎ足(取引最終日の翌日に次の限月へ乗り換え)と、その日のCTDのベーシス。日付の昇順。
      Contract: その日の期近("2026-12")  Settle: 清算値
      dSettle : 同じ限月の前の日からの変化(ロールの日も新しい限月どうし。積み上げれば段差の無い系列になる)
      Roll    : 限月が変わった日は1、それ以外は0  Repo: その日の段のレポ(%)
      CTD     : ネットベーシス最小の受渡適格銘柄。CF: 交換係数  Yield: 引け値の単利(%)  Price: 単価
      Gross/Carry/Net: グロスベーシス・キャリー・ネットベーシス(円)  IRR: インプライドレポ(%)
    レポの段より前の日・適格銘柄の引け値が無い日は、CTD以降の列が空欄。
    steps: レポの段([Start, Rate] の範囲。空欄=TONA実績から自動)。
    """
    rows: List[list] = [list(FRONT_HEADER)]
    for day in _front_cached(_steps(steps), *_db_key()):
        q, c = day.quote, day.ctd
        ctd_cols = (
            ["", "", "", "", "", "", "", ""] if c is None else
            [c.code, c.conversion_factor, c.simple_yield, c.price, c.gross_basis, c.carry, c.net_basis, c.implied_repo]
        )
        rows.append(
            [q.as_of, q.contract.label, q.settle, _blank(q.change), int(q.rolled), _blank(day.repo)] + ctd_cols
        )
    return rows


@xw.func
@xw.arg("contracts", ndim=1)
@xw.arg("steps", ndim=2)
def FutOverlay(contracts, field="NET", steps=None) -> List[list]:
    """
    限月を重ねたCTDのベーシス。見出し行 [BD, 限月1, …, 限月n] + 取引最終日までの営業日数(BD)の降順の行。
    限月ごとに、清算値のある日(期近になる前も含む)のCTDの値を、その日から取引最終日までの営業日数に並べる。
    field: NET(ネットベーシス、既定)/ GROSS / CARRY(円)/ IRR(%)。値の無いところは空欄。
    steps: レポの段(FutHistory と同じ)。
    """
    names = _contracts(contracts)
    key = str(field or "NET").strip().upper()
    if key not in history.OVERLAY_FIELDS:
        raise ValueError(f"field は {'/'.join(history.OVERLAY_FIELDS)} のどれかです: {field!r}")
    series = _overlay_cached(names, _steps(steps), key, *_db_key())
    days = sorted({bd for points in series.values() for bd in points}, reverse=True)
    rows: List[list] = [["BD"] + [c.label for c in names]]
    for bd in days:
        rows.append([bd] + [_blank(series[c].get(bd)) for c in names])
    return rows


DAY_HEADER = [
    "Code", "Coupon", "Maturity", "CF", "Yield", "Close date", "Price", "Gross", "Carry", "Net", "IRR", "CTD",
]


@xw.func
@xw.arg("yields", ndim=2)
def FutBasis(contract, as_of=None, futures_price=None, repo=None, yields=None) -> List[list]:
    """
    1日分の受渡適格銘柄ごとのベーシス(償還日の順)。見出し行 + 銘柄ごとの行 + 最後に [Futures, 先物価格, Repo, レポ]。
      CF: 交換係数  Yield: 単利(%)  Close date: 引け値の日付(その日以前で直近)  Price: 単価(T+1)
      Gross/Carry/Net: 円  IRR: %  CTD: ネットベーシス最小の銘柄は1、それ以外は0
    contract: 限月("2026-12" または "JGBZ6")。as_of: 評価日(空欄=今日)。
    futures_price: 先物価格(空欄=その日のDBの清算値。ライブの値を使うときはRtGetのセル等を渡す)。
    repo: レポ(%、空欄=その日以前で直近のTONA実績)。
    yields: 引け値の上書き([Code, Yield] の範囲。空欄の行は無視)。
    """
    c = Contract.parse(contract)
    d = udf_common._resolve_valuation_date(as_of)
    overrides = {
        str(row[0]).strip().upper(): float(row[1])
        for row in (yields or []) if row and len(row) >= 2 and not _is_blank(row[0]) and not _is_blank(row[1])
    }
    day = history.basis_on(
        _data(), c, d,
        None if _is_blank(futures_price) else float(futures_price),
        None if _is_blank(repo) else float(repo),
        overrides,
    )
    ctd = jf.cheapest([r for r, _ in day.rows])
    rows: List[list] = [list(DAY_HEADER)]
    for r, close_date in day.rows:
        rows.append([
            r.code, r.coupon, r.maturity, r.conversion_factor, r.simple_yield, close_date, r.price,
            r.gross_basis, r.carry, r.net_basis, r.implied_repo, int(r is ctd),
        ])
    width = len(DAY_HEADER)
    rows.append(["Futures", day.futures_price, "Repo", day.repo] + [""] * (width - 4))
    return rows
