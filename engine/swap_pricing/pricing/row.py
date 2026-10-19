"""
pricingシートの1行分の入力(Excelのセルの値)を取引条件の型に直す。Excelの値を型に直すのはここだけ。

- swap_terms: start/tenor/end と規約 → SwapTerms(パーレートの計算単位)
- swap_params: SwapTerms+想定元本・PAY/REC・固定金利 → SwapParams(PV・デルタの計算単位)
"""

from engine.common import udf_common
from engine.common.conventions import business_days_start, forward_start_date, is_business_days, is_tenor
from engine.common.terms import SwapConvention, SwapParams, SwapTerms


def start_date(value, curves):
    """start列の値 → 起算日。日付・IMMコードに加え、テナー(10y等)ならスポット日+tenor
    (フォワードスタート)、営業日数(2bd等)なら評価日からN営業日後(2bd=スポット日)。"""
    if is_tenor(value):
        return forward_start_date(curves.spot_date, value)
    if is_business_days(value):
        return business_days_start(curves.valuation_date, value)
    return udf_common._to_date(value)


def swap_terms(start, tenor, end_date, fix_freq, fix_dcf, float_freq, roll_conv, curves) -> SwapTerms:
    """curves: 評価日のカーブ一式(startがテナー・営業日数の入力のときの起点=スポット日・評価日を使う)。"""
    return SwapTerms(
        start_date(start, curves), SwapConvention(fix_freq, fix_dcf, float_freq, roll_conv),
        tenor=None if udf_common._is_blank(tenor) else tenor, end=udf_common._to_date(end_date),
    )


def swap_params(terms: SwapTerms, notional, pay_rec, fix_rate) -> SwapParams:
    """fix_rateが空欄ならNone(パーレートで組む。デルタではパーレートに固定する)。"""
    return SwapParams(
        terms, float(notional), pay_rec,
        fix_rate=None if udf_common._is_blank(fix_rate) else float(fix_rate),
    )
