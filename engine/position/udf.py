"""
ポジション・リスク管理のブック(PositionSheet.xlsm)のUDF。

■ 設計方針(他のブックと同じ)
  - UDFにするのは、QuantLibが要る日中の取引・現物(Raw_JGB)・現物オプションのデルタ・ガンマと、DBの引け値の
    読み出しだけ。
    会社のリスクの抽出・今のデルタ(SOD + ガンマ × Move + 今日の取引)・合計はExcelの数式(四則演算)で計算する。
  - 計算方法は「手動」+ Shift+F9。UDFは非volatile。TODAY()は引数に使わない(評価日の空欄=今日)。
  - 配列はそのまま返し、Excelの動的配列のスピルに任せる(@xw.ret(expand=...)は使わない)。

■ 単位・符号
  想定元本・額面: 百万円。デルタ: 百万円/bp(金利が1bp上がったときのPVの変化)。ガンマ: 百万円/bp²。

■ 引数(共通)
  pillars: グリッドのピラーの縦の並び(短い順)。結果の行はこの並び。
  real_time_range: フラグ/Ticker/Value の範囲(Positionシートのピラーの行)。評価日の空欄=今日はライブ
    (他のブックと同じ udf_common._market)。
"""

from typing import List, Optional, Tuple

import QuantLib as ql
import xlwings as xw

from engine.common import swaption, udf_common
from engine.common.conventions import CALENDAR
from engine.common.curve import spot_date_for
from engine.common.history import normalize_label
from engine.common.historical_store import HistoricalStore
from engine.common.udf_common import _is_blank, _to_date
from engine.position import bond_options, bonds, intraday, trades, vega


def _labels(values) -> Tuple[str, ...]:
    return tuple(str(v).strip() for v in (values or []) if not _is_blank(v))


def _number(value, name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name}が数値ではありません: {value!r}") from None


def _optional_number(value, name: str) -> Optional[float]:
    return None if _is_blank(value) else _number(value, name)


def _rows(table) -> List[list]:
    return [list(r) if r is not None else [] for r in (table or [])]


def _deltas_row(deltas: dict, pillars: Tuple[str, ...]) -> list:
    """{カーブのラベル(小文字): デルタ} → ピラーの並び(カーブに使っていないピラーは0)。"""
    return [float(deltas.get(p.lower(), 0.0)) for p in pillars]


def _blank_row(n: int) -> list:
    return [""] * n


def _date_or_period(value):
    """日付(datetime・シリアル値)は日付に、それ以外(「5Y」等)は文字列のまま。"""
    if hasattr(value, "year") or (isinstance(value, (int, float)) and not isinstance(value, bool)):
        return _to_date(value)
    return str(value).strip()


def _db_key() -> Optional[int]:
    store = HistoricalStore()
    return store.db_path.stat().st_mtime_ns if store.exists() else None


@xw.func
def PosBaseDate(override=None):
    """会社のリスクの基準日(引け値を読む日付)。overrideに日付があればそれ、空欄ならDBのカーブがある
    今日より前の直近の日付(朝バッチが置いた昨日までの引け)。TODAY()を使わないため(volatileになる)、
    空欄の解決をUDFで行う(BondOptionのValuationDateと同じ)。"""
    d = _to_date(override)
    if d is not None:
        return d
    today = udf_common._today()
    with udf_common._historical_store() as store:
        dates = [x for x in store.dates() if x < today]
    if not dates:
        raise ValueError("DBに今日より前のカーブがありません(朝バッチを流してください)")
    return dates[-1]


@xw.func
@xw.arg("pillars", ndim=1)
def PosCloseRates(pillars, as_of_date) -> List[list]:
    """DBのその日の引け値(%、ピラーの並びの縦1列。O/NはTONA実績、値が無いピラーは空欄)。
    PositionシートのMove(今のレート − 会社のリスクの基準日の引け)の基準に使う。"""
    d = _to_date(as_of_date)
    if d is None:
        raise ValueError("日付を入れてください(会社のリスクの基準日)")
    with udf_common._historical_store() as store:
        quotes = {normalize_label(k): v for k, v in store.quotes_on(d).items()}
        fixing = store.fixings().get(d)
    if not quotes:
        raise ValueError(f"{d}の引け値がDBにありません")
    if fixing is not None:
        quotes["O/N"] = fixing
    return [[quotes.get(normalize_label(p), "")] for p in _labels(pillars)]



