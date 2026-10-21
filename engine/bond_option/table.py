"""
BondOption.xlsmのOptionシートの全行を1回でまとめて計算する(表UDFの本体)。

xlwingsのUDFは1セルごとにExcelとの往復がかかり、行ごとにUDFを置くと再計算の時間がセルの数で決まる
(PricingTableと同じ理由、docs/decisions.md)。QuantLibが必要な計算だけを列ごとの範囲で受け取り、
行ごとの結果を1つの配列で返す。四則演算程度の計算(単利⇔単価・先渡価格・ボラの換算・ストライク)は
Excelの式のまま残す。

シートでは表UDFを右の計算用の領域にスピルさせ、表示の列はそのセルを参照するだけにする。表示の
セルを手で上書きすれば、下流の式(とその先の表UDF)はその値で計算される。

  market_table      : [先渡複利, 先渡DD, 先渡MD, ノーマルvol(補間), message](F7・F8・ボラの補間)
  itm_buy_write_table: [ストライク, message](F11。Strike basisが"ITMBW"の行だけ)
  premium_table     : [プレミアム(額面100円あたりの円), message](F10)

■ エラー
  1行の入力の誤りで表全体をエラーにしない。計算できなかった列は空欄にし、理由をmessage列に出す
  (シートの表示の列は空欄を#N/Aにする)。計算しない行(銘柄が空欄等)は全部空欄。
"""

import datetime
from datetime import date
from typing import Callable, List, Sequence

from engine.bond_option import pricer, vol
from engine.common import bond, udf_common

ITMBW = "ITMBW"


def as_date(value, name: str) -> date:
    """日付だけを受け付ける(スワップ用のIMMコード・テナーの表記は通さない)。"""
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return udf_common._to_date(value)  # Excelのシリアル値
    if udf_common._is_blank(value):
        raise ValueError(f"{name}が空です")
    raise ValueError(f"{name}は日付です: {value!r}")


def as_number(value, name: str) -> float:
    if udf_common._is_blank(value):
        raise ValueError(f"{name}が空です")
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name}は数値です: {value!r}")


def _rows(*columns: Sequence) -> List[tuple]:
    """列ごとの範囲 → 行ごとの組。列の長さが違えば短い列を空欄で埋める。"""
    cols = [list(c) if isinstance(c, (list, tuple)) else [c] for c in columns]
    n = max(len(c) for c in cols)
    return list(zip(*(c + [None] * (n - len(c)) for c in cols)))


def _try(f: Callable, errors: List[str], label: str):
    try:
        return f()
    except Exception as e:
        errors.append(f"{label}: {e}")
        return ""


def market_table(
    bonds: Sequence, coupons: Sequence, maturities: Sequence, forward_dirty: Sequence,
    forward_settle: Sequence, expiries: Sequence, vol_rows: Sequence, trade_date: date,
) -> List[list]:
    """行ごとの [先渡複利(%), 先渡DD, 先渡MD, ノーマルvol(bp/年、補間), message]。銘柄が空欄の行は空欄。"""
    grid_errors: List[str] = []
    grid = _try(lambda: vol.build_grid([list(r) for r in (vol_rows or []) if r is not None], trade_date),
                grid_errors, "Vol")
    out = []
    for code, coupon, maturity, dirty, settle, expiry in _rows(
            bonds, coupons, maturities, forward_dirty, forward_settle, expiries):
        if udf_common._is_blank(code):
            out.append([""] * 5)
            continue
        errors: List[str] = []
        risk = _try(lambda: pricer.forward_duration(
            bond.Jgb(as_number(coupon, "利率"), as_date(maturity, "償還日")),
            as_number(dirty, "先渡dirty"), as_date(settle, "行使後の受渡日"),
        ), errors, "Fwd risk")
        if grid_errors:
            normal = ""
            errors.extend(grid_errors)
        else:
            normal = _try(lambda: vol.grid_vol(grid, trade_date, as_date(expiry, "満期"), as_date(maturity, "償還日")),
                          errors, "Vol")
        row = list(risk) if risk != "" else ["", "", ""]
        out.append(row + [normal, " / ".join(errors)])
    return out


def itm_buy_write_table(
    bases: Sequence, forwards: Sequence, targets: Sequence, price_vols: Sequence, terms: Sequence,
) -> List[list]:
    """行ごとの [ストライク, message]。Strike basisが"ITMBW"の行だけ計算する(それ以外は空欄)。"""
    out = []
    for basis, forward, target, price_vol, term in _rows(bases, forwards, targets, price_vols, terms):
        if str(basis or "").strip().upper() != ITMBW:
            out.append(["", ""])
            continue
        errors: List[str] = []
        k = _try(lambda: pricer.itm_buy_write_strike(
            as_number(forward, "先渡価格"), as_number(target, "購入単価"), as_number(price_vol, "価格ボラ"),
            as_number(term, "期間"),
        ), errors, "ITMBW")
        out.append([k, " / ".join(errors)])
    return out


def premium_table(
    types: Sequence, forwards: Sequence, strikes: Sequence, price_vols: Sequence, terms: Sequence,
) -> List[list]:
    """行ごとの [プレミアム(額面100円あたりの円、割引なし), message]。種類(Call/Put)が空欄の行は空欄。"""
    out = []
    for option_type, forward, strike, price_vol, term in _rows(types, forwards, strikes, price_vols, terms):
        if udf_common._is_blank(option_type):
            out.append(["", ""])
            continue
        errors: List[str] = []
        premium = _try(lambda: pricer.black_premium(
            option_type, as_number(forward, "先渡価格"), as_number(strike, "ストライク"),
            as_number(price_vol, "価格ボラ"), as_number(term, "期間"),
        ), errors, "Premium")
        out.append([premium, " / ".join(errors)])
    return out
