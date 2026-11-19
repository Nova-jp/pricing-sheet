"""
JGBのASW(アセットスワップ・スプレッド)の計算の部品と、朝バッチの日次ASW(DBの asw_quotes)の計算。

■ 定義(Neonの ASW_data と同じ。書き込み元は別プロジェクト market-analysis の QuantLibHelper.calculate_mms)
  ASW = 現物の複利 − MMS(マッチド・マチュリティ・スワップ)。
  - 現物の複利: 単利 → 単価(スポット受渡 T+1、小数3桁で切り捨て)→ 利含み単価に一致する半年複利
    (うるう日除き/365、小数3桁で切り捨て。bond.compound_yield)。Neon側はJSDAの平均値の複利だが、
    こちらはDBの引け値(jgb_closes。みずほ→大和→JCL)を使う。
  - MMS: 起算日=スポット(T+2)、満期日=償還日(Modified Following)、償還日から遡る日程(roll conv=BWD)、
    固定側 Act/365・PA/SA、変動側も同じ頻度のOISのパーレート(swap_pricerの経路)。
  ASWPricerシート(engine/swap_pricing/asw/)もここの spot_bond・mms_rate を使う。

■ 朝バッチの日次ASW(build)
  カーブ(curves)と引け値(jgb_closes)がそろう全日付について、その日に引け値のある全銘柄のPA/SAのASW(%)を
  計算し、asw_quotes に日付ごと丸ごと置き換えて保存する(ASWシートの AswHistoricalRates が読む)。
  - 現物もスワップもその日にそろえる(その日のT+1受渡の複利と、その日のカーブのT+2起算のMMS)
  - 銘柄は asw_quotes の bond_code(回号5桁+種別4桁、例 JB380 → 003800067。ASWシートの読み替えと同じ形)で持つ
  - 入力(計算方法・その日のカーブの入力ハッシュ・引け値・銘柄情報)のハッシュが前回と同じ日は計算し直さない
  - 計算していない日(カーブか引け値の無い日・失敗した日)の行は消す(以前Neonから取得した値も残さない。
    2026-11-19 ユーザー判断)
"""

import hashlib
import json
import re
from datetime import date
from typing import Dict, Iterable, List, NamedTuple, Optional, Tuple

from engine.common import bond
from engine.common.conventions import ACT_365_FIXED, ROLL_BWD
from engine.common.curve import spot_date_for
from engine.common.historical_store import AswRate
from engine.common.market_data import Market
from engine.common.swap_pricer import par_rate
from engine.common.terms import SwapConvention, SwapTerms

SPOT_SETTLEMENT_DAYS = 1  # 現物のスポット受渡(T+1)
# Neonの ASW_data と同じMMSの規約(PA/SA)
MMS_CONVENTIONS = {
    freq: SwapConvention(fix_freq=freq, fix_dcf=ACT_365_FIXED, float_freq=freq, roll_conv=ROLL_BWD)
    for freq in ("PA", "SA")
}
# 計算方法の名前(定義・規約を変えたら変える。全日付を計算し直す)
ASW_METHOD = "close-t1-compound-minus-mms-t2-bwd-act365/v1"

_RIC = re.compile(r"JP(\d{4})(\d{4})=")


class SpotBond(NamedTuple):
    settle: date
    simple_yield: float  # %
    price: float  # 単価(額面100円あたり)
    accrued: float
    compound_yield: float  # 半年複利(%、小数3桁)


def spot_bond(jgb: bond.Jgb, simple_yield: float, valuation_date: date) -> SpotBond:
    """評価日のスポット受渡(T+1)での単価・複利。"""
    settle = bond.settlement_date(valuation_date, SPOT_SETTLEMENT_DAYS)
    if settle >= jgb.maturity:
        raise ValueError(f"受渡日 {settle} が償還日 {jgb.maturity} 以降です")
    price = bond.price_from_simple_yield(jgb, simple_yield, settle)
    accrued = bond.accrued_interest(jgb, settle)
    return SpotBond(settle, simple_yield, price, accrued, bond.compound_yield(jgb, price + accrued, settle))


def mms_rate(market: Market, maturity: date, convention: SwapConvention) -> float:
    """評価日のスポット(T+2)起算・償還日満期のMMSのパーレート(%)。"""
    terms = SwapTerms(spot_date_for(market.valuation_date), convention, end=maturity)
    return par_rate(market, terms).target_fixrate