@xw.func
@xw.arg("expiries", ndim=1)
@xw.arg("tails", ndim=1)
def PosCloseVols(expiries, tails, as_of_date) -> List[list]:
    """DBのその日のスワップションATMノーマルvol(bp/年、営業日ベース)。行=満期、列=原資産(値が無ければ空欄)。
    Positionシートのボラの動き(今のvol − 会社のリスクの基準日の引け)の基準に使う。"""
    d = _to_date(as_of_date)
    if d is None:
        raise ValueError("日付を入れてください(会社のリスクの基準日)")
    with udf_common._historical_store() as store:
        vols = {normalize_label(k): v for k, v in store.vol_quotes().get(d, {}).items()}
    if not vols:
        raise ValueError(f"{d}のvolがDBにありません")
    return [[vols.get(normalize_label(f"{e}x{t}"), "") for t in _labels(tails)] for e in _labels(expiries)]

@xw.func
@xw.arg("expiries", ndim=1)
def PosVolCalendarFactor(expiries, trade_date=None) -> List[list]:
    """営業日ベースのvol(DB・フィード)→ 暦日ベースのvol(bp/年)の掛け目の縦1列(満期の並び)。
    = √(取引日から満期日までの営業日数/252) × √(365/暦日数)。会社のベガは暦日の年率のvolで微分した値なので、
    Positionのボラの動きは暦日ベースで見る(ユーザー指示、2026-11-10)。trade_date: 空欄なら今日(ライブのvol)。
    引けのvolには基準日を渡す。"""
    d = _to_date(trade_date) or udf_common._today()
    return [[swaption.calendar_factor(d, e)] for e in _labels(expiries)]


@xw.func
def PosBusinessDays(start_date, end_date=None) -> int:
    """start_date から end_date(空欄なら今日)までの営業日数(ql.Japan。基準日の翌営業日から今日までを数える)。
    現物オプションのセータ(1営業日)のPLに掛ける。"""
    a = _to_date(start_date)
    if a is None:
        raise ValueError("日付を入れてください(基準日)")
    b = _to_date(end_date) or udf_common._today()
    return CALENDAR.businessDaysBetween(ql.Date.from_date(a), ql.Date.from_date(b), False, True)


@xw.func
@xw.arg("datasets", ndim=1)
@xw.arg("expiries", ndim=1)
@xw.arg("tails", ndim=1)
@xw.arg("vegas", ndim=1)
@xw.arg("expiry_labels", ndim=1)
@xw.arg("tail_labels", ndim=1)
def PosVegaGrid(datasets, expiries, tails, vegas, dataset, expiry_labels, tail_labels) -> List[list]:
    """会社のベガ(Raw_IRVOLの列: dataset・満期・原資産・ベガ)を、行=expiry_labels × 列=tail_labels の格子に
    振り分けた合計(engine/position/vega.py。格子に無い満期・原資産は前後の点に年数で線形に分け、最も短い点より
    短い満期は最も短い点に全部。単位は生シートのまま(円)。どの行も載らないマスは空欄)。"""
    return vega.company_grid(list(datasets or []), list(expiries or []), list(tails or []), list(vegas or []),
                             dataset, _labels(expiry_labels), _labels(tail_labels))


# ---------------------------------------------------------------------------
# 日中の取引(Positionシートの下段。OIS・現物)のデルタ・ガンマ(engine/position/intraday.py)
# ---------------------------------------------------------------------------

INTRADAY_HEADER = ["Rate", "PV", "DV01", "Gamma", "Pillar", "Trade move", "Message"]
GRID_HEADER = ["Delta Swap", "Delta Bond", "Gamma Swap", "Gamma Bond"]
_STATUS_FILTERS = {"DONE": (intraday.DONE,), "WHATIF": (intraday.WHAT_IF,), "ALL": intraday.STATUSES}

# 直近の計算結果(PosIntradayRiskとPosIntradayGridが同じ入力で1回だけ計算するため)。キーは取引の表・
# カーブのスナップショット・DBの更新時刻
_intraday_cache: dict = {}


