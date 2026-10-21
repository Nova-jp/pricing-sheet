"""
現物オプション・プライサー(BondOption.xlsm)のUDF。

Excelの式では書きにくいものだけをUDFにし、それ以外(受渡日・営業日数・単利⇔単価・経過利子・先渡価格・
ストライク・ボラの選択・価格ボラ・銭への換算)はExcelの式で計算する。不具合のときもセルに値を
手で入れれば残りが計算されるように、UDFは銘柄コードではなく利率・償還日のセルの値を受け取る。

  ValuationDate    : 評価日(取引日)。空欄=今日。シートの他の式はこの結果を参照する
  JgbMaster        : 全銘柄の [銘柄コード, 利率, 償還日, 引け値の単利, 引けの日付, RIC, ISIN](Bondsシート)
  JapanHolidays    : 日本の祝日(土日を除く)の一覧(Holidaysシート。WORKDAY・NETWORKDAYSに渡す)
  JgbForwardRisk   : [先渡複利, 先渡DD, 先渡MD](F7・F8。利回りの解の探索とQuantLibのデュレーション)
  SwaptionVol      : スワップションのノーマルvol(bp/年)を満期×テナーで補間
  BlackPremium     : プレミアム(額面100円あたりの円、割引なし)(F10)
  ItmBuyWriteStrike: ITMバイライトのストライク(ストライク+プレミアム=購入単価、F11)
  OptionMarketTable / ItmBuyWriteTable / PremiumTable:
                     上の3つ(先渡リスク+ボラの補間、ITMBW、プレミアム)の表版。シートはこちらを使い、全行を
                     1回で計算する(1セルごとの往復を減らすため。本体は table.py)。1行用は単独の確認用に残す
式の詳細とシートの構成は docs/bond_option.md。

■ データ
  銘柄情報・引け値は朝のバッチ(python -m engine.common.morning_batch)がLSEGから data/historical.db に
  保存したものを読むだけ。ボラはフィード(live_feed.toml の "vol")を LiveQuotes("vol") で読んだ範囲を渡す。
  計算方法は「手動」+F9。評価日はシートの評価日セル(空欄=今日)。TODAY()は使わない。
"""

from typing import List

import QuantLib as ql
import xlwings as xw

from engine.bond_option import pricer, table, vol
from engine.bond_option.table import as_date as _date, as_number as _number
from engine.common import bond, udf_common
from engine.common.conventions import CALENDAR


def _jgb(coupon, maturity) -> bond.Jgb:
    return bond.Jgb(_number(coupon, "利率"), _date(maturity, "償還日"))


def _blank_if_none(value):
    return "" if value is None else value


@xw.func
def ValuationDate(valuation_date=None):
    """評価日(取引日)。空欄=今日。TODAY()を使わないため(volatileになる)、空欄の解決をUDFで行う。"""
    return udf_common._resolve_valuation_date(valuation_date)


@xw.func
def JgbMaster(valuation_date=None) -> List[list]:
    """全銘柄(償還日の順)の [銘柄コード, 利率(%), 償還日, 引け値の単利(%), 引けの日付, RIC, ISIN]。
    引け値は評価日(空欄=今日)以前で直近のもの。無ければ空欄。"""
    as_of = udf_common._resolve_valuation_date(valuation_date)
    with udf_common._historical_store() as store:
        rows = store.jgb_master(as_of)
    if not rows:
        raise ValueError("JGBの銘柄がDBにありません(python -m engine.common.morning_batch でLSEGから取得してください)")
    return [
        [r.code, r.coupon, r.maturity, _blank_if_none(r.close_yield), _blank_if_none(r.close_date), r.ric, r.isin]
        for r in rows
    ]


@xw.func
def JapanHolidays(start_date, end_date) -> List[list]:
    """start_date〜end_date の日本の祝日(土日に当たるものは除く、ql.Japan())を縦に並べる。
    引数名に end は使えない(VBAの予約語 End と衝突し、xlwingsのラッパーがコンパイルできない)。"""
    first, last = _date(start_date, "開始日"), _date(end_date, "終了日")
    if last < first:
        raise ValueError(f"終了日 {last} が開始日 {first} より前です")
    days = ql.Calendar.holidayList(CALENDAR, ql.Date.from_date(first), ql.Date.from_date(last), False)
    return [[d.to_date()] for d in days] or [[""]]


