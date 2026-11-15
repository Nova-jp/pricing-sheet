"""
LiveScreen.xlsm(スワップとボラのリアルタイムの画面)のUDF。

■ ブックの使い方(docs/live_screen.md)
  - 別のExcelのインスタンスで開き、計算方法を「自動」にする。レートはLSEGアドインの RtGet で読み、値が届くたびに
    Excelが再計算する(VBA・タイマーは使わない)。PricingSheet等(手動計算)とはExcelのプロセスが別なので、
    計算方法が互いに影響しない。
  - スプレッド・フライ・合計・年率への直し方はExcelの数式。UDFはQuantLibが要る計算(日付・パーレート・
    プレミアム→σ√T)だけ。
  - 評価日は常に今日(ライブのカーブ)。カーブは全UDFで共有する(engine/common/market.py。レートが同じなら
    ブートストラップは1回だけ)。

■ UDF
  - SwapBusinessDays  : 前後の営業日(Datesシートの行)
  - SwapDateGrid      : 取引日×テナーの起算日・満期日・長さ・前の営業日との長さの差(Datesシート)
  - SwapLengthAdjust  : テナーごとの日数の調整幅(Swapシート。定義は engine/live_screen/day_length.py)
  - SwaptionStdevGrid : 満期×原資産のプレミアム → σ√T(Volシート。定義は engine/live_screen/vol.py)
  - SwaptionExpiryDays: 満期ごとの満期日・残りの営業日数・暦日数(Volシート)
"""

from typing import List

import xlwings as xw

from engine.common import udf_common
from engine.common.udf_common import _is_blank
from engine.live_screen import day_length, vol

DATE_ITEMS = ("Spot", "Maturity", "Days", "DaysChg")


def _int(value, name: str) -> int:
    if _is_blank(value):
        raise ValueError(f"{name}を入れてください")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name}は整数で入れてください: {value!r}") from None
    if number != int(number):
        raise ValueError(f"{name}は整数で入れてください: {value!r}")
    return int(number)


def _labels(values) -> List[str]:
    """縦の範囲のラベル(空欄は "" のまま残し、行の位置を保つ)。"""
    return ["" if _is_blank(v) else str(v).strip() for v in (values or [])]


@xw.func
def SwapBusinessDays(center_date, before, after) -> List[list]:
    """center_date(空欄=今日。休日ならその次の営業日)の前 before・後 after 営業日を縦に並べる(ql.Japan())。"""
    days = day_length.business_days_around(
        udf_common._resolve_valuation_date(center_date), _int(before, "前の日数"), _int(after, "後の日数"),
    )
    return [[d] for d in days]


@xw.func
@xw.arg("trade_dates", ndim=1)
@xw.arg("tenors", ndim=1)
def SwapDateGrid(trade_dates, tenors, item) -> List[list]:
    """
    取引日(行)×テナー(列)のスポットのスワップの日付(起算日=T+2、標準の規約)。
    item: "Spot"=起算日 / "Maturity"=満期日 / "Days"=長さ(満期日−起算日、暦日) /
          "DaysChg"=長さ − 前の営業日の長さ(暦日)。
    取引日・テナーが空欄の行・列は空欄、解釈できないテナー(O/N・会合スワップ等)の列も空欄。
    """
    kind = str(item).strip()
    if kind not in DATE_ITEMS:
        raise ValueError(f"item は {', '.join(DATE_ITEMS)} のどれかです: {item!r}")
    labels = _labels(tenors)
    out = []
    for value in trade_dates or []:
        d = udf_common._to_date(value)
        row = []
        for label in labels:
            cell = ""
            if d is not None and label:
                try:
                    today = day_length.swap_dates_on(d, label)
                    if kind == "Spot":
                        cell = today.spot
                    elif kind == "Maturity":
                        cell = today.maturity
                    elif kind == "Days":
                        cell = today.days
                    else:
                        prev = day_length.swap_dates_on(day_length.previous_business_day(d), label)
                        cell = today.days - prev.days
                except ValueError:
                    cell = ""
            row.append(cell)
        out.append(row)
    return out or [[""]]


@xw.func
@xw.arg("rates", ndim=2)
@xw.arg("tenors", ndim=1)
def SwapLengthAdjust(rates, tenors) -> List[list]:
    """
    テナーごとの [Days(今日の長さ), Prev days(前の営業日の長さ), Diff(差、暦日), Adj(bp), Message]。
    Adj = 今のカーブで、今日の起算日から今日の満期日までのパーレート − 前の営業日と同じ長さにしたパーレート
    (今日のほうが短く、カーブが右上がりなら負)。長さが同じなら0。
    rates: カーブに使う [Flag, Ticker, Mid] の範囲(Flag 0 の行はカーブから外す。Main シートと同じ)。
    行の誤り(O/N・会合スワップ・解釈できないテナー)はその行を空欄にし、Message に理由を出す。
    """
    market = udf_common._market(rates, None).base
    out = []
    for label in _labels(tenors):
        if not label:
            out.append(["", "", "", "", ""])
            continue
        try:
            a = day_length.length_adjustment(market, label)
            out.append([a.days, a.prev_days, a.days - a.prev_days, a.adjustment, ""])
        except (ValueError, RuntimeError) as exc:
            out.append(["", "", "", "", str(exc)])
    return out or [[""]]


@xw.func
@xw.arg("rates", ndim=2)
@xw.arg("expiries", ndim=1)
@xw.arg("tenors", ndim=1)
@xw.arg("premiums", ndim=2)
def SwaptionStdevGrid(rates, expiries, tenors, premiums) -> List[list]:
    """
    満期(行)×原資産(列)のATMストラドルのフォワード・プレミアム(bp、Traditionの PRIMACT_1)→
    満期までの標準偏差 σ√T(bp)。カーブは rates([Flag, Ticker, Mid])の今のレート。
    プレミアムが無い・0以下のセルは空欄。年率・1日あたりへの直し方はExcelで(engine/live_screen/vol.py)。
    """
    cs = udf_common._market(rates, None)
    grid = vol.stdev_grid(
        cs.base.curve, cs.valuation_date, udf_common._now(),
        [e for e in _labels(expiries)], [t for t in _labels(tenors)], premiums or [],
    )
    return [["" if v is None else v for v in row] for row in grid] or [[""]]


@xw.func
@xw.arg("expiries", ndim=1)
@xw.arg("premiums", ndim=2)
def SwaptionExpiryDays(expiries, premiums=None) -> List[list]:
    """
    満期ごとの [Expiry(満期日), Bus days(満期の15時までの残りの営業日数、日中の残りも減らす), Cal days(満期日−今日)]。
    premiums: プレミアムの範囲。値は使わず、プレミアムが届くたびに残りの営業日数を数え直すために渡す。
    """
    today = udf_common._today()
    now = udf_common._now()
    out = []
    for e in _labels(expiries):
        if not e:
            out.append(["", "", ""])
            continue
        d = vol.expiry_days(today, now, e)
        out.append([d.expiry, d.business_days, d.calendar_days])
    return out or [[""]]
