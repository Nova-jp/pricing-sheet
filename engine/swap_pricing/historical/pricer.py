"""
保存済みの日次カーブから、任意のコンベンションのヒストリカル・パーレート推移を
計算するモジュール。

各過去日付Dについて、カーブの供給元(通常は historical_store.HistoricalStore=朝バッチが
作った日次カーブDB)からDのカーブを受け取り、par_rate()でパーレートを求める。
ここではブートストラップをしない(カーブの引き方を知らない)ため、引き方を
変えてもこのモジュールは影響を受けない。

■ 入力は起算日・満期日の2つの日付として読む
  start/end の入力の種類(日付・IMMコード・空欄・tenor)に関係なく、まず基準日B(通常は
  Mainシートの評価日、空欄=今日)の取引の起算日・満期日に直す。
    ・空欄のstart → Bのスポット日
    ・startのテナー(10y等) → Bのスポット日+tenor(フォワードスタート)
    ・startの営業日数(2bd等) → BからN営業日後(2bd=Bのスポット日)
    ・IMMコード → そのIMM日(第3水曜)
    ・tenor → 起算日+tenor(QuantLibのスケジュールが組む満期日)
  FIXED/ROLLINGのどちらにするかは mode 引数だけで決まり、入力の種類には依存しない。

■ 2つの計算モード(mode引数、pricingシートの1行分のコンベンションに対して選ぶ)
  - FIXED(同一取引のヒストリカル):
      Bの取引の日付をそのまま固定し、「その取引が各過去日Dにいくらだったか」を計算する。
      D<startならフォワード起算、D>startなら過去起算(Dより前のTONA実績フィキシングが
      必要)。満期済みの日付は系列に含めない。
  - ROLLING(一定期間のローリング):
      Bの取引の「Bのスポット日→起算日」と「起算日→満期日」の営業日数(ql.Japan)を保った
      まま、各過去日Dのスポット日に平行移動したスワップを計算する(いわゆる「5Y」「1y5y」の
      推移)。暦日数ではなく営業日数を固定するのは、土日祝の並びで期間の長さ(day count)が
      変わり、その差がヒストリカルの変化に混ざらないようにするため(ユーザー指示)。
      その代わり満期日は「スポット日+tenor」から数日ずれることがあり、各日の市場の
      気配(カーブのピラー)とは厳密には一致しない(了承済み)。また固定されるのは営業日数で、
      暦日数は祝日の入り方で変わる(1年物で最大10日程度)。fix dcf(act/365fixed等)で測る
      期間の長さは日ごとに変わり、その差はパーレートに残る(docs/decisions.md)。
      roll conv が IMM の脚は、移動後の日付がIMM日でなくなるためエラーにする。

■ 先読みの排除
  過去日Dの計算には、D当日以降のTONA実績を渡さない(D時点ではまだ公表されていない。
  日中のリアルタイム計算で当日の実績が無いのと同じ扱いにする)。D当日の分はカーブから
  予測されるが、カーブ側のO/NピラーにD当日の実績を使っている場合(historical_curve.toml)
  は結果的に同じ値になる。

カーブ(スプレッド)・フライはExcel側でこの系列を引き算する(HistChartシート参照)。
"""

from datetime import date, timedelta
from typing import Dict, List, NamedTuple, Optional, Protocol, Sequence, Tuple, Union

import QuantLib as ql

from engine.common.conventions import (
    CALENDAR, ROLL_IMM, business_days_start, forward_start_date, imm_code_to_date, is_business_days,
    is_imm_code, is_tenor,
)
from engine.common.curve import spot_date_for
from engine.common.market_data import Market
from engine.common.swap_pricer import SwapExpiredError, par_rate, swap_dates
from engine.common.terms import SwapConvention, SwapTerms

FIXED = "FIXED"
ROLLING = "ROLLING"
SUPPORTED_MODES = (FIXED, ROLLING)

# start/end の入力: 日付、IMMコード(例 "M27")、None(空欄)。startはテナー(例 "10Y")も可
DateInput = Union[date, str, None]

# カーブを読み出す範囲の余裕(満期日の休日調整・EOM等で数日後ろにずれる分)
_HORIZON_MARGIN = timedelta(days=40)


