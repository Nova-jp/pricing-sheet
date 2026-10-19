"""
Main/pricingシート用のUDF(パーレート・PV・デルタ・notional逆算・リスクの合算)。

Excel境界の型変換と評価日時点のカーブ解決(`_market`等)は`engine.common.udf_common`に
共有されている。このモジュールからは`udf_common.xxx(...)`の形で呼ぶ(名前のコピーはしない。
理由は`udf_common.py`のdocstring)。
"""

from typing import List

import xlwings as xw

from engine.common import udf_common
from engine.common.risk import bucketed_deltas, price, resolve_params
from engine.common.terms import SwapParams
from engine.swap_pricing.pricing import measures, table
from engine.swap_pricing.pricing.row import swap_params, swap_terms
from engine.swap_pricing.pricing.table import is_flagged as _is_flagged


def _params(
    start, tenor, end_date, fix_freq, fix_dcf, float_freq, roll_conv,
    notional, pay_rec, fix_rate, curves,
) -> SwapParams:
    """pricingシートの1行分の入力 → ポジションの型(RiskGrid用。型への変換はrow.py)。"""
    terms = swap_terms(start, tenor, end_date, fix_freq, fix_dcf, float_freq, roll_conv, curves)
    return swap_params(terms, notional, pay_rec, fix_rate)


@xw.func
@xw.arg("pricing_table", ndim=2)
@xw.arg("real_time_range", ndim=2)
def PricingTable(pricing_table, real_time_range, valuation_date=None) -> List[list]:
    """
    pricing表の全行のtarget fixrate・PV・delta(bump)・delta(annuity)・adj_notional・messageを1回で返す
    (1行目は見出し、以降は入力の行と同じ並び。計算はflag=1の行だけ。詳細はpricing/table.py)。

    pricing_table: 見出し行から最終行まで。出力の列(target fixrate・adj_notional等)を含めると循環参照に
        なるので、入力の列だけをHSTACKで渡す(例: HSTACK($A$3:$E$103,$G$3:$O$103,$S$3:$S$103))。
        列は見出し名で探す(flag, notional, delta, start, tenor, end, fix rate, fix freq, fix dcf,
        float freq, roll conv, pay/rec)。
    """
    curves = udf_common._market(real_time_range, valuation_date)
    return table.price_table(pricing_table, curves)


@xw.func
@xw.arg("real_time_range", ndim=2)
def FairRate(
    start, tenor, end_date, fix_freq, fix_dcf, float_freq, roll_conv,
    real_time_range, valuation_date=None,
) -> float:
    """指定コンベンションのパーレート(%)を返す。tenorかend_dateのどちらかは空欄にする。

    real_time_range は フラグ/Ticker/Value の範囲(各シートの TAKE(カーブ用の範囲#,,3)。1列目=採否フラグ[1/空欄=使う, 0=使わない],
    C列=Ticker, D列=Value)。valuation_date は Main!評価日(空欄=今日)。

    引数名は ``end`` だとVBAの予約語(Endステートメント)と衝突し、xlwingsが
    生成する ``xlwings_udfs`` モジュールがコンパイルできなくなるため ``end_date``。
    """
    curves = udf_common._market(real_time_range, valuation_date)
    terms = swap_terms(start, tenor, end_date, fix_freq, fix_dcf, float_freq, roll_conv, curves)
    return measures.fair_rate(curves, terms)


@xw.func
@xw.arg("real_time_range", ndim=2)
def SwapPV(
    start, tenor, end_date, fix_freq, fix_dcf, float_freq, roll_conv,
    notional, pay_rec, fix_rate, real_time_range, valuation_date=None,
) -> float:
    """PV(入力fix_rateが空欄ならパーレートで計算、常に0近辺)。"""
    curves = udf_common._market(real_time_range, valuation_date)
    params = _params(
        start, tenor, end_date, fix_freq, fix_dcf, float_freq, roll_conv, notional,
        pay_rec, fix_rate, curves,
    )
    return measures.pv(curves, params)


@xw.func
@xw.arg("real_time_range", ndim=2)
def SwapDelta(
    start, tenor, end_date, fix_freq, fix_dcf, float_freq, roll_conv,
    notional, pay_rec, fix_rate, real_time_range, valuation_date=None,
) -> float:
    """カーブに使っている全テナーを+1bpしたときのデルタ(百万円単位)。
    fix_rateが空欄ならパーレートに固定してから計算する。"""
    curves = udf_common._market(real_time_range, valuation_date)
    params = _params(
        start, tenor, end_date, fix_freq, fix_dcf, float_freq, roll_conv, notional,
        pay_rec, fix_rate, curves,
    )
    return measures.parallel_delta_mm(curves, params)


