"""
金利スワップのプライシングエンジン。QuantLibの ql.MakeOIS(→ql.OvernightIndexedSwap)を使用。

前提: ディスカウントカーブとフォーキャストカーブが同一(単一カーブ、
JSCC TONA OISのみ対応)。固定脚・変動脚は独立したスケジュール
(freq/roll conventionが異なってよい)で構築する。

■ スワップ構築はQuantLib純正のビルダー(ql.MakeOIS)に一本化
  スケジュール生成(Forward/Backward、EOM、IMM等)を自前のql.Schedule構築で
  再実装せず、QuantLib標準のOISビルダーに委ねる。カーブのブートストラップ
  (curve.pyのql.OISRateHelper)も同じ仕組みでスケジュールを組むため、両者が
  食い違うリスクが構造的になくなる(経緯はdocs/decisions.md参照)。なお
  overnight(変動)脚の日数計算はQuantLibのOISモデル自体がインデックス固有の
  DayCounterに固定する仕様のため、float_dcf引数は現状も反映されない。

■ 主要な出力
  - annuity: 固定脚のアニュイティ(Σ DF×dcf)。fixedLegBPS()から逆算する
    (BPSはクーポンレートに依存しないため、レート0でも正しく取れる)
  - target_fixrate: パーレート(%) = fairRate()
  - PV: 入力された固定金利(またはパーレート)でのスワップ時価

■ startが過去日付の場合(既存ポジションの継続評価)
  評価日は作業日(=valuation_date)のままでよい。OvernightIndexedSwapは、
  まだ支払われていないクーポンの期間のうち評価日より前の日について、TONAの
  実績フィキシングを要求する(評価日当日は登録されていれば実績、無ければ
  カーブから予測。評価日より後は常に予測で、履歴に未来日の値があっても
  使われない)。実績はExcel側(historical_data.xlsxのFixingシート=LSEGの
  JPONMU=RR)から fixings 引数で受け取り、ql.Tonar の履歴に登録する。
  必要な日が欠けていれば、欠損日を示してエラーにする(別物の値での代用はしない)。

■ 評価日(ql.Settings.evaluationDate)は計算のたびに設定し直す
  evaluationDateはプロセス全体で1つのグローバル値で、構築済みのカーブ
  (OISRateHelper)もこれに連動して再計算される。xlwingsのUDFは同一プロセスで
  動くため、別日付のカーブ構築(ヒストリカル計算等)を挟んだ後にキャッシュ済み
  カーブを使い回すと、評価日がずれたまま計算される(10Yパーで約0.05bpのズレを
  実測)。price_swapの入口で必ずvaluation_dateに戻す。
"""

from datetime import date
from typing import Dict, List, NamedTuple, Optional, Tuple

import QuantLib as ql

from swap_pricing.conventions import FREQ_TO_QL_FREQUENCY, day_counter, rule_and_eom

PAY = "PAY"
REC = "REC"

_TYPE_MAP = {PAY: ql.OvernightIndexedSwap.Payer, REC: ql.OvernightIndexedSwap.Receiver}

class SwapExpiredError(ValueError):
    """評価日時点で全てのキャッシュフローが支払済み(満期済み)のスワップ。

    パーレートが定義できない(QuantLibは"result not available"になる)。ヒストリカル
    計算では満期後の日付を系列から外すために、これだけを他のエラーと区別して捕まえる。
    """


class SwapPricingResult(NamedTuple):
    annuity: float
    target_fixrate: float  # % 表記
    fix_rate_used: float  # % 表記(入力がNoneならtarget_fixrateと同じ)
    pv: float
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
    if (tenor is None) == (end is None):
        raise ValueError("tenor と end のどちらか一方だけを指定してください")
    if fix_freq not in FREQ_TO_QL_FREQUENCY or float_freq not in FREQ_TO_QL_FREQUENCY:
        raise ValueError(f"未対応のfreqです (対応: {tuple(FREQ_TO_QL_FREQUENCY)})")

    effective_ql = ql.Date.from_date(start)
    rule, end_of_month = rule_and_eom(roll_conv)
    # tenor未指定(=end指定)の場合、スワップ全体の期間は withTerminationDate で
    # 上書きするため、最初の位置引数(swapTenor)はダミー値でよい。
    swap_tenor = ql.PeriodParser.parse(tenor.upper()) if tenor is not None else ql.Period(0, ql.Days)

    kwargs = dict(
        swapType=_TYPE_MAP[pay_rec],
        nominal=notional,
        effectiveDate=effective_ql,
        fixedLegPaymentFrequency=FREQ_TO_QL_FREQUENCY[fix_freq],
        overnightLegPaymentFrequency=FREQ_TO_QL_FREQUENCY[float_freq],
        fixedLegDayCount=day_counter(fix_dcf),
        convention=ql.ModifiedFollowing,
        terminationDateConvention=ql.ModifiedFollowing,
        dateGenerationRule=rule,
        endOfMonth=end_of_month,
    )
    if end is not None:
        kwargs["terminationDate"] = ql.Date.from_date(end)

    return ql.MakeOIS(swap_tenor, index, fixed_rate, **kwargs)


