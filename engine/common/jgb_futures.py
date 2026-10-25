"""
長期国債先物(大阪取引所、標準物 利率6%・10年)の規約と、ベーシス・CTD・レポの階段・つなぎ足の計算。

取得(LSEG)は engine/common/futures_source.py、保存は historical_store.py の fut_settles。
ここはLSEGに依存しない計算だけ。単価・ベーシスは額面100円あたりの円(銭はシートで×100)、利回り・レポは%。

■ 限月(Contract)
  - 限月は3・6・9・12月。受渡決済期日 = 限月の20日(休日なら翌営業日)、取引最終日 = その5営業日前
    (JPXの告示: 2020年12月限 12/21・12/14、2021年9月限 9/21・9/13、2026年9月限 9/24・9/14 と一致)。
  - RIC: 取引中は JGB{月コード}{年の下1桁}(JGBZ6)、満了後は JGB{月コード}{年の下1桁}^{年の10の位}(JGBU6^2)。
    満了した限月はリアルタイムのレコードが無いため、受渡日・取引最終日は上の規則で出す。
■ 受渡適格銘柄・交換係数
  - 受渡適格 = 10年利付国債(JB)で、名目の受渡日(20日)から償還日までが7年以上11年未満(月数84以上132未満)。
  - 交換係数(JPXの交換比率): 月数 m = 名目の受渡日→償還日(1か月未満切捨て)、k, x = divmod(m, 6)、
    h = 標準物の利率/2(長期は3%)、v = (1+h)^−k として
        CF = [c/2 + c/2 × (1 − v)/h + v] / (1+h)^(x/6) − c/2 × (6 − x)/6   (c は小数)
    を小数6桁で切り捨て。JPXの公表値(2020〜2022年の中期・長期・超長期の78銘柄)と全部一致した。
    休日調整後の受渡日(21日等)で月数を数えると1か月ずれるので、名目の20日で数える。
■ ベーシス(1銘柄、評価日 d・先物価格 F・レポ r)
  スポット受渡 s = d の T+1、受渡日 D、単価 P = 引け値の単利から(bond.price_from_simple_yield)。
  - グロスベーシス = P − F × CF
  - ネットベーシス = 先渡単価 − F × CF(先渡単価は bond.forward_price。レポは s→D で一定、期中の利払も同じレポで運用)
  - キャリー = グロス − ネット
  - インプライドレポ(IRR)= 受渡代金と期中の利払で、スポットの利含み単価がちょうど賄えるレポ(同じ単利・暦日/365の慣行)
  - CTD = ネットベーシスが最小の銘柄(ユーザーの定義。先物の売り手が受渡日に渡すのは、レポで持ち続けた損益
    −ネットベーシスが最大の銘柄)。IRR最大とは順位が食い違うことがある(NB = 利含み単価 × t × (r − IRR) で、
    単価の違う銘柄同士では一致しない)ので、IRRは参考値。
■ レポの階段(ヒストリカル)
  TONAは日々ぶれるので、利上げ(水準の変化)のところだけ段が変わる階段にする(ユーザー指示)。repo_steps が
  TONA実績から段を見つけ、各段の値はその段の中のTONAの中央値。シートで段を手で上書きできるようにする。
■ つなぎ足
  取引最終日まではその限月、翌日から次の限月(ユーザー指示)。ロールの段差がヒストリカルの分析を妨げないよう、
  日々の変化は同じ限月の中だけで計算して返す(段差の調整はシートで行い、手で上書きできるようにする)。
"""

import math
import statistics
from datetime import date
from typing import Dict, Iterable, List, NamedTuple, Optional, Sequence

import QuantLib as ql

from engine.common import bond
from engine.common.conventions import CALENDAR

STANDARD_COUPON = 6.0  # 長期国債先物の標準物の利率(%)
DELIVERY_DAY = 20
LAST_TRADE_BUSINESS_DAYS = 5  # 取引最終日 = 受渡決済期日の5営業日前
DELIVERABLE_MONTHS = (84, 132)  # 残存7年以上11年未満(月数、上限は含まない)
DELIVERABLE_PREFIX = "JB"  # 10年利付国債
SPOT_SETTLEMENT_DAYS = 1
MONTH_CODES = {3: "H", 6: "M", 9: "U", 12: "Z"}
_CODE_MONTHS = {v: k for k, v in MONTH_CODES.items()}

# レポの階段: TONAが今の段の水準から閾値以上離れ、それが続いた日数で新しい段とみなす
REPO_STEP_THRESHOLD = 0.05  # %
REPO_STEP_CONFIRM_DAYS = 5


