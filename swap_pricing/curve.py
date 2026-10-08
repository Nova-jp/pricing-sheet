"""
JPY OIS(TONA)ディスカウントカーブのブートストラップモジュール。QuantLibを使用。

■ 使用データ
  RealTimeシートのスポットテナー(O/N〜40Y)のみ。会合スワップ(RD・M1〜M9)は使わない
  (BOJシートの比較用に swap_pricing/meeting_curve.py が別のカーブを引く)。

■ O/Nの扱い
  O/Nのみ settlementDays=0, tenor=1日 のOISヘルパーとして評価日(T)起点で
  構築し、それ以外(1W〜40Y)は settlementDays=2 でスポット日(T+2)起点。
  評価日→スポット日のDFは、QuantLibのブートストラップが全ヘルパーを
  同時に整合させる形で解くため、以前の自前実装にあった「O/Nレートを
  T→スポットの期間にそのまま延長する近似」は不要になった。

■ 補間法
  ql.PiecewiseConvexMonotoneForward (Hagan-West Convex Monotone)。
  QuantLibのブートストラップは全pillar確定後の整合性も含めて解くため、
  以前の自前実装で必要だった精緻化パス(_refine_gap_tenors)は不要。

■ どのテナーを使うか
  入力辞書(spot_rates)に入っているキーがそのまま使われる。「このレートを
  使う/使わない」の取捨選択は呼び出し側(Excelなら RealTime B列のフラグを
  読む swap_pricing.udf._rates_from_range)の責務で、この関数では行わない。
  ここでは O/N・週物・会合スワップ 以外のキーを ql.PeriodParser で期間化して
  OISRateHelper にするだけ。15M/18M/21M も特別扱いせず普通のテナーとして扱う。
  期間として解釈できないキーは skipped_tenors に記録する(無言で捨てない)。

■ 表記ゆれの吸収(TENOR_ALIASES)
  RealTimeシート(Excel)は Reuters フィードの都合で 1Y/2Y/3Y を 12M/24M/36M
  表記にしている(historical_data.xlsxのMidシートも同じ表記)。
  入力境界で正規化し、内部は常に 1Y/2Y/3Y 表記で扱う(対応表は conventions.py)。

■ 日付計算は全てQuantLib純正(自前の日付アルゴリズムを持たない)
  Python date⇄ql.Date変換は ql.Date.from_date()/to_date()、テナー文字列→期間は
  ql.PeriodParser.parse() を直接使う。スポット日(T+2)は自前公式ではなく、
  ダミーの ql.MakeOIS の開始日を読むことで OISRateHelper と同じ設定日ロジック
  を保証する(_standard_spot_date)。OISRateHelper構築時は rule=Forward・
  endOfMonth=False を明示し、pricing側(swap_pricer.py)の標準コンベンション
  "STD"と揃える(QuantLibの既定値のままだとパーレートが0.1bp以上ズレる。
  経緯は docs/decisions.md 参照)。

■ 未対応(スコープ外)
  - RD・M1〜M12: BOJ会合スワップ(meeting_curve.py で扱う)
"""

from datetime import date
from typing import Dict, List, NamedTuple

import QuantLib as ql

from swap_pricing.conventions import CALENDAR, TENOR_ALIASES

WEEK_TENORS = ["1w", "2w", "3w"]
EXCLUDED_MEETING_TENORS = {"rd"} | {f"m{i}" for i in range(1, 13)}
ON_KEYS = ("o/n", "on", "1d")
SPOT_SETTLEMENT_DAYS = 2


class BootstrapResult(NamedTuple):
    curve: ql.YieldTermStructureHandle
    index: "ql.OvernightIndex"
    valuation_date: date
    spot_date: date
    used_tenors: List[str]
    skipped_tenors: List[str]


def _standard_spot_date(index: "ql.OvernightIndex", settlement_days: int = SPOT_SETTLEMENT_DAYS) -> date:
    """
    OISRateHelper/MakeOISが内部で使う設定日ロジック(評価日を営業日に調整して
    からsettlement_days営業日進める)を、そのままQuantLibに問い合わせて求める。

    「evaluationDateを丸めてから advance」を自前で再実装すると、QuantLib内部の
    計算と評価日が非営業日のときに食い違う恐れがある(実際に発生した不整合)。
    ダミーのOISを1本組んでその開始日を読むことで、カーブのpillar(OISRateHelper)
    と完全に同じ計算経路を保証する。
    """
    probe = ql.MakeOIS(ql.Period(1, ql.Weeks), index, 0.0, settlementDays=settlement_days)
    return probe.startDate().to_date()


def normalize_tenor(label: str) -> str:
    """テナーのラベル → bootstrap_curve 内部の表記(小文字、12M/24M/36M → 1Y/2Y/3Y)。
    BootstrapResult.used_tenors と突き合わせるために使う。"""
    key = label.strip().lower()
    return TENOR_ALIASES.get(key, key)


