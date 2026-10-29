"""
金利スワップのプライシングエンジン。QuantLibの ql.MakeOIS(→ql.OvernightIndexedSwap)を使用。

前提: ディスカウントカーブとフォーキャストカーブが同一(単一カーブ、
TONA OISのみ対応。JSCC・LCHはカーブが違うだけで、ここは同じ)。固定脚・変動脚は独立したスケジュール
(freq/roll conventionが異なってよい)で構築する。

■ スワップ構築はQuantLib純正のビルダー(ql.MakeOIS)に一本化
  スケジュール生成(Forward/Backward、EOM、IMM等)を自前のql.Schedule構築で
  再実装せず、QuantLib標準のOISビルダーに委ねる。カーブのブートストラップ
  (curve.pyのql.OISRateHelper)も同じ仕組みでスケジュールを組むため、両者が
  食い違うリスクが構造的になくなる(経緯はdocs/decisions.md参照)。なお
  overnight(変動)脚の日数計算はQuantLibのOISモデル自体がインデックス固有の
  DayCounterに固定する仕様のため、float dcfは入力として持たない(engine/common/terms.py)。

■ 取引条件は engine/common/terms.py の型(SwapTerms=規約+日付)で受け取る

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
  使われない)。実績は市場データ(market_data.Market.fixings、評価日より前の日だけ。
  元はヒストリカルDB=LSEGのJPONMU=RR)から取り出し、ql.Tonar の履歴に登録する。
  必要な日が欠けていれば、欠損日を示してエラーにする(別物の値での代用はしない)。

■ 計算は取引条件(terms.SwapTerms)と市場データ(market_data.Market)を受け取る

■ 評価日(ql.Settings.evaluationDate)は計算のたびに設定し直す
  evaluationDateはプロセス全体で1つのグローバル値で、構築済みのカーブ
  (OISRateHelper)もこれに連動して再計算される。xlwingsのUDFは同一プロセスで
  動くため、別日付のカーブ構築(ヒストリカル計算等)を挟んだ後にキャッシュ済み
  カーブを使い回すと、評価日がずれたまま計算される(10Yパーで約0.05bpのズレを
  実測)。評価の入口(price_swap・par_rateが共通で通る_prepare_swap)で必ずvaluation_dateに戻す。
"""

from datetime import date
from types import MappingProxyType
from typing import Dict, List, Mapping, NamedTuple, Optional, Tuple

import QuantLib as ql

from engine.common.conventions import FREQ_TO_QL_FREQUENCY, day_counter, rule_and_eom
from engine.common.market_data import Market
from engine.common.terms import PAY, REC, SwapTerms

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
    terms: SwapTerms,
    notional: float,
    pay_rec: str,
    fixed_rate: float,
) -> ql.OvernightIndexedSwap:
    """取引条件どおりのOISを組む。規約の値と tenor/end の指定は SwapTerms が確認済み。"""
    conv = terms.convention
    rule, end_of_month = rule_and_eom(conv.roll_conv)
    # tenor未指定(=end指定)の場合、スワップ全体の期間は withTerminationDate で
    # 上書きするため、最初の位置引数(swapTenor)はダミー値でよい。
    swap_tenor = (
        ql.PeriodParser.parse(terms.tenor.upper()) if terms.tenor is not None else ql.Period(0, ql.Days)
    )

    kwargs = dict(
        swapType=_TYPE_MAP[pay_rec],
        nominal=notional,
        effectiveDate=ql.Date.from_date(terms.start),
        fixedLegPaymentFrequency=FREQ_TO_QL_FREQUENCY[conv.fix_freq],
        overnightLegPaymentFrequency=FREQ_TO_QL_FREQUENCY[conv.float_freq],
        fixedLegDayCount=day_counter(conv.fix_dcf),
        convention=ql.ModifiedFollowing,
        terminationDateConvention=ql.ModifiedFollowing,
        dateGenerationRule=rule,
        endOfMonth=end_of_month,
    )
    if terms.end is not None:
        kwargs["terminationDate"] = ql.Date.from_date(terms.end)

    return ql.MakeOIS(swap_tenor, index, fixed_rate, **kwargs)