def _intraday_input(row) -> Optional[intraday.IntradayTrade]:
    """[Status, Product, Start, Tenor/End, Code, Notional, Side, Rate] → IntradayTrade(Productが空の行はNone)。
    OIS: Start(空欄=スポット)・Tenor/End・Notional・Side(Pay/Rec)・Rate(固定金利%、空欄=パー)。
    Bond: Code・Notional(額面)・Side(Buy/Sell)・Rate(単利%、空欄=DBの引け値)。"""
    row = row + [None] * (8 - len(row))
    status, product, start, tenor, code, notional, side, rate = row[:8]
    if _is_blank(product):
        return None
    product = intraday.parse_product(product)
    status = intraday.parse_status(status)
    if product == intraday.OIS:
        if _is_blank(tenor):
            raise ValueError("Tenor/Endが空欄です")
        swap = trades.Trade(
            None if _is_blank(start) else _date_or_period(start), _date_or_period(tenor),
            _number(notional, "Notional"), side, _optional_number(rate, "Rate"),
        )
        trades.parse_pay_rec(side)
        return intraday.IntradayTrade(status, product, swap=swap)
    if _is_blank(code):
        raise ValueError("現物のCodeが空欄です")
    face = abs(_number(notional, "Notional")) * intraday.bond_sign(side)
    return intraday.IntradayTrade(
        status, product, bond=bonds.BondPosition(str(code), face, _optional_number(rate, "Rate")),
    )


def _freeze(rows) -> tuple:
    return tuple(tuple("" if v is None else v for v in r) for r in rows)


def _intraday(trade_table, real_time_range, valuation_date) -> List[object]:
    """取引の表の行ごとの IntradayRisk / 例外 / None(入力の行と同じ並び)。同じ入力なら前の結果を返す。"""
    rows = _rows(trade_table)
    curves = udf_common._market(real_time_range, valuation_date)
    key = (_freeze(rows), curves.snapshot, _db_key())
    if key in _intraday_cache:
        return _intraday_cache[key]
    parsed: List[Optional[intraday.IntradayTrade]] = []
    errors = {}
    for n, row in enumerate(rows):
        try:
            parsed.append(_intraday_input(row))
        except ValueError as e:
            parsed.append(None)
            errors[n] = e
    if any(t is not None and t.product == intraday.BOND for t in parsed):
        with udf_common._historical_store() as store:
            results = intraday.intraday_risks(curves, store, parsed)
    else:
        results = intraday.intraday_risks(curves, None, parsed)
    results = [errors.get(n, r) for n, r in enumerate(results)]
    _intraday_cache.clear()  # 直近1件だけ保持
    _intraday_cache[key] = results
    return results


@xw.func
@xw.arg("trade_table", ndim=2)
@xw.arg("pillars", ndim=1)
@xw.arg("real_time_range", ndim=2)
def PosIntradayRisk(trade_table, pillars, real_time_range, valuation_date=None) -> List[list]:
    """
    日中の取引の行ごとの要約(engine/position/intraday.py)。
    trade_table: [Status, Product, Start, Tenor/End, Code, Notional(百万円), Side, Rate(%)] の行(見出しなし)。
      Status: Done(約定済み)/ What-if(検討中)。Product: OIS / Bond。Productが空欄の行は計算しない。
    返り値: 見出し行 [Rate, PV, DV01, Gamma, Pillar, Trade move, Message] + 入力と同じ行数。
      Rate: 計算に使った金利(OISは固定金利、現物は単利)。PV: OISのPV(百万円、現物は空欄)。
      DV01・Gamma: pillarsのピラーのデルタ・ガンマの合計(百万円/bp、百万円/bp²)。
      Pillar・Trade move: 現物を載せたピラーと、約定の複利 − DBの引けの複利(bp。Rate空欄なら0、OISは空欄)。
      現物のPLはシートで デルタ × (BondのMove − Trade move) + ½ × ガンマ × (…)² にする。
    """
    labels = _labels(pillars)
    out = [list(INTRADAY_HEADER)]
    for r in _intraday(trade_table, real_time_range, valuation_date):
        if isinstance(r, Exception):
            out.append(_blank_row(len(INTRADAY_HEADER) - 1) + [str(r)])
        elif r is None:
            out.append(_blank_row(len(INTRADAY_HEADER)))
        else:
            out.append([
                r.rate, "" if r.pv is None else r.pv,
                sum(_deltas_row(r.deltas, labels)), sum(_deltas_row(r.gammas, labels)),
                r.pillar or "", "" if r.trade_move is None else r.trade_move,
                "" if r.product == intraday.OIS or r.trade_move is not None
                else "約定の利回りとの差を出せません(DBに引け値がありません)",
            ])
    return out