@xw.func
@xw.arg("real_time_range", ndim=2)
def SwapAnnuityDelta(
    start, tenor, end_date, fix_freq, fix_dcf, float_freq, roll_conv,
    notional, pay_rec, real_time_range, valuation_date=None,
) -> float:
    """アニュイティデルタ(解析的近似、参考値、百万円単位)。"""
    curves = udf_common._market(real_time_range, valuation_date)
    params = _params(
        start, tenor, end_date, fix_freq, fix_dcf, float_freq, roll_conv, notional,
        pay_rec, None, curves,
    )
    return measures.annuity_delta_mm(curves, params)


@xw.func
@xw.arg("real_time_range", ndim=2)
def NotionalForDelta(
    target_delta_mm, start, tenor, end_date, fix_freq, fix_dcf, float_freq, roll_conv,
    pay_rec, real_time_range, valuation_date=None,
) -> float:
    """目標delta(百万円単位)から必要なnotionalを逆算する(アニュイティデルタ基準)。"""
    curves = udf_common._market(real_time_range, valuation_date)
    terms = swap_terms(start, tenor, end_date, fix_freq, fix_dcf, float_freq, roll_conv, curves)
    return measures.notional_for_delta(curves, terms, target_delta_mm, pay_rec)


# RiskGrid が pricing表から読む列(見出し名)
_RISK_COLUMNS = (
    "risk_flag", "notional", "start", "tenor", "end", "fix rate", "fix freq", "fix dcf",
    "float freq", "roll conv", "pay/rec",
)


@xw.func
@xw.arg("pricing_table", ndim=2)
@xw.arg("real_time_range", ndim=2)
def RiskGrid(pricing_table, real_time_range, valuation_date=None) -> List[list]:
    """
    risk_flag=1 の全行について、テナーごとに+1bpしたときのデルタ(百万円単位)の合計を、
    RealTimeシートのテナー行の並びで返す(Ticker/Deltaの2列、1行目は見出し)。

    pricing_table: pricingシートの見出し行(1行目)から最終行まで(例: pricing!$A$1:$T$201)。
        列は見出し名で探す(risk_flag, notional, start, tenor, end, fix rate, fix freq,
        fix dcf, float freq, roll conv, pay/rec)。
    バンプするのはカーブに使っているテナーだけで、それ以外の行(フラグ0、会合スワップ等)は空欄。
    合計値(全テナーの和)はExcelのSUMで出す。
    """
    rows = [list(r) if r is not None else [] for r in (pricing_table or [])]
    if not rows:
        raise ValueError("pricing表が空です")
    header = [str(h).strip().lower() if h is not None else "" for h in rows[0]]
    missing = [c for c in _RISK_COLUMNS if c not in header]
    if missing:
        raise ValueError(f"pricing表に見出しがありません: {', '.join(missing)}")
    col = {c: header.index(c) for c in _RISK_COLUMNS}

    curves = udf_common._market(real_time_range, valuation_date)
    positions = []
    for n, row in enumerate(rows[1:], start=2):
        row = row + [None] * (len(header) - len(row))
        if not _is_flagged(row[col["risk_flag"]]):
            continue
        try:
            params = resolve_params(curves, _params(
                *(row[col[c]] for c in ("start", "tenor", "end", "fix freq", "fix dcf",
                                        "float freq", "roll conv", "notional",
                                        "pay/rec", "fix rate")),
                curves,
            ))
            price(curves.base, params)  # 計算できない行は、ここで行番号つきのエラーにする
        except Exception as e:
            raise ValueError(f"pricing表の{n}行目(risk_flag=1)を計算できません: {e}") from e
        positions.append(params)

    # テナーごとにクォートを1回だけ動かし、その間に全行を評価する(risk.bucketed_deltas)
    totals = {label: 0.0 for label in curves.bump_labels}
    for deltas in bucketed_deltas(curves, positions):
        for label, value in deltas.items():
            totals[label] += value

    result = [["Ticker", "Delta"]]
    for ticker, _, _ in udf_common._realtime_rows(real_time_range):
        value = totals.get(ticker.lower())
        result.append([ticker, value / 1_000_000.0 if value is not None else ""])
    return result


@xw.func
@xw.arg("real_time_range", ndim=2)
def MarketInfo(real_time_range, valuation_date=None) -> str:
    """評価日のカーブの元データの説明(例: 「RealTime 2026-09-28 / 34テナー」)。"""
    curves = udf_common._market(real_time_range, valuation_date)
    info = f"{curves.snapshot.source} {curves.valuation_date} / カーブに使用 {len(curves.bump_labels)}テナー"
    selected = {t.lower() for t, _, use in udf_common._realtime_rows(real_time_range) if use}
    unused = sorted(selected - set(curves.bump_labels))
    if unused:
        info += f" / 値なし・未使用: {', '.join(unused)}"
    return info
