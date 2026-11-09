"""
現物とスワップのヒストリカル・ボラの比(BondOption.xlsmのOptionシートのVol ratio)。

スワップションのボラ(補間値)は現物と同じ満期のスワップのボラなので、現物のボラは
「スワップションのボラ × (現物のHV ÷ スワップのHV)」で見積もる(ユーザー指示)。比を掛けるのはシートの式で、
ここでは行ごとのHVと比だけを計算する。

■ 何を比べるか(ユーザー決定)
  - 現物: その行の銘柄の引け値(DBの jgb_closes、JCLの単利)の日次変化。単利 → 単価(F1、起点 = その日のT+1)
    → 半年複利(QuantLib、切り捨てない)に換えてから差を取る。価格ボラは複利ベースのMDを掛けるので単位をそろえる。
  - スワップ: 起算日 = その日のスポット(T+2)、満期日 = 銘柄の償還日のOIS(規約 STANDARD)のパーレートを、
    その日のDBのカーブで評価した日次変化。現物もスワップも同じ満期日に向かって1日ずつ短くなる(ロールダウンは
    両方に同じように入る)ので、そのまま比べる。
  - 日付: 取引日以前で、カーブと引け値の両方がある日付の直近 window+1 日(window 個の日次変化)。
    休日・欠損をまたぐ日は2日以上の変化になる(VolHVと同じ)。DBは昨日までの引けなので、今日の動きは入らない。
  - HV: 日次変化(bp)の2乗平均の平方根(平均を引かない。VolHVと同じ)。単位は bp/日(シートのbp vol と同じ)。

■ 速さ
  カーブの読み込みとスワップの評価が重い(100行×20日で約1秒)。日付×満期日ごとのレートと、行ごとの結果を
  DBの更新時刻ごとにプロセス内で覚え、F9で入力が変わらなければ計算し直さない。
"""

import math
from datetime import date, timedelta
from typing import Dict, List, NamedTuple, Sequence, Tuple

import QuantLib as ql

from engine.common import bond
from engine.common.bond import Jgb
from engine.common.conventions import CALENDAR
from engine.common.forward import ForwardPricer
from engine.common.terms import STANDARD, SwapTerms

SWAP_SPOT_LAG = 2  # スワップのスポット(T+2)。現物はT+1(bond.settlement_date)
BOND_SPOT_LAG = 1
_HORIZON_MARGIN = timedelta(days=10)  # カーブを読む範囲の余裕(償還日の休日調整分)


class VolRatio(NamedTuple):
    bond_hv: float  # bp/日
    swap_hv: float  # bp/日
    ratio: float  # bond_hv / swap_hv
    changes: int  # 日次変化の数(= window)
    first: date  # 窓の最初の日付(変化の起点)
    last: date  # 窓の最後の日付


def _rms(xs: Sequence[float]) -> float:
    return math.sqrt(sum(x * x for x in xs) / len(xs))


def bond_compound_yield(jgb: Jgb, simple_yield: float, as_of_date: date) -> float:
    """その日の引け値の単利 → 半年複利(%、切り捨てない)。受渡日はT+1。"""
    settle = bond.settlement_date(as_of_date, BOND_SPOT_LAG)
    clean = bond.price_from_simple_yield(jgb, simple_yield, settle)
    return bond.compound_yield(jgb, clean + bond.accrued_interest(jgb, settle), settle, truncate=False)


def swap_terms(as_of_date: date, maturity: date) -> SwapTerms:
    start = CALENDAR.advance(ql.Date.from_date(as_of_date), SWAP_SPOT_LAG, ql.Days).to_date()
    return SwapTerms(start=start, convention=STANDARD, end=maturity)


class CurveRates:
    """日付×満期日ごとのスワップレート(%)を、DBのカーブで計算して覚えておく。

    store: HistoricalStore(dates()・load(日付, until))。db_key が変わったら(朝バッチでDBが変わったら)作り直す。"""

    def __init__(self, store, db_key=None) -> None:
        self.store = store
        self.db_key = db_key
        self.curve_dates = store.dates()
        self.rates: Dict[Tuple[date, date], float] = {}

    def fill(self, needs: Dict[date, set]) -> None:
        """needs: {日付: {満期日, …}} のうち、まだ計算していないものを計算する(日付ごとにカーブを1回読む)。"""
        pricer = ForwardPricer()
        for d in sorted(needs):
            todo = [m for m in needs[d] if (d, m) not in self.rates]
            if not todo:
                continue
            curve = self.store.load(d, until=max(todo) + _HORIZON_MARGIN).curve
            pricer.set_curve(d, curve)
            for m in todo:
                self.rates[(d, m)] = pricer.rate(swap_terms(d, m))
            pricer.keep_only([])

    def rate(self, d: date, maturity: date) -> float:
        return self.rates[(d, maturity)]


def window_dates(curve_dates: Sequence[date], close_dates: Sequence[date], trade_date: date,
                 window: int, maturity: date) -> List[date]:
    """取引日以前で、カーブと引け値の両方がある直近 window+1 日(昇順)。足りなければエラー。"""
    closes = set(close_dates)
    both = [d for d in curve_dates if d in closes and d <= trade_date and swap_terms(d, maturity).start < maturity]
    if len(both) < window + 1:
        raise ValueError(f"カーブと引け値の両方がある日が{max(len(both) - 1, 0)}日分しかありません(窓 {window}日)")
    return both[-(window + 1):]


def vol_ratio(jgb: Jgb, closes: Dict[date, float], rates: CurveRates, trade_date: date, window: int) -> VolRatio:
    """1銘柄のHVと比。closes: その銘柄の {日付: 引け値の単利}。rates は fill 済みであること。"""
    dates = window_dates(rates.curve_dates, sorted(closes), trade_date, window, jgb.maturity)
    ys = [bond_compound_yield(jgb, closes[d], d) for d in dates]
    ss = [rates.rate(d, jgb.maturity) for d in dates]
    dy = [(b - a) * 100.0 for a, b in zip(ys, ys[1:])]
    ds = [(b - a) * 100.0 for a, b in zip(ss, ss[1:])]
    bond_hv, swap_hv = _rms(dy), _rms(ds)
    if swap_hv == 0:
        raise ValueError("スワップのHVが0です")
    return VolRatio(bond_hv, swap_hv, bond_hv / swap_hv, window, dates[0], dates[-1])


def parse_window(value) -> int:
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"HVの窓は営業日数です: {value!r}")
    if n != int(n) or n < 2:
        raise ValueError(f"HVの窓は2以上の整数です: {value!r}")
    return int(n)


def needed(curve_dates: Sequence[date], closes_by_code: Dict[str, Dict[date, float]],
           rows: Sequence[Tuple[str, Jgb]], trade_date: date, window: int) -> Dict[date, set]:
    """rows(銘柄コード, Jgb)の計算に要る {日付: {満期日}}。日付が足りない行は飛ばす(vol_ratio がエラーにする)。"""
    out: Dict[date, set] = {}
    for code, jgb in rows:
        try:
            dates = window_dates(curve_dates, sorted(closes_by_code.get(code, {})), trade_date, window, jgb.maturity)
        except ValueError:
            continue
        for d in dates:
            out.setdefault(d, set()).add(jgb.maturity)
    return out