def swap_dates(terms: SwapTerms) -> Tuple[date, date]:
    """カーブを使わずに、QuantLibが組むスワップの起算日・満期日(休日調整後)を返す。
    price_swapと同じビルダーでスケジュールを組むので、日付は評価と同じになる。"""
    swap = _build_swap(ql.Tonar(), terms, 1.0, PAY, 0.0)
    dates = swap.fixedSchedule().dates()
    return dates[0].to_date(), dates[-1].to_date()


# QuantLibの履歴(指標名TONARごとにプロセス全体で共有)に登録済みの内容。None=未確認(次は全部入れ直す)。
# この記録が履歴と一致している前提で差分だけを登録するため、TONARの履歴は_set_fixing_history
# 以外から触らない(addFixings/clearFixingsを直接呼ばない)。
_registered_fixings: Optional[Dict[date, float]] = None
# 前回揃えたときに渡された Market.fixings(読み取り専用のMappingProxyType)。同じオブジェクトなら
# 中身も同じなので照合を省く(ヒストリカルは同じ日の Market で複数の脚を続けて評価する)。
# 参照を持ち続けるので、別のオブジェクトが同じidになることはない。
_registered_source: Optional[Mapping[date, float]] = None


def _set_fixing_history(index: "ql.OvernightIndex", fixings: Optional[Mapping[date, float]]) -> None:
    """TONAのフィキシング履歴を、渡されたものと完全に一致させる(Noneなら空にする)。

    履歴は指標名ごとにグローバル共有のため、前回の呼び出しで登録した値が
    残って黙って使われることがないよう、渡された内容に揃える。登録済みの内容に
    日付が増えただけなら増えた分だけを登録し、消す必要がある(日付が減った・値が
    変わった)ときは全部消して登録し直す(QuantLibに1件だけ消す機能はないため)。
    ヒストリカルは日付を進めるごとに前日までの実績を渡すので、毎回1日分の登録で済む。
    """
    global _registered_fixings, _registered_source
    if (
        isinstance(fixings, MappingProxyType) and fixings is _registered_source
        and _registered_fixings is not None
    ):
        return
    _registered_source = None
    target = dict(fixings) if fixings else {}
    prev = _registered_fixings
    # 登録済みの内容が全てそのまま残っていれば(日付が増えただけなら)差分だけでよい
    reset = prev is None or any(target.get(d) != v for d, v in prev.items())
    added = target if reset else {d: v for d, v in target.items() if d not in prev}
    if not reset and not added:
        _registered_source = fixings if isinstance(fixings, MappingProxyType) else None
        return
    # 履歴に手を入れる前に記録を「未確認」にする。登録の途中で例外が起きても、記録が古いまま
    # 残って次の呼び出しが差分だけを足す(消えた分を入れ直さない)ことがないように。
    _registered_fixings = None
    if reset:
        index.clearFixings()
    dates = [ql.Date.from_date(d) for d in added]
    valid = [(d, v / 100.0) for d, v in zip(dates, added.values()) if index.isValidFixingDate(d)]
    if valid:
        index.addFixings([d for d, _ in valid], [v for _, v in valid], True)
    _registered_fixings = target
    _registered_source = fixings if isinstance(fixings, MappingProxyType) else None


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
    fixings: Mapping[date, float],
) -> None:
    """取引条件から必要な実績の日付を判断し、市場データに全てあるか確かめる。"""
    missing = [d for d in _required_fixing_dates(swap, index, valuation_date) if d not in fixings]
    if missing:
        shown = ", ".join(str(d) for d in missing[:3])
        raise ValueError(
            f"過去起算に必要なTONA実績(フィキシング)が{len(missing)}日分不足しています({shown}...)。"
            "historical_data.xlsxを更新・保存してから python -m engine.common.morning_batch を実行し、"
            "DBに取り込んでください(Fixingの取得期間が起算日をカバーしているかも確認)"
        )


