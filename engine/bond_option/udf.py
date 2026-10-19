"""
現物オプション・プライサー(BondOption.xlsm)のUDF。式ごとに1つのUDFにし、Excelの1行で組み合わせる。

  ValuationDate: 評価日(取引日)。空欄=今日。シートの他の式はこの結果を参照する
  JgbInfo      : 銘柄コード → [利率, 償還日]
  JgbClose     : 銘柄コード → [引け値の単利, 引けの日付](評価日以前で直近。配信元JCL)
  JgbSettle    : 取引日・行使日の T+n(営業日)
  JgbPrice     : 単利 → 単価(F1。起点の日付を渡す: スポットはスポット受渡日、ストライクは満期日)
  JgbYield     : 単価 → 単利(F2。表示用)
  BondForward  : 先渡価格 [clean, dirty](F6)
  ForwardDuration: [先渡複利, 先渡DD, 先渡MD](F7・F8)
  OptionTerm   : 期間T(営業日/252)
  SwaptionVol  : スワップションのノーマルvol(bp/年)を満期×テナーで補間
  BlackPremium : プレミアム(額面100円あたりの円、割引なし)(F10)
ATM単利(引け値+スライド)、bp vol(ノーマルvol/√252)、価格ボラ(bp vol×√252×MD)、オーバーライドの選択、
銭への換算はExcelの数式で行う(四則演算はUDFにしない)。式の詳細は docs/bond_option.md。

■ データ
  銘柄情報・引け値は朝のバッチ(python -m engine.common.morning_batch)がLSEGから data/historical.db に
  保存したものを読むだけ。ボラはフィード(live_feed.toml の "vol")を LiveQuotes("vol") で読んだ範囲を渡す。
  計算方法は「手動」+F9。評価日はシートの評価日セル(空欄=今日)。TODAY()は使わない。
"""

from typing import List

import xlwings as xw

from engine.bond_option import pricer, vol
from engine.common import bond, udf_common


def _code(value) -> str:
    if udf_common._is_blank(value):
        raise ValueError("銘柄コードが空です(例 JB380)")
    return str(value).strip().upper()


def _jgb(code) -> bond.Jgb:
    code = _code(code)
    with udf_common._historical_store() as store:
        row = store.jgb_bond(code)
    if row is None:
        raise ValueError(
            f"銘柄 {code} がDBにありません(python -m engine.common.morning_batch でLSEGから取得してください)"
        )
    return bond.Jgb(row.coupon, row.maturity)


def _date(value, name: str):
    d = udf_common._to_date(value)
    if d is None:
        raise ValueError(f"{name}が空です")
    return d


def _number(value, name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name}は数値です: {value!r}")


@xw.func
def ValuationDate(valuation_date=None):
    """評価日(取引日)。空欄=今日。TODAY()を使わないため(volatileになる)、空欄の解決をUDFで行う。"""
    return udf_common._resolve_valuation_date(valuation_date)


@xw.func
def JgbInfo(code) -> List[list]:
    """[利率(%), 償還日](横にスピル)。"""
    b = _jgb(code)
    return [[b.coupon, b.maturity]]


@xw.func
def JgbClose(code, valuation_date=None) -> List[list]:
    """[引け値の単利(%), 引けの日付](横にスピル)。評価日(空欄=今日)以前で直近の引け値。"""
    code = _code(code)
    as_of = udf_common._resolve_valuation_date(valuation_date)
    with udf_common._historical_store() as store:
        row = store.jgb_close(code, as_of)
    if row is None:
        raise ValueError(f"{code}の{as_of}以前の引け値がDBにありません")
    return [[row.simple_yield, row.as_of_date]]


@xw.func
def JgbSettle(trade_date, days=1):
    """T+days(日本の営業日)。スポット受渡は取引日のT+1、行使後の受渡は行使日のT+1またはT+2。"""
    return bond.settlement_date(_date(trade_date, "日付"), int(_number(days, "日数")))


@xw.func
def JgbPrice(code, simple_yield, start_date) -> float:
    """単利(%) → 単価(小数3桁で切り捨て)。日数は start_date→償還日(うるう日除き/365)。"""
    return bond.price_from_simple_yield(
        _jgb(code), _number(simple_yield, "単利"), _date(start_date, "起点の日付"),
    )


@xw.func
def JgbYield(code, price, start_date) -> float:
    """単価 → 単利(%、小数3桁で切り捨て)。"""
    return bond.simple_yield_from_price(_jgb(code), _number(price, "単価"), _date(start_date, "起点の日付"))


@xw.func
def BondForward(code, spot_price, spot_settle, forward_settle, repo) -> List[list]:
    """[先渡clean, 先渡dirty](横にスピル)。レポは%(単利・暦日/365)。"""
    fwd = pricer.forward_price(
        _jgb(code), _number(spot_price, "スポット単価"), _date(spot_settle, "スポット受渡日"),
        _date(forward_settle, "行使後の受渡日"), _number(repo, "レポ"),
    )
    return [[fwd.clean, fwd.dirty]]


@xw.func
def ForwardDuration(code, forward_dirty, forward_settle) -> List[list]:
    """[先渡複利(%), 先渡DD, 先渡MD](横にスピル)。"""
    d = pricer.forward_duration(
        _jgb(code), _number(forward_dirty, "先渡dirty"), _date(forward_settle, "行使後の受渡日"),
    )
    return [[d.compound_yield, d.dollar_duration, d.modified_duration]]


@xw.func
def OptionTerm(trade_date, expiry) -> float:
    """期間T = 営業日数(取引日→満期)/252。取引日の空欄=今日。"""
    return pricer.option_term(udf_common._resolve_valuation_date(trade_date), _date(expiry, "満期"))


@xw.func
@xw.arg("vol_range", ndim=2)
def SwaptionVol(vol_range, trade_date, expiry, code) -> float:
    """スワップションのATMノーマルvol(bp/年)を、満期(取引日→オプション満期)×テナー(満期→償還日)で補間。
    vol_range: LiveQuotes("vol") 等の [銘柄(1Mx10Y), …, 値] の範囲(1列目と最後の列を使う)。"""
    rows = [list(r) for r in (vol_range or []) if r is not None]
    return vol.bond_option_vol(
        rows, udf_common._resolve_valuation_date(trade_date), _date(expiry, "満期"), _jgb(code).maturity,
    )


@xw.func
def BlackPremium(option_type, forward, strike, price_vol, term) -> float:
    """プレミアム(額面100円あたりの円、割引なし)。銭はExcel側で×100。"""
    return pricer.black_premium(
        option_type, _number(forward, "先渡価格"), _number(strike, "ストライク"),
        _number(price_vol, "価格ボラ"), _number(term, "期間"),
    )