class Contract(NamedTuple):
    year: int
    month: int

    @property
    def label(self) -> str:
        return f"{self.year}-{self.month:02d}"

    @classmethod
    def parse(cls, text) -> "Contract":
        """"2026-12" / "JGBZ6"(2020年代とみなす)/ "JGBU6^2" → Contract。"""
        s = str(text).strip().upper()
        try:
            if s.startswith("JGB"):
                code, year_digit = s[3], int(s[4])
                decade = int(s.split("^")[1]) if "^" in s else 2
                c = cls(2000 + decade * 10 + year_digit, _CODE_MONTHS[code])
            else:
                y, m = s.split("-")
                c = cls(int(y), int(m))
        except (IndexError, KeyError, ValueError):
            raise ValueError(f"限月を読めません: {text!r}(例 2026-12)") from None
        if c.month not in MONTH_CODES:
            raise ValueError(f"限月は3・6・9・12月です: {text!r}")
        return c

    def ric(self, listed: bool) -> str:
        base = f"JGB{MONTH_CODES[self.month]}{self.year % 10}"
        return base if listed else f"{base}^{(self.year // 10) % 10}"

    def next(self) -> "Contract":
        return Contract(self.year + 1, 3) if self.month == 12 else Contract(self.year, self.month + 3)


def delivery_date(contract: Contract) -> date:
    nominal = ql.Date(DELIVERY_DAY, contract.month, contract.year)
    return CALENDAR.adjust(nominal, ql.Following).to_date()


def last_trade_date(contract: Contract) -> date:
    return CALENDAR.advance(
        ql.Date.from_date(delivery_date(contract)), -LAST_TRADE_BUSINESS_DAYS, ql.Days,
    ).to_date()


