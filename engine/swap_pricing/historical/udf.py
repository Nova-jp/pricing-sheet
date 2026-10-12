"""
HistChartシート用のUDF(任意コンベンションのヒストリカル・パーレート推移)。

ブートストラップをせず、朝バッチ(engine/common/morning_batch.py)が作った日次カーブDB
(data/historical.db)を読むだけ。Excel境界の型変換・評価日解決・`_market`は
`engine.common.udf_common`を`udf_common.xxx(...)`の形で呼ぶ(名前のコピーはしない。
理由はudf_common.pyのdocstring)。
"""

import datetime
from typing import List, Optional

import QuantLib as ql
import xlwings as xw

from engine.common import udf_common
from engine.common.conventions import CALENDAR, is_imm_code, is_tenor
from engine.swap_pricing.historical.pricer import (
    SwapSpec,
    historical_multi_rate_series,
    historical_swap_rate_series,
)
from engine.common.historical_store import HistoricalStore
from engine.common.market import db_fixings
from engine.common.terms import SwapConvention


def _date_or_imm(value):
    """ヒストリカル用のstart/end列の値 → 日付、IMMコード・テナーの文字列、または None(空欄)。
    IMMコードとテナー(startのフォワードスタート)は文字列のまま渡し、日付への変換は
    historical.pricer が基準日のスポット日を決めてから行う。"""
    if isinstance(value, str) and (is_imm_code(value) or is_tenor(value)):
        return value.strip().upper()
    return udf_common._to_date(value)


@xw.func
def HistoricalRate(
    start, tenor, end_date, fix_freq, fix_dcf, float_freq, roll_conv, mode,
    base_date=None, from_date=None, to_date=None,
) -> List[list]:
    """
    pricingシートの1行分のコンベンションのヒストリカル・パーレート推移
    (Date/Rate/Start/Endの4列スピル配列、1行目は見出し)。

    mode: "FIXED"(同じ日付の取引をそのまま過去日で評価)/
          "ROLLING"(基準日のスポット日からの距離を保って各過去日へ平行移動)。
          詳細は engine/swap_pricing/historical/pricer.py。
    base_date: ROLLINGの基準日(空欄=今日)。通常は Main!評価日 を渡す。
    from_date/to_date: 計算する過去日の範囲(空欄=DBにある全期間)。
    過去起算になる日のTONA実績は、DBに取り込み済みのもの(Fixingシート由来)を使う。
    """
    with udf_common._historical_store() as store:
        spec = SwapSpec(
            SwapConvention(fix_freq, fix_dcf, float_freq, roll_conv),
            start=_date_or_imm(start), tenor=tenor or None, end=_date_or_imm(end_date),
        )
        series = historical_swap_rate_series(
            spec, mode,
            base_date=udf_common._to_date(base_date), from_date=udf_common._to_date(from_date),
            to_date=udf_common._to_date(to_date), fixings=db_fixings(store), curves=store,
        )
    rows = [["Date", "Rate", "Start", "End"]]
    rows += [[p.as_of_date, p.par_rate, p.start_date, p.maturity_date] for p in series]
    return rows


_MAX_LEGS = 3
_LEG_LABELS = ("A", "B", "C")


def _specs_from_range(conventions) -> List[Optional[SwapSpec]]:
    """コンベンション範囲(最大3行 × start/tenor/end/fix freq/fix dcf/float freq/roll conv の
    7列)→ 脚ごとの SwapSpec。start・tenor・endが全て空の行は脚なし(None)。"""
    rows = [list(r) if r is not None else [] for r in (conventions or [])]
    if len(rows) > _MAX_LEGS:
        raise ValueError(f"脚は最大{_MAX_LEGS}本(3行)までです: {len(rows)}行")
    specs: List[Optional[SwapSpec]] = []
    for n, row in enumerate(rows, start=1):
        start, tenor, end, fix_freq, fix_dcf, float_freq, roll_conv = (row + [None] * 7)[:7]
        if all(udf_common._is_blank(v) for v in (start, tenor, end)):
            specs.append(None)
            continue
        try:
            convention = SwapConvention(fix_freq, fix_dcf, float_freq, roll_conv)
        except ValueError as e:
            raise ValueError(f"{n}本目: {e}") from e
        specs.append(SwapSpec(
            convention,
            start=_date_or_imm(start), tenor=None if udf_common._is_blank(tenor) else str(tenor).strip(),
            end=_date_or_imm(end),
        ))
    return specs