class _PreparedSwap(NamedTuple):
    swap: ql.OvernightIndexedSwap  # 固定金利0・評価エンジン設定済み
    index: "ql.OvernightIndex"
    engine: "ql.DiscountingSwapEngine"


def _prepare_swap(market: Market, terms: SwapTerms, *, notional: float, pay_rec: str) -> _PreparedSwap:
    """評価の前提をそろえ、固定金利0のスワップを評価できる状態で返す(price_swap/par_rate共通)。

    評価日の設定・フィキシング履歴の登録・満期済みの判定・必要な実績の確認をここに集める。
    固定金利0なのは、クーポンレートがfairRate/BPSに影響しないため。
    """
    valuation_date = market.valuation_date
    ql.Settings.instance().evaluationDate = ql.Date.from_date(valuation_date)
    engine = ql.DiscountingSwapEngine(market.curve)
    _set_fixing_history(market.index, market.fixings)

    swap = _build_swap(market.index, terms, notional, pay_rec, 0.0)
    # 評価日当日の支払いは発生済み扱い(QuantLibの既定 includeReferenceDateEvents=False と同じ)
    today = ql.Date.from_date(valuation_date)
    if all(cf.date() <= today for cf in (*swap.fixedLeg(), *swap.overnightLeg())):
        raise SwapExpiredError(
            f"評価日{valuation_date}時点で満期済みのスワップです"
            f"(最終支払日 {swap.maturityDate().to_date()})"
        )
    _check_fixings(swap, market.index, valuation_date, market.fixings)
    swap.setPricingEngine(engine)
    return _PreparedSwap(swap, market.index, engine)


class ParRateResult(NamedTuple):
    target_fixrate: float  # % 表記
    maturity_date: date


def par_rate(market: Market, terms: SwapTerms) -> ParRateResult:
    """パーレートと満期日だけを求める(ヒストリカル用)。PV・アニュイティが要らないときに、
    price_swapの2本目のスワップ(PV用)を組まずに済ませる。値は想定元本1・PAYで呼んだ
    price_swapのtarget_fixrate・maturity_dateと完全に同じ(fairRateは想定元本が違うと
    最下位ビットが変わりうるため、条件をそろえている)。"""
    prepared = _prepare_swap(market, terms, notional=1.0, pay_rec=PAY)
    return ParRateResult(
        target_fixrate=prepared.swap.fairRate() * 100.0,
        maturity_date=prepared.swap.fixedSchedule().dates()[-1].to_date(),
    )


def price_swap(
    market: Market,
    terms: SwapTerms,
    *,
    notional: float,
    pay_rec: str,
    fix_rate: Optional[float] = None,
) -> SwapPricingResult:
    """fix_rate: 固定金利(%)。None ならパーレートで組む。過去起算の実績は market.fixings から使う。"""
    if pay_rec not in (PAY, REC):
        raise ValueError(f"pay_recは'{PAY}'または'{REC}'である必要があります: {pay_rec!r}")

    # target fixrate(パーレート)・アニュイティは固定金利0のスワップから取る
    prepared = _prepare_swap(market, terms, notional=notional, pay_rec=pay_rec)
    target_fixrate = prepared.swap.fairRate() * 100.0
    annuity = abs(prepared.swap.fixedLegBPS()) / (notional * 0.0001)

    fix_rate_used = target_fixrate if fix_rate is None else fix_rate

    # PVは入力の固定金利(空欄ならパーレート)で組んだスワップをQuantLibで評価する
    priced_swap = _build_swap(prepared.index, terms, notional, pay_rec, fix_rate_used / 100.0)
    priced_swap.setPricingEngine(prepared.engine)
    pv = priced_swap.NPV()

    maturity_date = priced_swap.fixedSchedule().dates()[-1].to_date()

    return SwapPricingResult(
        annuity=annuity,
        target_fixrate=target_fixrate,
        fix_rate_used=fix_rate_used,
        pv=pv,
        maturity_date=maturity_date,
    )
