"""
ASWシート用のUDF(JGB銘柄別ASWスプレッドのヒストリカル推移)と、ASWPricerシート用のUDF
(AswPricingTable。リアルタイムのカーブと引け値+スライドでASWを計算する。本体は table.py・pricer.py)。

ASWはスワップのROLLING/FIXEDのような「基準日からの平行移動」が無く、銘柄コード
(bond_code)ごとに日付→値が決まっているだけ(Act/365、PA/SAの2コンベンション)。
Neon取得・ローカル保存は朝バッチ(engine/common/morning_batch.py)が行い、ここは
`asw_quotes`(engine/common/historical_store.py)を読むだけ(カーブは組まない)。

Excel境界の型変換は`engine.common.udf_common`を`udf_common.xxx(...)`の形で呼ぶ
(名前のコピーはしない。理由はudf_common.pyのdocstring)。
"""

from typing import Dict, List, Optional

import xlwings as xw

from engine.common import udf_common
from engine.common.historical_store import AswRate
from engine.swap_pricing.asw import table

_CONVENTIONS = ("PA", "SA")
_MAX_LEGS = 3
_LEG_LABELS = ("A", "B", "C")


def _convention(value) -> str:
    conv = str(value).strip().upper()
    if conv not in _CONVENTIONS:
        raise ValueError(f"PA/SAのいずれかを指定してください: {value!r}")
    return conv


def _rate(q: Optional[AswRate], convention: str):
    if q is None:
        return ""
    return q.pa if convention == "PA" else q.sa


@xw.func
def AswHistoricalRate(bond_code, convention, from_date=None, to_date=None) -> List[list]:
    """
    指定銘柄(bond_code)のASWスプレッド推移(Date/Rateの2列スピル配列、1行目は見出し)。

    convention: "PA"/"SA"(ASW_data.asw_act365_pa / asw_act365_sa)。
    from_date/to_date: 計算する過去日の範囲(空欄=DBにある全期間)。
    """
    conv = _convention(convention)
    with udf_common._historical_store() as store:
        quotes = store.asw_quotes_for(
            str(bond_code).strip(), udf_common._to_date(from_date), udf_common._to_date(to_date),
        )
    rows = [["Date", "Rate"]]
    rows += [[d, _rate(quotes[d], conv)] for d in sorted(quotes)]
    return rows


def _codes_from_range(bond_codes) -> List[Optional[str]]:
    """銘柄コード範囲(最大3行×1列)→ 脚ごとのbond_code。空行は脚なし(None)。"""
    rows = [list(r) if r is not None else [] for r in (bond_codes or [])]
    if len(rows) > _MAX_LEGS:
        raise ValueError(f"脚は最大{_MAX_LEGS}本(3行)までです: {len(rows)}行")
    codes: List[Optional[str]] = []
    for row in rows:
        value = row[0] if row else None
        codes.append(None if udf_common._is_blank(value) else str(value).strip())
    return codes


@xw.func
@xw.arg("bond_codes", ndim=2)
def AswHistoricalRates(bond_codes, convention, from_date=None, to_date=None) -> List[list]:
    """
    最大3銘柄(カーブ・フライの各脚)のASWスプレッドを、同じ日付の並びで返す
    (Date/A/B/Cの4列スピル配列、1行目は見出し)。カーブ(B−A)・フライ(2B−A−C)の
    引き算はExcel側の数式で行う(ここでは各脚のASWスプレッドだけを計算する)。

    bond_codes: 最大3行×1列(銘柄コード)。空行は脚なしで、その列は空欄になる。
        脚が1本もなければ日付の列だけを返す。
    convention: "PA"/"SA"(全脚共通。カーブ・フライの計算が混在しないよう統一する)。
    from_date/to_date: 計算する過去日の範囲(空欄=DBにある全期間)。
    """
    conv = _convention(convention)
    codes = _codes_from_range(bond_codes)
    active = [c for c in codes if c is not None]
    with udf_common._historical_store() as store:
        per_leg: Dict[str, Dict] = {
            c: store.asw_quotes_for(c, udf_common._to_date(from_date), udf_common._to_date(to_date))
            for c in active
        }
    all_dates = sorted(set().union(*per_leg.values())) if per_leg else []
    rows = [["Date", *_LEG_LABELS]]
    for d in all_dates:
        row = [d]
        for code in codes + [None] * (_MAX_LEGS - len(codes)):
            row.append(_rate(per_leg[code].get(d), conv) if code is not None else "")
        rows.append(row)
    return rows


@xw.func
@xw.arg("asw_table", ndim=2)
@xw.arg("real_time_range", ndim=2)
def AswPricingTable(asw_table, real_time_range, valuation_date=None) -> List[list]:
    """
    ASWPricer表の全行の coupon・maturity・close・close date・simple yield・settle・price・compound yield・
    start date・end date・swap rate(MMS)・par-par asw(bp)・adj_notional・message を1回で返す
    (1行目は見出し、以降は入力の行と同じ並び。計算はflag=1の行だけ。詳細はasw/table.py)。
    ASW(bp) = (compound yield − swap rate)×100 はシートの式で計算する。

    asw_table: 見出し行から最終行まで、入力の列だけ(出力の列を含めると循環参照になる)。列は見出し名で探す
        (flag, bond, slide, yield override, delta, start, fix freq, fix dcf, float freq, roll conv, pay/rec)。
    real_time_range: PricingTableと同じ(そのシートの TAKE(カーブ用の範囲#,,3))。
    """
    curves = udf_common._market(real_time_range, valuation_date)
    with udf_common._historical_store() as store:
        return table.price_table(asw_table, curves, store)
