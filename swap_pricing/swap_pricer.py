"""
金利スワップのプライシングエンジン。QuantLibの ql.OvernightIndexedSwap を使用。

前提: ディスカウントカーブとフォーキャストカーブが同一(単一カーブ、
JSCC TONA OISのみ対応)。固定脚・変動脚は独立したスケジュール
(freq/dcfが異なってよい)で構築する。

■ 主要な出力
  - annuity: 固定脚のアニュイティ(Σ DF×dcf)。fixedLegBPS()から逆算する
    (BPSはクーポンレートに依存しないため、レート0でも正しく取れる)
  - target_fixrate: パーレート(%) = fairRate()
  - PV: 入力された固定金利(またはパーレート)でのスワップ時価

■ startが過去日付の場合(既存ポジションの継続評価)
  OvernightIndexedSwapは、既に始まっている期間についてTONAの実績
  フィキシングを要求する。実績フィキシングの代用として、ローカル
  キャッシュ(local_cache、朝バッチがNeonから取得したO/N=1Dレート)を
  使う(日中にNeonへは接続しない)。start〜評価日前営業日までの各営業日の
  O/Nレートを ql.Tonar のフィキシング履歴として登録してから評価する。
"""

from datetime import date, timedelta
from typing import NamedTuple, Optional

import QuantLib as ql

from swap_pricing.calendars import CALENDAR
from swap_pricing.daycount import day_counter, to_ql_date
from swap_pricing.local_cache import load_rates
from swap_pricing.schedule import generate_ql_schedule

PAY = "PAY"
REC = "REC"

_TYPE_MAP = {PAY: ql.OvernightIndexedSwap.Payer, REC: ql.OvernightIndexedSwap.Receiver}


class SwapPricingResult(NamedTuple):
    annuity: float
    float_leg_pv: float
    target_fixrate: float  # % 表記
    fix_rate_used: float  # % 表記(入力がNoneならtarget_fixrateと同じ)
    pv: float
    telescoping_check_diff: float  # QuantLib移行後は常に0.0(検算不要になったため)
    maturity_date: date  # tenorから計算した場合の満期日(endを直接指定した場合はend)


def _build_swap(
    index: "ql.OvernightIndex",
    start: date,
    fix_freq: str,
    fix_dcf: str,
    float_freq: str,
    float_dcf: str,
    roll_conv: str,
    notional: float,
    pay_rec: str,
    fixed_rate: float,
    tenor: Optional[str] = None,
    end: Optional[date] = None,
) -> ql.OvernightIndexedSwap:
    fixed_schedule = generate_ql_schedule(start, fix_freq, roll_conv, tenor=tenor, end=end)
    float_schedule = generate_ql_schedule(start, float_freq, roll_conv, tenor=tenor, end=end)
    fixed_dc = day_counter(fix_dcf)

    swap = ql.OvernightIndexedSwap(
        _TYPE_MAP[pay_rec],
        [notional] * (len(fixed_schedule) - 1),
        fixed_schedule,
        fixed_rate,
        fixed_dc,
        [notional] * (len(float_schedule) - 1),
        float_schedule,
        index,
    )
    return swap


def _register_historical_fixings(index: "ql.OvernightIndex", start: date, valuation_date: date) -> None:
    """start(過去日付)〜評価日前営業日までの各営業日のO/Nレートを実績フィキシングとして登録する。"""
    if start >= valuation_date:
        return
    d = start
    while d < valuation_date:
        if CALENDAR.isBusinessDay(to_ql_date(d)):
            rates = load_rates(d)
            if rates is None:
                raise ValueError(
                    f"{d}のTONA実績フィキシング代用レートがローカルキャッシュにありません"
                    "(朝バッチのルックバック期間を確認してください)"
                )
            on_rate = rates.get("1d", rates.get("o/n", rates.get("on")))
            if on_rate is None:
                raise ValueError(f"{d}のO/Nレートがローカルキャッシュ内に見つかりません")
            index.addFixing(to_ql_date(d), on_rate / 100.0, True)
        d += timedelta(days=1)


def price_swap(
    curve: "ql.YieldTermStructureHandle",
    valuation_date: date,
    start: date,
    fix_freq: str,
    fix_dcf: str,
    float_freq: str,
    float_dcf: str,
    roll_conv: str,
    notional: float,
    pay_rec: str,
    tenor: Optional[str] = None,
    end: Optional[date] = None,
    fix_rate: Optional[float] = None,
    index: Optional["ql.OvernightIndex"] = None,
) -> SwapPricingResult:
    if pay_rec not in (PAY, REC):
        raise ValueError(f"pay_recは'{PAY}'または'{REC}'である必要があります: {pay_rec!r}")

    if index is None:
        index = ql.Tonar(curve)
    engine = ql.DiscountingSwapEngine(curve)

    _register_historical_fixings(index, start, valuation_date)

    # target fixrate(パーレート)取得用(クーポンレートはBPS/fairRateに影響しないため0でよい)
    probe_swap = _build_swap(
        index, start, fix_freq, fix_dcf, float_freq, float_dcf, roll_conv,
        notional, pay_rec, 0.0, tenor=tenor, end=end,
    )
    probe_swap.setPricingEngine(engine)
    target_fixrate = probe_swap.fairRate() * 100.0
    annuity = abs(probe_swap.fixedLegBPS()) / (notional * 0.0001)

    fix_rate_used = target_fixrate if fix_rate is None else fix_rate

    priced_swap = _build_swap(
        index, start, fix_freq, fix_dcf, float_freq, float_dcf, roll_conv,
        notional, pay_rec, fix_rate_used / 100.0, tenor=tenor, end=end,
    )
    priced_swap.setPricingEngine(engine)
    pv = priced_swap.NPV()

    fixed_schedule = generate_ql_schedule(start, fix_freq, roll_conv, tenor=tenor, end=end)
    maturity_date = date(
        fixed_schedule.dates()[-1].year(),
        fixed_schedule.dates()[-1].month(),
        fixed_schedule.dates()[-1].dayOfMonth(),
    )

    return SwapPricingResult(
        annuity=annuity,
        float_leg_pv=target_fixrate / 100.0 * annuity,
        target_fixrate=target_fixrate,
        fix_rate_used=fix_rate_used,
        pv=pv,
        telescoping_check_diff=0.0,
        maturity_date=maturity_date,
    )