def bond_code(ric: str) -> str:
    """JGBのRIC(JP03800067=)→ asw_quotes の bond_code(回号5桁+種別4桁、003800067)。"""
    m = _RIC.fullmatch(ric)
    if m is None:
        raise ValueError(f"JGBのRICの形ではありません: {ric!r}")
    return f"{int(m.group(1)):05d}{m.group(2)}"


class BondInfo(NamedTuple):
    bond_code: str
    coupon: float
    maturity: date


def compute_day(
    market: Market, closes: Dict[str, float], bonds: Dict[str, BondInfo],
) -> Tuple[Dict[str, AswRate], List[str], Dict[str, str]]:
    """その日の全銘柄のASW(%)。closes: {銘柄コード JB380: 単利}。
    → ({bond_code: AswRate(pa, sa)}, 償還間近で計算しない銘柄, {銘柄: 失敗の内容})。"""
    rates: Dict[str, AswRate] = {}
    near_maturity: List[str] = []
    errors: Dict[str, str] = {}
    spot = spot_date_for(market.valuation_date)
    for code in sorted(closes):
        info = bonds.get(code)
        if info is None:
            errors[code] = "銘柄情報(jgb_bonds)がありません"
            continue
        if spot >= info.maturity:
            near_maturity.append(code)  # MMSの起算日が償還日以降
            continue
        try:
            compound = spot_bond(bond.Jgb(info.coupon, info.maturity), closes[code], market.valuation_date).compound_yield
            pa, sa = (compound - mms_rate(market, info.maturity, MMS_CONVENTIONS[f]) for f in ("PA", "SA"))
        except Exception as e:  # 1銘柄の失敗で日付全体を止めない
            errors[code] = str(e)
            continue
        rates[info.bond_code] = AswRate(pa, sa)
    return rates, near_maturity, errors


def input_hash(curve_hash: str, closes: Dict[str, float], bonds: Dict[str, BondInfo]) -> str:
    rows = sorted(
        (code, y, *(bonds[code][1:] if code in bonds else (None, None))) for code, y in closes.items()
    )
    payload = json.dumps({"method": ASW_METHOD, "curve": curve_hash, "closes": rows}, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _bonds(store) -> Dict[str, BondInfo]:
    return {
        r.code: BondInfo(bond_code(r.ric), r.coupon, r.maturity) for r in store.jgb_master(date.max)
    }


def build(store, rebuild: bool = False, log=print, dates: Optional[Iterable[date]] = None) -> Dict[date, str]:
    """store(historical_store.HistoricalStore)のカーブと引け値のそろう全日付について asw_quotes を作る。
    入力が変わった日だけ計算する。dates: 計算する日付を絞る(省略時は全日付。試し用)。失敗した日 → 内容 を返す。"""
    curve_hashes = store.input_hashes()
    if not curve_hashes:
        log("ASW計算: カーブが無いためスキップ")
        return {}
    closes = store.jgb_closes_between(min(curve_hashes), max(curve_hashes))
    bonds = _bonds(store)
    existing = {} if rebuild else store.asw_input_hashes()
    targets = sorted(d for d in curve_hashes if d in closes)
    if dates is not None:
        wanted = set(dates)
        targets = [d for d in targets if d in wanted]
    keep: List[date] = []
    built = 0
    failed: Dict[date, str] = {}
    bond_errors: Dict[str, str] = {}
    with store:
        for d in targets:
            h = input_hash(curve_hashes[d], closes[d], bonds)
            if existing.get(d) == h:
                keep.append(d)
                continue
            try:
                stored = store.load(d)
                rates, _, errors = compute_day(Market(d, stored.curve, stored.index), closes[d], bonds)
            except Exception as e:  # 1日の失敗で全体を止めない
                failed[d] = str(e)
                continue
            store.save_asw_day(d, rates, h)
            keep.append(d)
            bond_errors.update(errors)
            built += 1
            if built % 100 == 0:
                log(f"  ASW 計算中... {built}日分")
        removed = 0 if dates is not None else store.delete_asw_except(keep)
    log(f"ASW計算: 新規/更新 {built}日 / 変更なし {len(keep) - built}日 / "
        f"失敗 {len(failed)}日 / 古い行を削除 {removed}日")
    for d, msg in sorted(failed.items()):
        log(f"  {d}: ASW計算の失敗 {msg}")
    for code, msg in sorted(bond_errors.items()):
        log(f"  {code}: ASWを計算できない銘柄 {msg}")
    return failed