def front_contract(as_of: date) -> Contract:
    """その日の期近(取引最終日まではその限月、翌日から次の限月)。"""
    month = ((as_of.month - 1) // 3 + 1) * 3
    c = Contract(as_of.year, month)
    while last_trade_date(c) < as_of:
        c = c.next()
    return c


def contracts_between(first: Contract, last: Contract) -> List[Contract]:
    out, c = [], first
    while (c.year, c.month) <= (last.year, last.month):
        out.append(c)
        c = c.next()
    return out


def months_to_maturity(contract: Contract, maturity: date) -> int:
    """名目の受渡日(限月の20日)→償還日の月数(1か月未満切捨て)。"""
    months = (maturity.year - contract.year) * 12 + (maturity.month - contract.month)
    return months - 1 if maturity.day < DELIVERY_DAY else months


def conversion_factor(coupon: float, maturity: date, contract: Contract, standard_coupon: float = STANDARD_COUPON) -> float:
    """交換係数(JPXの交換比率、小数6桁で切り捨て)。coupon・standard_coupon は%。"""
    k, x = divmod(months_to_maturity(contract, maturity), 6)
    c, h = coupon / 100, standard_coupon / 200
    v = (1 + h) ** -k
    value = (c / 2 + c / 2 * (1 - v) / h + v) / (1 + h) ** (x / 6) - c / 2 * (6 - x) / 6
    return math.floor(round(value * 1e6, 6)) / 1e6


def is_deliverable(code: str, maturity: date, contract: Contract) -> bool:
    lo, hi = DELIVERABLE_MONTHS
    return str(code).upper().startswith(DELIVERABLE_PREFIX) and lo <= months_to_maturity(contract, maturity) < hi


# ── ベーシス ────────────────────────────────────────────────────────────────

class BondQuote(NamedTuple):
    code: str
    coupon: float  # %
    maturity: date
    simple_yield: float  # 引け値の単利(%)


class BasisRow(NamedTuple):
    code: str
    coupon: float
    maturity: date
    conversion_factor: float
    simple_yield: float
    price: float  # スポット単価(T+1)
    gross_basis: float
    carry: float
    net_basis: float
    implied_repo: float  # %


def implied_repo(jgb: bond.Jgb, price: float, settle: date, delivery: date, invoice_clean: float) -> float:
    """受渡代金(invoice_clean + 受渡日の経過利子)と期中の利払で、スポットの利含み単価を賄うレポ(%)。
    dirty0 × (1 + R t) = invoice_dirty + Σ c/2 × (1 + R t_c) を R について解く(forward_price と同じ慣行)。"""
    dirty0 = price + bond.accrued_interest(jgb, settle)
    invoice_dirty = invoice_clean + bond.accrued_interest(jgb, delivery)
    coupons = bond.coupon_dates(jgb, settle, delivery)
    half = jgb.coupon / 2
    t = (delivery - settle).days / 365
    denominator = dirty0 * t - sum(half * (delivery - d).days / 365 for d in coupons)
    return (invoice_dirty + half * len(coupons) - dirty0) / denominator * 100


def basis_row(contract: Contract, futures_price: float, as_of: date, quote: BondQuote, repo: float) -> BasisRow:
    settle = bond.settlement_date(as_of, SPOT_SETTLEMENT_DAYS)
    delivery = delivery_date(contract)
    if settle >= delivery:
        raise ValueError(f"{contract.label}限: スポット受渡日 {settle} が受渡日 {delivery} 以降です")
    jgb = bond.Jgb(quote.coupon, quote.maturity)
    cf = conversion_factor(quote.coupon, quote.maturity, contract)
    price = bond.price_from_simple_yield(jgb, quote.simple_yield, settle)
    invoice = futures_price * cf
    forward = bond.forward_price(jgb, price, settle, delivery, repo)
    gross = price - invoice
    net = forward.clean - invoice
    return BasisRow(
        quote.code, quote.coupon, quote.maturity, cf, quote.simple_yield, price, gross, gross - net, net,
        implied_repo(jgb, price, settle, delivery, invoice),
    )


def basis_table(
    contract: Contract, futures_price: float, as_of: date, quotes: Iterable[BondQuote], repo: float,
) -> List[BasisRow]:
    """その日の受渡適格銘柄(quotes のうち適格なもの)のベーシス。償還日の順。"""
    rows = [
        basis_row(contract, futures_price, as_of, q, repo)
        for q in quotes if is_deliverable(q.code, q.maturity, contract)
    ]
    return sorted(rows, key=lambda r: (r.maturity, r.code))


def cheapest(rows: Sequence[BasisRow]) -> Optional[BasisRow]:
    """CTD = ネットベーシスが最小の銘柄。"""
    return min(rows, key=lambda r: r.net_basis) if rows else None


# ── レポの階段 ──────────────────────────────────────────────────────────────

class RepoStep(NamedTuple):
    start: date
    rate: float  # %


def repo_steps(
    fixings: Dict[date, float], threshold: float = REPO_STEP_THRESHOLD, confirm_days: int = REPO_STEP_CONFIRM_DAYS,
) -> List[RepoStep]:
    """TONA実績 → 水準の変わり目(利上げ・利下げ)だけで段が変わる階段。
    今の段のTONAの中央値から threshold 以上、同じ側に confirm_days 日続けて離れたら、その最初の日から新しい段。
    段の値はその段の中のTONAの中央値(小数3桁)。月末・期末の数日の跳ねでは段を変えない。"""
    dates = sorted(fixings)
    if not dates:
        return []
    starts = [0]
    i = 1
    while i < len(dates):
        level = statistics.median(fixings[d] for d in dates[starts[-1]:i])
        window = [fixings[d] - level for d in dates[i:i + confirm_days]]
        if len(window) == confirm_days and (
            all(x >= threshold for x in window) or all(x <= -threshold for x in window)
        ):
            starts.append(i)
            i += confirm_days
            continue
        i += 1
    bounds = starts + [len(dates)]
    return [
        RepoStep(dates[a], round(statistics.median(fixings[d] for d in dates[a:b]), 3))
        for a, b in zip(bounds, bounds[1:])
    ]


def repo_on(steps: Sequence[RepoStep], as_of: date) -> Optional[float]:
    """その日に効いている段の値(最初の段より前は None)。"""
    rate = None
    for step in sorted(steps):
        if step.start > as_of:
            break
        rate = step.rate
    return rate


# ── つなぎ足 ────────────────────────────────────────────────────────────────

class FrontQuote(NamedTuple):
    as_of: date
    contract: Contract
    settle: float
    change: Optional[float]  # 同じ限月の前営業日(値のある日)からの変化。限月の初日は None
    rolled: bool  # 前の行と限月が変わった日


def front_series(settles: Dict[Contract, Dict[date, float]]) -> List[FrontQuote]:
    """限月ごとの清算値 → 期近のつなぎ足(取引最終日の翌日に乗り換え)。
    change はロールの日も新しい限月どうしの変化なので、change を積み上げればロールの段差の無い系列になる。"""
    all_dates = sorted({d for prices in settles.values() for d in prices})
    out: List[FrontQuote] = []
    for d in all_dates:
        c = front_contract(d)
        prices = settles.get(c, {})
        if d not in prices:
            continue
        earlier = [x for x in prices if x < d]
        change = prices[d] - prices[max(earlier)] if earlier else None
        rolled = bool(out) and out[-1].contract != c
        out.append(FrontQuote(d, c, prices[d], change, rolled))
    return out
