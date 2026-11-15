"""
スワップションのATMプレミアム(Tradition、JPY{満期}{原資産}TOATM=TRDT)から、営業日ベースのノーマルvolを出す。

■ プレミアムの定義(2026-10-25に実機と実DBのカーブで確認。docs/decisions.md)
  - Traditionのプレミアムは ATMストラドル(ペイヤー+レシーバー)のフォワード・プレミアムで、想定元本に対するbp。
    リアルタイムは PRIMACT_1(=CF_LAST)、ヒストリカルは FWPM_OCPHM。フォワードの起点は原資産の起算日。
  - 満期日 = 取引日 + 満期(休日なら翌営業日)、起算日 = 満期日の2営業日後(ql.Japan)。
  - ATMストラドル = 2 × 年金(起算日へのフォワード) × σ√T × φ(0)(バシュリエ)。Tradition自身のvol
    (NRMV_OCPHM)は T = 暦日/365 で、自前のカーブの年金でプレミアムから逆算すると0.00bpで一致した。

■ 営業日ベースのvol(ユーザー指示)
  プレミアムから逆算した満期までの標準偏差 σ√T(bp)を、満期までの営業日数 N で割って1日あたりにし、
  年率は ×√252 で表す(vol = σ√T / √N × √252。bp/日 = vol / √252)。分散が営業日にだけ積み上がる見方で、
  年末年始・GWのように休日をまたぐ満期ほど暦日ベース(Traditionの値)とずれる。
  N は満期までの営業日数(engine/common/option/term.py。BondOptionのTと同じ数え方)。

■ 計算(QuantLib)
  原資産のフォワード・スワップ(起算日〜起算日+原資産、規約は STANDARD)を組み、フォワード・レートと
  年金(固定レッグのBPS / 起算日のDF)を読む。σ√T は ql.bachelierBlackFormulaImpliedVol を
  T=1 で解いた値(片側のオプションの価格 = プレミアム/2 を年金で割ったもの、ストライク=フォワード)。
"""

import math
import re
from datetime import date
from typing import NamedTuple, Optional, Tuple

import QuantLib as ql

from engine.common.conventions import CALENDAR
from engine.common.option import term
from engine.common.option.term import TRADING_DAYS, When, as_date, business_days
from engine.common.swap_pricer import PAY, _build_swap
from engine.common.terms import STANDARD, SwapTerms

START_LAG_DAYS = 2  # 満期日 → 原資産の起算日(営業日)

_LABEL = re.compile(r"^\s*(\d+[MY])\s*X\s*(\d+[MY])\s*$", re.I)


class SwaptionVol(NamedTuple):
    label: str
    expiry: date  # 満期日
    start: date  # 原資産の起算日
    days: float  # 満期までの残りの営業日数(日付なら当日を含む整数、ライブは日中の残りも数える)
    forward: float  # フォワード・スワップレート(%)
    annuity: float  # 起算日へのフォワードの年金(想定元本1あたり、年)
    stdev: float  # 満期までの標準偏差 σ√T(bp)
    vol: float  # 営業日ベースのノーマルvol(bp/年 = bp/日 × √252)


def parse_label(label) -> Tuple[str, str]:
    """「満期x原資産」(例 1Yx10Y) → ("1Y", "10Y")。"""
    m = _LABEL.match(str(label))
    if not m:
        raise ValueError(f"銘柄は「満期x原資産」で指定してください(例 1Yx10Y): {label!r}")
    return m.group(1).upper(), m.group(2).upper()


def expiry_date(trade_date: date, expiry: str) -> date:
    """満期日 = 取引日 + 満期(休日なら翌営業日)。"""
    d = ql.Date.from_date(trade_date) + ql.PeriodParser.parse(expiry.strip().upper())
    return CALENDAR.adjust(d, ql.Following).to_date()


def vol_from_stdev(stdev: float, days: float) -> float:
    """満期までの標準偏差(bp)→ 営業日ベースのノーマルvol(bp/年)。"""
    return stdev / math.sqrt(days) * math.sqrt(TRADING_DAYS)


CALENDAR_DAYS = 365  # 暦日ベースの年率の日数(Traditionの値と同じ T = 暦日/365)