class CurveSource(Protocol):
    """日次カーブの供給元(historical_store.HistoricalStore 等)。"""

    def dates(self) -> List[date]:
        """カーブのある日付(昇順)。"""

    def load(self, as_of_date: date, until: Optional[date] = None):
        """as_of_dateのカーブ(.curve と .index を持つ)。until までの日付を評価できればよい。"""


class HistoricalSwapPoint(NamedTuple):
    as_of_date: date
    par_rate: float  # %
    start_date: date  # QuantLibに渡した起算日(FIXEDは入力どおり=休日調整前、ROLLINGは営業日)
    maturity_date: date  # QuantLibが生成した満期日


def _input_date(value: DateInput) -> Optional[date]:
    """start/end の入力 → 日付(IMMコードはそのIMM日)。空欄は None。"""
    if value is None:
        return None
    return imm_code_to_date(value) if is_imm_code(value) else value


def _input_start(value: DateInput, base_date: date, base_spot: date) -> date:
    """start の入力 → 基準日の取引の起算日。空欄は基準日のスポット日、テナー(10y等)は
    基準日のスポット日+tenor(フォワードスタート)、営業日数(2bd等)は基準日からN営業日後。"""
    if value is None:
        return base_spot
    if is_tenor(value):
        return forward_start_date(base_spot, value)
    if is_business_days(value):
        return business_days_start(base_date, value)
    return _input_date(value)


class SwapSpec(NamedTuple):
    """ヒストリカルを計算する1本のスワップの雛形(pricingシートの1行分)。

    規約は確定済み(SwapConvention)、start/end は日付に決まる前の入力のまま持つ
    (空欄・テナー・営業日数・IMMコードを含む)。基準日の取引に直すのは _leg_dates、各過去日の
    具体的なスワップ(SwapTerms)にするのは _LegDates.on。
    """

    convention: SwapConvention
    start: DateInput = None
    tenor: Optional[str] = None
    end: DateInput = None


class _LegDates(NamedTuple):
    """1本の脚の、過去日ごとのスワップの決め方(基準日の取引から1回だけ作る)。"""

    base: SwapTerms  # 基準日の取引(FIXEDは全ての過去日でこのまま使う)
    offset_days: int  # ROLLING: スポット日→起算日の営業日数
    length_days: int  # ROLLING: 起算日→満期日の営業日数

    def on(self, mode: str, as_of_spot: date) -> SwapTerms:
        """過去日D(スポット日 as_of_spot)で組むスワップ。"""
        if mode == FIXED:
            return self.base
        start = CALENDAR.advance(ql.Date.from_date(as_of_spot), self.offset_days, ql.Days)
        end = CALENDAR.advance(start, self.length_days, ql.Days)
        return SwapTerms(start.to_date(), self.base.convention, end=end.to_date())


def _leg_dates(spec: SwapSpec, mode: str, base_date: date, base_spot: date) -> _LegDates:
    base = SwapTerms(
        _input_start(spec.start, base_date, base_spot), spec.convention, tenor=spec.tenor, end=_input_date(spec.end),
    )
    if mode == FIXED:
        return _LegDates(base, 0, 0)
    if spec.convention.roll_conv == ROLL_IMM:
        raise ValueError("roll convがIMMの脚はROLLINGで計算できません(移動後の日付がIMM日でなくなるため)")
    # 基準日の取引の起算日・満期日(休日調整後)を QuantLib のスケジュールから取り、営業日数を数える
    swap_start, swap_end = swap_dates(base)
    ql_start = ql.Date.from_date(swap_start)
    return _LegDates(
        base,
        CALENDAR.businessDaysBetween(ql.Date.from_date(base_spot), ql_start),
        CALENDAR.businessDaysBetween(ql_start, ql.Date.from_date(swap_end)),
    )


class LegResult(NamedTuple):
    par_rate: float  # %
    start_date: date  # QuantLibに渡した起算日(FIXEDは入力どおり=休日調整前、ROLLINGは営業日)
    maturity_date: date  # QuantLibが生成した満期日