@xw.func
def JgbForwardRisk(coupon, maturity, forward_dirty, forward_settle) -> List[list]:
    """[先渡複利(%), 先渡DD, 先渡MD](横にスピル)。forward_dirty は先渡dirty、forward_settle は行使後の受渡日。"""
    d = pricer.forward_duration(
        _jgb(coupon, maturity), _number(forward_dirty, "先渡dirty"), _date(forward_settle, "行使後の受渡日"),
    )
    return [[d.compound_yield, d.dollar_duration, d.modified_duration]]


@xw.func
@xw.arg("vol_range", ndim=2)
def SwaptionVol(vol_range, trade_date, expiry, maturity) -> float:
    """スワップションのATMノーマルvol(bp/年)を、満期(取引日→オプション満期)×テナー(満期→償還日)で補間。
    vol_range: LiveQuotes("vol") 等の [銘柄(1Mx10Y), …, 値] の範囲(1列目と最後の列を使う)。"""
    rows = [list(r) for r in (vol_range or []) if r is not None]
    return vol.bond_option_vol(
        rows, udf_common._resolve_valuation_date(trade_date), _date(expiry, "満期"), _date(maturity, "償還日"),
    )


@xw.func
def BlackPremium(option_type, forward, strike, price_vol, term) -> float:
    """プレミアム(額面100円あたりの円、割引なし)。銭はExcel側で×100。"""
    return pricer.black_premium(
        option_type, _number(forward, "先渡価格"), _number(strike, "ストライク"),
        _number(price_vol, "価格ボラ"), _number(term, "期間"),
    )


@xw.func
def ItmBuyWriteStrike(forward, target, price_vol, term) -> float:
    """ITMバイライトのストライク単価(小数3桁で切り捨て)。ストライク + コールのプレミアム = target。
    forward=先渡clean、target=購入単価(スポット単価)、price_vol=価格ボラ(%/年)、term=T。"""
    return pricer.itm_buy_write_strike(
        _number(forward, "先渡価格"), _number(target, "購入単価"), _number(price_vol, "価格ボラ"),
        _number(term, "期間"),
    )


# ---- 表UDF(Optionシートの全行を1回で計算する。本体は table.py) ----

@xw.func
@xw.arg("bonds", ndim=1)
@xw.arg("coupons", ndim=1)
@xw.arg("maturities", ndim=1)
@xw.arg("forward_dirty", ndim=1)
@xw.arg("forward_settle", ndim=1)
@xw.arg("expiries", ndim=1)
@xw.arg("vol_range", ndim=2)
def OptionMarketTable(
    bonds, coupons, maturities, forward_dirty, forward_settle, expiries, vol_range, trade_date,
) -> List[list]:
    """全行の [先渡複利(%), 先渡DD, 先渡MD, ノーマルvol(bp/年、補間), message](JgbForwardRisk・SwaptionVolの表版)。
    引数は同じ行数の列の範囲(銘柄・利率・償還日・先渡dirty・行使後の受渡日・満期)と、ボラの範囲・取引日。"""
    return table.market_table(
        bonds, coupons, maturities, forward_dirty, forward_settle, expiries, vol_range,
        udf_common._resolve_valuation_date(trade_date),
    )


@xw.func
@xw.arg("bases", ndim=1)
@xw.arg("forwards", ndim=1)
@xw.arg("targets", ndim=1)
@xw.arg("price_vols", ndim=1)
@xw.arg("terms", ndim=1)
def ItmBuyWriteTable(bases, forwards, targets, price_vols, terms) -> List[list]:
    """全行の [ITMバイライトのストライク, message](ItmBuyWriteStrikeの表版)。Strike basisが"ITMBW"の行だけ計算する。
    引数は同じ行数の列の範囲(Strike basis・先渡clean・スポット単価・価格ボラ・T)。"""
    return table.itm_buy_write_table(bases, forwards, targets, price_vols, terms)


@xw.func
@xw.arg("types", ndim=1)
@xw.arg("forwards", ndim=1)
@xw.arg("strikes", ndim=1)
@xw.arg("price_vols", ndim=1)
@xw.arg("terms", ndim=1)
def PremiumTable(types, forwards, strikes, price_vols, terms) -> List[list]:
    """全行の [プレミアム(額面100円あたりの円、割引なし), message](BlackPremiumの表版)。
    引数は同じ行数の列の範囲(Call/Put・先渡clean・ストライク・価格ボラ・T)。"""
    return table.premium_table(types, forwards, strikes, price_vols, terms)