@xw.func
@xw.arg("trade_table", ndim=2)
@xw.arg("pillars", ndim=1)
@xw.arg("real_time_range", ndim=2)
def PosIntradayGrid(trade_table, pillars, real_time_range, valuation_date=None, status="Done") -> List[list]:
    """
    日中の取引のピラーごとの合計(PositionシートのToday の列)。引数はPosIntradayRiskと同じ。
    status: Done(既定)/ What-if / All のどれの取引を足すか。エラーの行は足さない(PosIntradayRiskのMessageで確かめる)。
    返り値: 見出し行 [Delta Swap, Delta Bond, Gamma Swap, Gamma Bond] + pillarsの並びの行。
      Swap = OIS、Bond = 現物。カーブに使っていないピラーは0。
    """
    key = str(status or "Done").strip().upper().replace("-", "").replace(" ", "")
    if key not in _STATUS_FILTERS:
        raise ValueError(f"statusは Done / What-if / All のどれかです: {status!r}")
    wanted = _STATUS_FILTERS[key]
    labels = _labels(pillars)
    sums = {p: [0.0] * len(labels) for p in ("delta_" + intraday.OIS, "delta_" + intraday.BOND,
                                              "gamma_" + intraday.OIS, "gamma_" + intraday.BOND)}
    for r in _intraday(trade_table, real_time_range, valuation_date):
        if not isinstance(r, intraday.IntradayRisk) or r.status not in wanted:
            continue
        for measure, values in (("delta_", r.deltas), ("gamma_", r.gammas)):
            acc = sums[measure + r.product]
            for i, v in enumerate(_deltas_row(values, labels)):
                acc[i] += v
    columns = [sums["delta_" + intraday.OIS], sums["delta_" + intraday.BOND],
               sums["gamma_" + intraday.OIS], sums["gamma_" + intraday.BOND]]
    return [list(GRID_HEADER)] + [list(row) for row in zip(*columns)]


# ---------------------------------------------------------------------------
# 現物のポジション(会社のファイルのRaw_JGB。engine/position/bonds.py)
# ---------------------------------------------------------------------------

JGB_HEADER = ["Code", "Coupon", "Maturity", "Yield", "Compound", "MD", "Convexity", "Pillar", "Delta", "Gamma",
              "Message"]
JGB_GRID_HEADER = ["Delta", "Gamma"]


def _column(values) -> list:
    if values is None:
        return []
    return list(values) if isinstance(values, (list, tuple)) else [values]


def _jgb_rows(ids, maturities, faces, yields, pillars, base_date) -> List[object]:
    """Raw_JGBの行ごとの (BondRisk, ピラー) / 例外 / None(IDか額面が空欄・額面0の行)。"""
    d = _base_date(base_date)
    ids, maturities, faces, yields = (_column(v) for v in (ids, maturities, faces, yields))
    n = len(ids)
    for name, col in (("Maturity", maturities), ("Nominal", faces), ("Yield", yields)):
        if col and len(col) != n:
            raise ValueError(f"Instrument IDと{name}の行数が違います({n}行と{len(col)}行)")
    labels = _labels(pillars)

    def compute():
        pillar_list = bonds.pillar_dates(labels, spot_date_for(d))
        out: List[object] = []
        with udf_common._historical_store() as store:
            for i in range(n):
                face = faces[i] if faces else None
                if _is_blank(ids[i]) or _is_blank(face) or face == 0:
                    out.append(None)
                    continue
                try:
                    code = bonds.jgb_code(ids[i])
                    y = _optional_number(yields[i], "Yield") if yields else None
                    r = bonds.bond_risk(store, bonds.BondPosition(code, _number(face, "Nominal"), y), d)
                    m = _to_date(maturities[i]) if maturities and not _is_blank(maturities[i]) else None
                    if m is not None and m != r.maturity:
                        raise ValueError(f"{code}の償還日がDB({r.maturity})と違います: {m}")
                    out.append((r, bonds.bucket(r.maturity, pillar_list)))
                except Exception as e:  # 行ごとにメッセージを出して他の行は続ける
                    out.append(e)
        return out

    return _cached("jgb", (_freeze([ids, maturities, faces, yields]), labels, d), compute)