def swap_dates(
    start: date,
    fix_freq: str,
    fix_dcf: str,
    float_freq: str,
    float_dcf: str,
    roll_conv: str,
    tenor: Optional[str] = None,
    end: Optional[date] = None,
) -> Tuple[date, date]:
    """カーブを使わずに、QuantLibが組むスワップの起算日・満期日(休日調整後)を返す。
    price_swapと同じビルダーでスケジュールを組むので、日付は評価と同じになる。"""
    swap = _build_swap(
        ql.Tonar(), start, fix_freq, fix_dcf, float_freq, float_dcf, roll_conv,
        1.0, PAY, 0.0, tenor=tenor, end=end,
    )
    dates = swap.fixedSchedule().dates()
    return dates[0].to_date(), dates[-1].to_date()


_registered_fixings_key: Optional[tuple] = None


def _set_fixing_history(index: "ql.OvernightIndex", fixings: Optional[Dict[date, float]]) -> None:
    """TONAのフィキシング履歴を、渡されたものと完全に一致させる(Noneなら空にする)。

    履歴は指標名ごとにグローバル共有のため、前回の呼び出しで登録した値が
    残って黙って使われることがないよう、毎回入れ替える。同じ内容なら何もしない
    (バケットデルタ等で同じ系列を何十回も渡されるため)。
    """
    global _registered_fixings_key
    items = tuple(sorted(fixings.items())) if fixings else ()
    if items == _registered_fixings_key:
        return
    index.clearFixings()
    dates = [ql.Date.from_date(d) for d, _ in items]
    valid = [(d, v / 100.0) for d, (_, v) in zip(dates, items) if index.isValidFixingDate(d)]
    if valid:
        index.addFixings([d for d, _ in valid], [v for _, v in valid], True)
    _registered_fixings_key = items


def _required_fixing_dates(
    swap: ql.OvernightIndexedSwap, index: "ql.OvernightIndex", valuation_date: date
) -> List[date]:
    """未払いクーポンの期間のうち、評価日より前のフィキシング日(=実績が必須の日)。"""
    today = ql.Date.from_date(valuation_date)
    alive_starts = [
        ql.as_coupon(cf).accrualStartDate() for cf in swap.overnightLeg() if cf.date() > today
    ]
    if not alive_starts:
        return []
    required = []
    d = min(alive_starts)
    while d < today:
        if index.isValidFixingDate(d):
            required.append(d.to_date())
        d += 1
    return required


def _check_fixings(
    swap: ql.OvernightIndexedSwap, index: "ql.OvernightIndex", valuation_date: date,
    fixings: Optional[Dict[date, float]],
) -> None:
    required = _required_fixing_dates(swap, index, valuation_date)
    if not required:
        return
    if fixings is None:
        raise ValueError(
            f"過去起算のためTONA実績フィキシング({required[0]}〜{required[-1]})が必要です。"
            "fixing_range引数にhistorical_data.xlsxのFixingシートを指定してください"
        )
    missing = [d for d in required if d not in fixings]
    if missing:
        shown = ", ".join(str(d) for d in missing[:3])
        raise ValueError(
            f"TONAフィキシングが{len(missing)}日分不足しています({shown}...)。"
            "historical_data.xlsxが開いているか、Fixingの取得期間が起算日を"
            "カバーしているか確認してください"
        )


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
    fixings: Optional[Dict[date, float]] = None,
) -> SwapPricingResult:
    """fixings: {フィキシング対象日: TONA(%)}。過去起算のときのみ必要。"""
    if pay_rec not in (PAY, REC):
        raise ValueError(f"pay_recは'{PAY}'または'{REC}'である必要があります: {pay_rec!r}")

    ql.Settings.instance().evaluationDate = ql.Date.from_date(valuation_date)
    if index is None:
        index = ql.Tonar(curve)
    engine = ql.DiscountingSwapEngine(curve)
    _set_fixing_history(index, fixings)

    # target fixrate(パーレート)取得用(クーポンレートはBPS/fairRateに影響しないため0でよい)
    probe_swap = _build_swap(
        index, start, fix_freq, fix_dcf, float_freq, float_dcf, roll_conv,
        notional, pay_rec, 0.0, tenor=tenor, end=end,
    )
    # 評価日当日の支払いは発生済み扱い(QuantLibの既定 includeReferenceDateEvents=False と同じ)
    today = ql.Date.from_date(valuation_date)
    if all(cf.date() <= today for cf in (*probe_swap.fixedLeg(), *probe_swap.overnightLeg())):
        raise SwapExpiredError(
            f"評価日{valuation_date}時点で満期済みのスワップです"
            f"(最終支払日 {probe_swap.maturityDate().to_date()})"
        )
    _check_fixings(probe_swap, index, valuation_date, fixings)
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

    maturity_date = priced_swap.fixedSchedule().dates()[-1].to_date()

    return SwapPricingResult(
        annuity=annuity,
        target_fixrate=target_fixrate,
        fix_rate_used=fix_rate_used,
        pv=pv,
        maturity_date=maturity_date,
    )