class _WithRealTimeLatest:
    """ヒストリカルDBの日付に、今日(RealTimeシートのライブのレート)を最新日として加えたカーブの
    供給元。今日のカーブはpricingシートと同じ market のカーブ一式を使う(DBに今日の引け値が
    あっても、今日はライブのレートを優先する)。日付だけを求める呼び出しではカーブを作らない。"""

    def __init__(self, store: HistoricalStore, real_time_range, today: datetime.date):
        self._store = store
        self._real_time_range = real_time_range
        self._today = today

    def dates(self) -> List[datetime.date]:
        return [d for d in self._store.dates() if d < self._today] + [self._today]

    def load(self, as_of_date: datetime.date, until=None):
        if as_of_date == self._today:
            return udf_common._market(self._real_time_range, self._today).base
        return self._store.load(as_of_date, until=until)


@xw.func
@xw.arg("conventions", ndim=2)
@xw.arg("real_time_range", ndim=2)
def HistoricalRates(
    conventions, mode, base_date=None, from_date=None, to_date=None, real_time_range=None,
) -> List[list]:
    """
    最大3本(カーブ・フライの各脚)のヒストリカル・パーレートを、同じ日付の並びで返す
    (Date/A/B/Cの4列スピル配列、1行目は見出し)。カーブ(B−A)・フライ(2B−A−C)の
    引き算はExcel側の数式で行う(ここでは各脚のパーレートだけを計算する)。

    conventions: 最大3行 × 7列(start, tenor, end, fix freq, fix dcf, float freq,
        roll conv。pricingシートと同じ並び)。start・tenor・endが全て空の行は
        脚なしで、その列は空欄になる。脚が1本もなければ日付の列だけを返す。
    mode: "FIXED" / "ROLLING"(全脚に共通)。base_date/from_date/to_date は HistoricalRate と同じ。
    期間内でカーブのある日付は全て返し、計算できない日(FIXEDで満期後)は空欄("")にする。
    日付ごとにDBからカーブを1回だけ読み、全脚を評価する。
    real_time_range: RealTime!B2:D45(省略可)。基準日が今日(空欄を含む)で今日が営業日なら、
        今日を最新日として加え、DBの引け値ではなくRealTimeのライブのレートで計算する
        (pricingシートのFairRateと同じカーブ)。日付の並びを揃えるため、日付軸だけを求める
        呼び出しにも同じ範囲を渡すこと。並び順(昇順)の変更はExcel側で行う。
    """
    specs = _specs_from_range(conventions)
    active = [s for s in specs if s is not None]
    today = udf_common._today()
    base = udf_common._to_date(base_date) or today
    with udf_common._historical_store() as store:
        curves = store
        if (
            real_time_range is not None and base == today
            and CALENDAR.isBusinessDay(ql.Date.from_date(today))
        ):
            curves = _WithRealTimeLatest(store, real_time_range, today)
        series = historical_multi_rate_series(
            active, mode, base_date=base, from_date=udf_common._to_date(from_date),
            to_date=udf_common._to_date(to_date), fixings=db_fixings(store) if active else None,
            curves=curves,
        )
    rows = [["Date", *_LEG_LABELS]]
    for as_of_date, legs in series:
        results = iter(legs)
        row = [as_of_date]
        for spec in specs + [None] * (_MAX_LEGS - len(specs)):
            leg = next(results) if spec is not None else None
            row.append(leg.par_rate if leg is not None else "")
        rows.append(row)
    return rows