@xw.func
@xw.arg("ids", ndim=1)
@xw.arg("maturities", ndim=1)
@xw.arg("faces", ndim=1)
@xw.arg("yields", ndim=1)
@xw.arg("pillars", ndim=1)
def PosJgbRisk(ids, maturities, faces, yields, pillars, base_date) -> List[list]:
    """
    会社のファイルの現物(Raw_JGB)の行ごとのデルタ・ガンマ(修正デュレーション・コンベクシティ。bonds.py)。
    ids: Instrument ID(0-02-回号4桁-000種別)。maturities: Maturity(DBの償還日と照合、空欄なら照合しない)。
    faces: 額面(百万円、Yesterday)。yields: 単利(%、Closing Mark S Yld。空欄ならDBの基準日の引け値)。
    pillars: グリッドのピラー(満期が入る区間の長いほうに載せる)。base_date: 会社のリスクの基準日。
    返り値: 見出し行 [Code, Coupon, Maturity, Yield, Compound, MD, Convexity, Pillar, Delta, Gamma, Message] +
      入力と同じ行数(IDか額面が空欄・額面0の行は空欄)。Delta: 百万円/bp、Gamma: 百万円/bp²。
    """
    out = [list(JGB_HEADER)]
    for row in _jgb_rows(ids, maturities, faces, yields, pillars, base_date):
        if row is None:
            out.append(_blank_row(len(JGB_HEADER)))
        elif isinstance(row, Exception):
            out.append(_blank_row(len(JGB_HEADER) - 1) + [str(row)])
        else:
            r, label = row
            out.append([r.code, r.coupon, r.maturity, r.simple_yield, r.compound_yield, r.modified_duration,
                        r.convexity, label, r.delta, r.gamma, ""])
    return out


@xw.func
@xw.arg("ids", ndim=1)
@xw.arg("maturities", ndim=1)
@xw.arg("faces", ndim=1)
@xw.arg("yields", ndim=1)
@xw.arg("pillars", ndim=1)
def PosJgbGrid(ids, maturities, faces, yields, pillars, base_date) -> List[list]:
    """Raw_JGBのデルタ・ガンマのピラーごとの合計。見出し行 [Delta, Gamma] + pillarsの並びの行。
    引数はPosJgbRiskと同じ。エラーの行は足さない(PosJgbRiskのMessageで確かめる)。"""
    labels = _labels(pillars)
    deltas: dict = {}
    gammas: dict = {}
    for row in _jgb_rows(ids, maturities, faces, yields, pillars, base_date):
        if isinstance(row, tuple):
            r, label = row
            key = label.lower()
            deltas[key] = deltas.get(key, 0.0) + r.delta
            gammas[key] = gammas.get(key, 0.0) + r.gamma
    return [list(JGB_GRID_HEADER)] + [list(x) for x in zip(_deltas_row(deltas, labels), _deltas_row(gammas, labels))]


# ---------------------------------------------------------------------------
# 現物オプションの約定(BondOptシート。engine/position/bond_options.py)
# ---------------------------------------------------------------------------

BOND_RISK_HEADER = ["Delta", "Gamma", "Theta"]

# Vol ratio用のスワップレート(DBの更新時刻ごと)と、表UDFの直近の結果(入力とDBの更新時刻がキー)
_bond_rates: Optional[bond_options.hv.CurveRates] = None
_bond_cache: dict = {}


def _base_date(value):
    d = _to_date(value)
    if d is None:
        raise ValueError("基準日を入れてください(SetupのBase date)")
    return d