def spot_date_for(valuation_date: date) -> date:
    """カーブを組まずに、任意の評価日のスポット日だけを求める(bootstrap_curveと同じ経路)。

    ql.Settings.evaluationDate を書き換える点に注意(price_swapは入口で毎回設定し直す)。
    """
    ql.Settings.instance().evaluationDate = ql.Date.from_date(valuation_date)
    return _standard_spot_date(ql.Tonar())


def bootstrap_curve(
    valuation_date: date, spot_rates: Dict[str, float]
) -> BootstrapResult:
    """
    spot_rates: {tenor_label: rate(パーセント表記)} 例 {'12M': 1.135, '5Y': 1.62, ...}
    キーの大小文字・空白は問わない。ここに入っているキーがそのまま使われる
    (「使う/使わない」の取捨選択は呼び出し側の責務)。O/N・週物・会合スワップ 以外で
    期間として解釈できないキーは skipped_tenors に記録して無視する。
    """
    rates = {k.strip().lower(): v for k, v in spot_rates.items() if v is not None}
    for alias, canonical in TENOR_ALIASES.items():
        if alias in rates and canonical not in rates:
            rates[canonical] = rates.pop(alias)

    valuation_ql = ql.Date.from_date(valuation_date)
    ql.Settings.instance().evaluationDate = valuation_ql

    bootstrap_index = ql.Tonar()  # ヘルパー構築専用(カーブに未リンク)
    spot_date = _standard_spot_date(bootstrap_index)
    used_tenors: List[str] = []
    skipped_tenors: List[str] = []
    handled = set()  # O/N・週物・会合スワップ として処理済みのキー

    helpers = []

    on_key = next((k for k in ON_KEYS if k in rates), None)
    if on_key is not None:
        # 1日物のOISRateHelperはQuantLib内部のスケジュール生成が特定の評価日で
        # 退化する(degenerate single date)ことがあるため、単純な預金型の
        # DepositRateHelperを使う(O/Nは複利計算不要な1日物のため実質等価)。
        quote = ql.QuoteHandle(ql.SimpleQuote(rates[on_key] / 100.0))
        helpers.append(
            ql.DepositRateHelper(
                quote, ql.Period(1, ql.Days), 0, CALENDAR, ql.ModifiedFollowing, False, ql.Actual365Fixed()
            )
        )
        used_tenors.append(on_key)
        handled.add(on_key)

    for label in WEEK_TENORS:
        if label not in rates:
            continue
        quote = ql.QuoteHandle(ql.SimpleQuote(rates[label] / 100.0))
        helpers.append(
            ql.OISRateHelper(
                SPOT_SETTLEMENT_DAYS, ql.PeriodParser.parse(label.upper()), quote, bootstrap_index,
                rule=ql.DateGeneration.Forward, endOfMonth=False,
            )
        )
        used_tenors.append(label)
        handled.add(label)

    # 会合スワップ(RD・M1〜)はメインのカーブに使わない。「未対応の商品タイプ」として明示的に計上する。
    for label in [t for t in rates if t in EXCLUDED_MEETING_TENORS]:
        skipped_tenors.append(label)
        handled.add(label)

    # 残りのキーは ql.PeriodParser で期間として解釈してOISRateHelper化する
    # (候補テナーの決め打ちはしない)。解釈できないキーは無言で捨てず記録する。
    month_tenors = []
    for label in rates:
        if label in handled:
            continue
        try:
            period = ql.PeriodParser.parse(label.upper())
        except RuntimeError:
            skipped_tenors.append(label)
            continue
        month_tenors.append((period, label))
    for period, label in sorted(month_tenors, key=lambda t: t[0]):
        quote = ql.QuoteHandle(ql.SimpleQuote(rates[label] / 100.0))
        helpers.append(
            ql.OISRateHelper(
                SPOT_SETTLEMENT_DAYS, period, quote, bootstrap_index,
                rule=ql.DateGeneration.Forward, endOfMonth=False,
            )
        )
        used_tenors.append(label)

    if not helpers:
        raise ValueError("ブートストラップに使えるレートがありません")

    term_structure = ql.PiecewiseConvexMonotoneForward(valuation_ql, helpers, ql.Actual365Fixed())
    term_structure.enableExtrapolation()
    curve_handle = ql.YieldTermStructureHandle(term_structure)
    index = ql.Tonar(curve_handle)

    return BootstrapResult(
        curve=curve_handle,
        index=index,
        valuation_date=valuation_date,
        spot_date=spot_date,
        used_tenors=used_tenors,
        skipped_tenors=skipped_tenors,
    )


# カーブの引き方(構築方法)の登録表。historical_curve.toml の builder で名前を選ぶ。
# 引き方を追加するときは「(評価日, {テナー: レート%}) → curve/index/used_tenors/
# skipped_tenors を持つ結果」の関数をここに登録するだけでよい。保存(curve_store.py)と
# 評価(historical_pricer.py)は日次のDFしか扱わないため、引き方に依存しない。
CURVE_BUILDERS = {
    "convex_monotone": bootstrap_curve,
}


def get_curve_builder(name: str):
    try:
        return CURVE_BUILDERS[name]
    except KeyError:
        raise ValueError(
            f"未対応のカーブ構築方法です: {name!r} (対応: {tuple(CURVE_BUILDERS)})"
        ) from None
