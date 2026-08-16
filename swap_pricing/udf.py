"""
Excelのセル関数(UDF)として直接呼び出せる計算関数群。xlwingsのExcelアドイン経由で
`=FairRate(...)`のように、ネイティブのExcel関数と全く同じ感覚でどのセルにも置ける。

■ 設計方針
  - QuantLibが必要な計算(パーレート・PV・delta・notional逆算・ヒストリカル・
    バケットデルタ)だけをここに置く。カーブ(スプレッド)やフライ、複数行の
    合計といった単純な四則演算はExcel自身の数式に任せる(このファイルには書かない)。
  - 計算方法はブックの計算方法を「手動」にし、F9(再計算)で都度実行する想定。
    UDFは非volatileのままでよい(手動計算モードでもF9時点の最新セル値を
    使って再計算されるため)。VBAコードは一切不要。
  - RealTimeシートのTicker/Value範囲は、都度Excelから引数として渡してもらう
    (例: RealTime!A2:B43)。同じ内容(値のハッシュ)なら、直近1回分の
    ブートストラップ結果をキャッシュして使い回す(重い再計算を避けるため)。

■ 使い方(Windows実機でのセットアップ)
  1. pip install xlwings
  2. xlwings addin install (Excelアドインを1度だけインストール)
  3. ブックの _xlwings.conf シートの "UDF Modules" に "swap_pricing.udf" を設定
     (または xlwings のRibbonから対象ブックにこのモジュールを紐付ける)
  4. ブックの計算方法を手動に設定(このモジュールでは行わない、Excel側の設定)
"""

import datetime
import hashlib
from typing import Dict, List, Optional

import xlwings as xw

from swap_pricing.curve import BootstrapResult, bootstrap_curve
from swap_pricing.historical_pricer import historical_par_rate_series
from swap_pricing.risk import (
    SwapParams,
    annuity_delta,
    bucketed_delta,
    parallel_bump_delta,
    solve_notional_for_target_delta,
)
from swap_pricing.swap_pricer import price_swap

_curve_cache: Dict[tuple, BootstrapResult] = {}


def _to_date(value) -> Optional[datetime.date]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    raise ValueError(f"日付として解釈できません: {value!r}")


def _rates_from_range(real_time_range) -> Dict[str, float]:
    rates: Dict[str, float] = {}
    for row in real_time_range:
        if row is None:
            continue
        ticker, value = row[0], row[1]
        if ticker is None or value is None or value == "":
            continue
        rates[str(ticker).strip().lower()] = float(value)
    return rates


def _get_curve(rates: Dict[str, float], valuation_date: datetime.date):
    rates_hash = hashlib.md5(repr(sorted(rates.items())).encode()).hexdigest()
    key = (valuation_date, rates_hash)
    if key not in _curve_cache:
        _curve_cache.clear()  # 直近1スナップショット分だけ保持(溜め込まない)
        _curve_cache[key] = bootstrap_curve(valuation_date, rates)
    return _curve_cache[key]


def _resolve_valuation_date(valuation_date) -> datetime.date:
    d = _to_date(valuation_date)
    return d if d is not None else datetime.date.today()


@xw.func
@xw.arg("real_time_range", ndim=2)
def FairRate(
    start, tenor, end, fix_freq, fix_dcf, float_freq, float_dcf, roll_conv,
    real_time_range, valuation_date=None,
) -> float:
    """指定コンベンションのパーレート(%)を返す。tenorかendのどちらかは空欄にする。"""
    valuation_date = _resolve_valuation_date(valuation_date)
    rates = _rates_from_range(real_time_range)
    boot = _get_curve(rates, valuation_date)
    result = price_swap(
        boot.curve, valuation_date, _to_date(start), fix_freq, fix_dcf, float_freq, float_dcf,
        roll_conv, notional=1.0, pay_rec="PAY", tenor=tenor or None, end=_to_date(end),
        index=boot.index,
    )
    return result.target_fixrate


@xw.func
@xw.arg("real_time_range", ndim=2)
def SwapPV(
    start, tenor, end, fix_freq, fix_dcf, float_freq, float_dcf, roll_conv,
    notional, pay_rec, fix_rate, real_time_range, valuation_date=None,
) -> float:
    """PV(入力fix_rateが空欄ならパーレートで計算、常に0近辺)。"""
    valuation_date = _resolve_valuation_date(valuation_date)
    rates = _rates_from_range(real_time_range)
    boot = _get_curve(rates, valuation_date)
    result = price_swap(
        boot.curve, valuation_date, _to_date(start), fix_freq, fix_dcf, float_freq, float_dcf,
        roll_conv, notional, pay_rec, tenor=tenor or None, end=_to_date(end),
        fix_rate=fix_rate if fix_rate not in ("", None) else None, index=boot.index,
    )
    return result.pv