def calendar_factor(when: When, expiry: str) -> float:
    """営業日ベースのvol → 暦日ベースのvol(bp/年)の掛け目 = √(営業日数/252) × √(365/暦日数)。
    満期までの標準偏差 σ√T は同じで、年率に直す日数だけを替える(日付なら暦日数 = 満期日 − 取引日。
    時刻なら満期日の15時までの残り。engine/common/option/term.py)。"""
    trade_date = as_date(when)
    end = expiry_date(trade_date, expiry)
    days = term.remaining_days(when, end)
    calendar_days = term.calendar_days(when, end)
    if days <= 0 or calendar_days <= 0:
        raise ValueError(f"満期が取引日以前です: {trade_date} {expiry}")
    return math.sqrt(days / TRADING_DAYS) * math.sqrt(CALENDAR_DAYS / calendar_days)


class Underlying(NamedTuple):
    """取引日のスワップション(満期x原資産)の契約。volの換算・原資産のFwd(DBのswaption_forwards)・
    ライブのFwdは全てこれを使う(契約の決め方をここ1か所にする)。"""

    label: str  # 「満期x原資産」(例 1Yx10Y)
    expiry: date  # 満期日
    days: int  # 満期までの営業日数(当日を含む。引け時点)
    terms: SwapTerms  # 原資産のフォワード・スワップ(起算日=満期日+START_LAG_DAYS営業日、規約 STANDARD)


def underlying(trade_date: date, label) -> Underlying:
    """取引日 trade_date の契約。満期日 = 取引日+満期(翌営業日)、起算日 = 満期日の2営業日後
    (スワップのスポットと同じT+2。現物の国債のT+1とは別)。"""
    e, tail = parse_label(label)
    expiry = expiry_date(trade_date, e)
    start = CALENDAR.advance(ql.Date.from_date(expiry), START_LAG_DAYS, ql.Days).to_date()
    return Underlying(
        f"{e}x{tail}", expiry, business_days(trade_date, expiry), SwapTerms(start=start, convention=STANDARD, tenor=tail),
    )


def annuity(swap: ql.OvernightIndexedSwap, curve: ql.YieldTermStructureHandle) -> float:
    """起算日へのフォワードの年金(想定元本1あたり、年)= 固定レッグのBPS / 起算日のDF。"""
    return abs(swap.fixedLegBPS()) / 1e-4 / curve.discount(swap.startDate())


class PremiumConverter:
    """1つのカーブ(取引日のもの)で、プレミアムをvolに換える。呼ぶ前後で評価日を取引日にしておく。
    when: 満期までの残りを数える時点。省略すれば取引日の引け(朝バッチ)。ライブは今の時刻(フィード)。"""

    def __init__(self, curve: ql.YieldTermStructureHandle, trade_date: date, when: Optional[When] = None):
        self.curve = curve
        self.trade_date = trade_date
        self.when = trade_date if when is None else when
        self.index = ql.Tonar(curve)
        self.engine = ql.DiscountingSwapEngine(curve)

    def convert(self, label, premium) -> Optional[SwaptionVol]:
        """premium: ATMストラドルのフォワード・プレミアム(bp)。値が無い・0以下なら None。"""
        if premium is None or isinstance(premium, bool):
            return None
        premium = float(premium)
        if not math.isfinite(premium) or premium <= 0:
            return None
        u = underlying(self.trade_date, label)
        days = term.remaining_days(self.when, u.expiry)
        if days <= 0:
            return None
        swap = _build_swap(self.index, u.terms, 1.0, PAY, 0.0)
        swap.setPricingEngine(self.engine)
        forward = swap.fairRate()
        ann = annuity(swap, self.curve)
        price = premium / 1e4 / 2 / ann  # 片側(ペイヤー)の価格、レートの単位
        stdev = ql.bachelierBlackFormulaImpliedVol(ql.Option.Call, forward, forward, 1.0, price) * 1e4
        return SwaptionVol(
            u.label, u.expiry, u.terms.start, days, forward * 100, ann, stdev, vol_from_stdev(stdev, days),
        )