def _normalize_mode(mode: str) -> str:
    mode = str(mode).strip().upper()
    if mode not in SUPPORTED_MODES:
        raise ValueError(f"未対応のmodeです: {mode!r} (対応: {SUPPORTED_MODES})")
    return mode


def _last_date(terms: SwapTerms) -> date:
    """カーブを読み出す範囲の目安(満期日。tenor指定なら起算日+tenor)。"""
    if terms.end is not None:
        return terms.end
    return (ql.Date.from_date(terms.start) + ql.PeriodParser.parse(terms.tenor.upper())).to_date()


def historical_multi_rate_series(
    specs: Sequence[SwapSpec],
    mode: str,
    base_date: Optional[date] = None,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    fixings: Optional[Dict[date, float]] = None,
    *,
    curves: CurveSource,
) -> List[Tuple[date, List[Optional[LegResult]]]]:
    """
    複数のスワップ(カーブ・フライの各脚)のヒストリカル・パーレートを、同じ日付の並びで返す。

    戻り値: [(as_of_date, [脚ごとの結果]), ...](as_of_dateの昇順)。期間内でカーブのある
    日付は全て返し、計算できない日(FIXEDで満期後)の脚は None にする(日付の並びを脚の間・
    系列の間で揃えるため)。specs が空なら日付だけを返す(脚のリストは空)。
    日付ごとにカーブは1回だけ読み出し(全脚の満期をカバーする範囲)、全脚を評価する。

    mode: FIXED / ROLLING(全脚に共通。モジュールのdocstring参照)
    base_date: 入力の取引を組む基準日(空欄のstartはこの日のスポット日。ROLLINGの営業日数も
        この日の取引で数える)。省略時は今日
    from_date/to_date: 計算する過去日の範囲(省略時はカーブのある全期間)
    fixings: {フィキシング対象日: TONA(%)}。過去起算になる日があるときに必要
    curves: 日次カーブの供給元
    """
    mode = _normalize_mode(mode)
    legs_dates: List[_LegDates] = []
    if specs:
        base_date = base_date or date.today()
        base_spot = spot_date_for(base_date)
        for n, spec in enumerate(specs, start=1):
            try:
                legs_dates.append(_leg_dates(spec, mode, base_date, base_spot))
            except Exception as e:
                raise ValueError(f"{n}本目: {e}") from e

    results: List[Tuple[date, List[Optional[LegResult]]]] = []
    for as_of_date in curves.dates():
        if (from_date is not None and as_of_date < from_date) or                 (to_date is not None and as_of_date > to_date):
            continue
        if not specs:
            results.append((as_of_date, []))
            continue

        as_of_spot = spot_date_for(as_of_date)
        terms_on = [d.on(mode, as_of_spot) for d in legs_dates]
        until = max(_last_date(t) for t in terms_on)
        boot = curves.load(as_of_date, until=until + _HORIZON_MARGIN)
        # 日付Dの市場データ。実績はDより前の日だけになる(先読みの排除はMarketが行う)
        market = Market(as_of_date, boot.curve, boot.index, fixings or {})

        legs: List[Optional[LegResult]] = []
        for terms in terms_on:
            try:
                result = par_rate(market, terms)
            except SwapExpiredError:
                legs.append(None)  # FIXEDで満期後の日付
                continue
            except Exception as e:
                raise ValueError(f"{as_of_date}時点の計算に失敗しました: {e}") from e
            legs.append(LegResult(result.target_fixrate, terms.start, result.maturity_date))
        results.append((as_of_date, legs))

    return results


def historical_swap_rate_series(
    spec: SwapSpec,
    mode: str,
    base_date: Optional[date] = None,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    fixings: Optional[Dict[date, float]] = None,
    *,
    curves: CurveSource,
) -> List[HistoricalSwapPoint]:
    """
    1本のスワップのヒストリカル・パーレート系列(as_of_dateの昇順)。
    historical_multi_rate_series の1本版で、計算できない日(FIXEDで満期後)は系列に含めない。
    """
    rows = historical_multi_rate_series(
        [spec], mode, base_date, from_date, to_date, fixings, curves=curves,
    )
    return [
        HistoricalSwapPoint(d, leg.par_rate, leg.start_date, leg.maturity_date)
        for d, (leg,) in rows if leg is not None
    ]