def _resolved_params(
    start, tenor, end, fix_freq, fix_dcf, float_freq, float_dcf, roll_conv,
    notional, pay_rec, fix_rate, boot, valuation_date,
) -> SwapParams:
    base = price_swap(
        boot.curve, valuation_date, _to_date(start), fix_freq, fix_dcf, float_freq, float_dcf,
        roll_conv, notional, pay_rec, tenor=tenor or None, end=_to_date(end),
        fix_rate=fix_rate if fix_rate not in ("", None) else None, index=boot.index,
    )
    return SwapParams(
        start=_to_date(start), fix_freq=fix_freq, fix_dcf=fix_dcf, float_freq=float_freq,
        float_dcf=float_dcf, roll_conv=roll_conv, notional=notional, pay_rec=pay_rec,
        tenor=tenor or None, end=_to_date(end), fix_rate=base.fix_rate_used,
    ), base


@xw.func
@xw.arg("real_time_range", ndim=2)
def SwapDelta(
    start, tenor, end, fix_freq, fix_dcf, float_freq, float_dcf, roll_conv,
    notional, pay_rec, fix_rate, real_time_range, valuation_date=None,
) -> float:
    """バンプデルタ(百万円単位)。fix_rateが空欄ならパーレートに固定してから計算する。"""
    valuation_date = _resolve_valuation_date(valuation_date)
    rates = _rates_from_range(real_time_range)
    boot = _get_curve(rates, valuation_date)
    params, _ = _resolved_params(
        start, tenor, end, fix_freq, fix_dcf, float_freq, float_dcf, roll_conv,
        notional, pay_rec, fix_rate, boot, valuation_date,
    )
    delta_yen = parallel_bump_delta(rates, valuation_date, params)
    return delta_yen / 1_000_000.0


@xw.func
@xw.arg("real_time_range", ndim=2)
def SwapAnnuityDelta(
    start, tenor, end, fix_freq, fix_dcf, float_freq, float_dcf, roll_conv,
    notional, pay_rec, real_time_range, valuation_date=None,
) -> float:
    """アニュイティデルタ(解析的近似、参考値、百万円単位)。"""
    valuation_date = _resolve_valuation_date(valuation_date)
    rates = _rates_from_range(real_time_range)
    boot = _get_curve(rates, valuation_date)
    result = price_swap(
        boot.curve, valuation_date, _to_date(start), fix_freq, fix_dcf, float_freq, float_dcf,
        roll_conv, notional, pay_rec, tenor=tenor or None, end=_to_date(end), index=boot.index,
    )
    return annuity_delta(result.annuity, notional, pay_rec) / 1_000_000.0


@xw.func
@xw.arg("real_time_range", ndim=2)
def NotionalForDelta(
    target_delta_mm, start, tenor, end, fix_freq, fix_dcf, float_freq, float_dcf, roll_conv,
    pay_rec, real_time_range, valuation_date=None,
) -> float:
    """目標delta(百万円単位)から必要なnotionalを逆算する(アニュイティデルタ基準)。"""
    valuation_date = _resolve_valuation_date(valuation_date)
    rates = _rates_from_range(real_time_range)
    boot = _get_curve(rates, valuation_date)
    result = price_swap(
        boot.curve, valuation_date, _to_date(start), fix_freq, fix_dcf, float_freq, float_dcf,
        roll_conv, notional=1.0, pay_rec=pay_rec, tenor=tenor or None, end=_to_date(end),
        index=boot.index,
    )
    target_delta_yen = float(target_delta_mm) * 1_000_000.0
    return solve_notional_for_target_delta(target_delta_yen, result.annuity, pay_rec)


@xw.func
@xw.ret(expand="table")
def HistoricalOutright(tenor, fix_freq, fix_dcf, float_freq, float_dcf, roll_conv) -> List[list]:
    """
    ローカルキャッシュ全期間の、指定テナーのヒストリカル・パーレート推移を返す
    (Date/Rateの2列スピル配列)。カーブ(スプレッド)やフライは、この結果を
    Excel側で引き算するだけで求まるため別関数にしていない。
    """
    series = historical_par_rate_series(fix_freq, fix_dcf, float_freq, float_dcf, roll_conv, tenor=tenor)
    rows = [["Date", "Rate"]]
    rows += [[p.as_of_date, p.par_rate] for p in series]
    return rows


@xw.func
@xw.arg("real_time_range", ndim=2)
@xw.ret(expand="table")
def BucketedDelta(
    start, tenor, end, fix_freq, fix_dcf, float_freq, float_dcf, roll_conv,
    notional, pay_rec, fix_rate, real_time_range, valuation_date=None,
) -> List[list]:
    """
    RealTimeシートの各テナーを個別に+1bpバンプしたバケットデルタ(百万円単位)を返す
    (テナーラベル行・値行の2行スピル配列、1スワップ単位)。複数スワップの合計は
    Excel側のSUMで行う想定。
    """
    valuation_date = _resolve_valuation_date(valuation_date)
    rates = _rates_from_range(real_time_range)
    boot = _get_curve(rates, valuation_date)
    params, _ = _resolved_params(
        start, tenor, end, fix_freq, fix_dcf, float_freq, float_dcf, roll_conv,
        notional, pay_rec, fix_rate, boot, valuation_date,
    )
    deltas = bucketed_delta(rates, valuation_date, params)
    labels = [k for k in rates if k in deltas]
    values = [deltas[k] / 1_000_000.0 for k in labels]
    return [labels, values]