def _cached(name: str, key: tuple, compute):
    full = (name, key, _db_key())
    if full not in _bond_cache:
        for k in [k for k in _bond_cache if k[0] == name]:
            del _bond_cache[k]  # UDFごとに直近1件だけ保持
        _bond_cache[full] = compute()
    return _bond_cache[full]


def _vol_rows(store, d) -> list:
    return [[label, v] for label, v in store.vol_quotes().get(d, {}).items()]


@xw.func
@xw.arg("codes", ndim=1)
@xw.arg("expiries", ndim=1)
@xw.arg("settles", ndim=1)
def PosBondOptMarket(codes, expiries, settles, base_date, repo, window) -> List[list]:
    """現物オプションの行ごとの [Coupon, Maturity, Close yield, Close date, Spot price, Fwd clean, Fwd MD, Fwd DD,
    Swaption vol, Vol ratio, T, Message](基準日の引け。BondOptionの OptionMarketTable・VolRatioTable と同じ計算)。
    codes・expiries・settles: 約定の銘柄・行使日・行使後の受渡日(同じ行数の列)。repo: レポ(%)。window: HVの窓。"""
    d = _base_date(base_date)
    r = _optional_number(repo, "Repo")  # 空欄なら約定の行ごとにmessage(約定が無ければ何も出ない)

    def compute():
        global _bond_rates
        with udf_common._historical_store() as store:
            key = (str(store.db_path), store.db_path.stat().st_mtime_ns)
            if _bond_rates is None or _bond_rates.db_key != key:
                _bond_rates = bond_options.hv.CurveRates(store, key)
            _bond_rates.store = store
            return bond_options.market_rows(codes, expiries, settles, d, r, window, store, _bond_rates,
                                            _vol_rows(store, d))

    return _cached("market", (_freeze([codes, expiries, settles]), d, r, window), compute)


@xw.func
@xw.arg("types", ndim=1)
@xw.arg("forwards", ndim=1)
@xw.arg("strikes", ndim=1)
@xw.arg("price_vols", ndim=1)
@xw.arg("terms", ndim=1)
def PosBondOptGreeks(types, forwards, strikes, price_vols, terms) -> List[list]:
    """現物オプションの行ごとの [Premium, Delta, Gamma, Vega, Theta, Message](額面100円あたり、Black式・割引なし)。
    Vega: 価格ボラ1%あたり。Theta: 1営業日。引数は同じ行数の列(Call/Put・先渡clean・ストライク・価格ボラ(%)・T)。"""
    return bond_options.greeks_rows(types, forwards, strikes, price_vols, terms)


@xw.func
@xw.arg("codes", ndim=1)
@xw.arg("faces", ndim=1)
@xw.arg("price_gammas", ndim=1)
@xw.arg("pillars", ndim=1)
@xw.arg("real_time_range", ndim=2)
@xw.arg("thetas", ndim=1)
def PosBondRiskGrid(codes, faces, price_gammas, pillars, real_time_range, base_date, thetas=None) -> List[list]:
    """銘柄ごとのデルタ換算の額面(百万円)・価格のガンマ × 額面・セータ(百万円/日)を、基準日のDBのカーブで
    ピラーに分けた見出し行 [Delta, Gamma, Theta] + pillarsの並びの行(百万円/bp、百万円/bp²、百万円/営業日)。
    価格のガンマ・セータが空欄なら0(現物)。セータは原資産の銘柄のピラーに載せる(デルタと同じ)。
    real_time_range: カーブに使うピラーのフラグの範囲(Positionシートと同じ)。DBに無い銘柄はエラーにする。"""
    d = _base_date(base_date)
    labels = _labels(pillars)
    theta_column = list(thetas or [])

    def compute():
        curves = udf_common._market(real_time_range, d)
        with udf_common._historical_store() as store:
            r = bond_options.pillar_risk(curves, store, codes, faces, price_gammas, theta_column)
        if r.errors:
            raise ValueError(" / ".join(r.errors))
        return [list(BOND_RISK_HEADER)] + [list(row) for row in zip(
            _deltas_row(r.deltas, labels), _deltas_row(r.gammas, labels), _deltas_row(r.thetas, labels))]

    return _cached("grid", (_freeze([codes, faces, price_gammas, theta_column]), labels, _freeze(real_time_range), d),
                   compute)
