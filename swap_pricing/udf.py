"""
Excelのセル関数(UDF)として直接呼び出せる計算関数群。xlwingsのExcelアドイン経由で
`=FairRate(...)`のように、ネイティブのExcel関数と全く同じ感覚でどのセルにも置ける。

■ 設計方針
  - QuantLibが必要な計算(パーレート・PV・delta・notional逆算・リスクの合算・ヒストリカル)
    だけをここに置く。カーブ(スプレッド)やフライ、合計といった単純な四則演算はExcel自身の
    数式に任せる(このファイルには書かない)。
  - 計算方法はブックの計算方法を「手動」にし、F9(再計算)で都度実行する想定。
    UDFは非volatileのままでよい。VBAコードは一切不要。
  - 評価日は Mainシートの評価日セル(名前「評価日」)を各UDFに渡す。空欄なら今日。
    TODAY()は使わない(volatileなのでF9のたびに全UDFが再計算されてしまうため)。
  - 評価日が今日なら RealTimeシートのライブのレート、過去日ならヒストリカルDBのその日の
    レートでカーブを組む(判定は _market だけ。詳細は swap_pricing/market.py)。どちらも
    RealTimeシートの フラグ/Ticker/Value 範囲(例: RealTime!B2:D45)でテナーを選ぶ。
    B列の採否フラグ: 1(または空欄)=カーブ構築に使う、0=使わない。
  - カーブ(基準カーブと+1bpしたカーブ)は market.curve_set で全行・全UDFが共有する。
    同じレート・評価日なら、ブートストラップはその最初の1回だけ。
  - 過去起算スワップ用のTONA実績フィキシングは引数(fixing_range、省略可)で受け取る。
    historical_data.xlsxのFixingシート(日付/TONAの2列スピル)を
    `[historical_data.xlsx]Fixing!$A$5#` のように参照する想定。評価日より前の日だけを使う。
  - ヒストリカル系UDF(HistoricalRate(s), HistoricalOutright)はブートストラップをせず、
    朝バッチ(python -m swap_pricing.curve_batch)が作った日次カーブDB
    (data/historical.db)を読むだけ。TONA実績もDBに取り込み済みのものを使う。
  - 配列を返すUDF(RiskGrid, HistoricalOutright, HistoricalRate, HistoricalRates)は、
    Excelの動的配列のスピルにそのまま任せる。xlwingsの @xw.ret(expand="table") は使わない
    (計算後に非同期でセル範囲を旧式の配列数式に書き換えるため、動的配列の
    スピル参照(B1#等)や数式の@付与と衝突する。実際にHistoricalシートが
    旧式配列化して先頭1セルしか表示されなくなっていた)。

■ 使い方(Windows実機でのセットアップ)
  1. pip install xlwings
  2. xlwings addin install (Excelアドインを1度だけインストール)
  3. ブックの _xlwings.conf シートの "UDF Modules" に "swap_pricing.udf" を設定
     (または xlwings のRibbonから対象ブックにこのモジュールを紐付ける)
  4. ブックの計算方法を手動に設定(このモジュールでは行わない、Excel側の設定)
"""

import datetime
from typing import Dict, List, Optional

import QuantLib as ql
import xlwings as xw

from swap_pricing.conventions import CALENDAR, forward_start_date, imm_code_to_date, is_imm_code, is_tenor
from swap_pricing.curve import BootstrapResult, ON_KEYS
from swap_pricing.curve_store import CurveStore
from swap_pricing.historical_pricer import (
    ROLLING,
    SwapSpec,
    historical_multi_rate_series,
    historical_swap_rate_series,
)
from swap_pricing.market import CurveSet, curve_set, db_snapshot, realtime_snapshot
from swap_pricing.meeting_curve import MeetingPeriod, build_meeting_curve
from swap_pricing.risk import (
    SwapParams,
    annuity_delta,
    bucketed_delta,
    parallel_delta,
    price,
    resolve_params,
    solve_notional_for_target_delta,
)


@xw.func
def Ping(value=1) -> float:
    """xlwings配線の疎通確認用(QuantLib計算を伴わない)。

    Import Functions後にこの関数だけが失敗する場合、原因はUDFの配線
    (xlwingsアドイン/ブックのVBAプロジェクト)側にある。
    """
    return float(value) * 2


_EXCEL_EPOCH = datetime.date(1899, 12, 30)  # Excelのシリアル値1900-01-01=1、1900うるう年バグ込み


def _to_date(value) -> Optional[datetime.date]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    # Excelがセル参照でなく TODAY() や日付書式セルを引数で渡すと、xlwings経由で
    # datetimeではなくシリアル値(数値)で届くことがある。その場合はここで日付に変換する。
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _EXCEL_EPOCH + datetime.timedelta(days=int(value))
    # start/end列にはIMMコード(M27等)も入力できる。ロール(STD/IMM等)はroll conv列の
    # 指定にそのまま従う(両方IMMコードなら既定でIMMになるのはExcel側のP列数式の役割)。
    if is_imm_code(value):
        return imm_code_to_date(value)
    raise ValueError(
        f"日付として解釈できません: {value!r}(IMMコードは月記号H/M/U/Z+2桁の年、例: M27。"
        "startにはテナー(例: 10y)も入力できる)"
    )


def _start_date(value, spot: datetime.date) -> Optional[datetime.date]:
    """start列の値 → 起算日。日付・IMMコードに加え、テナー(10y等)ならスポット日+tenor
    (フォワードスタート)。"""
    if is_tenor(value):
        return forward_start_date(spot, value)
    return _to_date(value)


def _is_blank(value) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def _flag_excludes(flag) -> bool:
    """RealTime B列の採否フラグ判定。空欄・None・非数値は「使う」、明示的な 0 のみ除外。"""
    if flag is None or flag == "":
        return False
    try:
        return float(flag) == 0.0
    except (TypeError, ValueError):
        return False


def _realtime_rows(real_time_range):
    """RealTimeレンジ → [(Ticker, Value, 使う?)]。3列=[フラグ, Ticker, Value]、
    2列=[Ticker, Value](後方互換。フラグなし=全行使う)。Tickerが空の行は除く。"""
    rows = []
    for row in real_time_range or []:
        if row is None:
            continue
        flag, ticker, value = (row[0], row[1], row[2]) if len(row) >= 3 else (None, row[0], row[1])
        if _is_blank(ticker):
            continue
        rows.append((str(ticker).strip(), value, not _flag_excludes(flag)))
    return rows


def _rates_from_range(real_time_range) -> Dict[str, float]:
    """RealTimeレンジ → ブートストラップ入力辞書(フラグ≠0で値のあるテナー、ラベルは小文字)。
    「どのスポットレートをカーブに使うか」を決めるのはこの関数(過去日は _market で同じフラグを使う)。"""
    return {
        ticker.lower(): float(value)
        for ticker, value, use in _realtime_rows(real_time_range)
        if use and not _is_blank(value)
    }


def _today() -> datetime.date:
    """今日の日付(テストで差し替えられるように1か所にまとめる)。"""
    return datetime.date.today()


def _resolve_valuation_date(valuation_date) -> datetime.date:
    d = _to_date(valuation_date)
    return d if d is not None else _today()


def _market(real_time_range, valuation_date) -> CurveSet:
    """評価日のカーブ一式。今日ならRealTime、過去日ならヒストリカルDB(同じフラグでテナーを選ぶ)。"""
    valuation_date = _resolve_valuation_date(valuation_date)
    today = _today()
    if valuation_date > today:
        raise ValueError(f"評価日が未来日です: {valuation_date}")
    if valuation_date == today:
        snapshot = realtime_snapshot(valuation_date, _rates_from_range(real_time_range))
    else:
        labels = [t for t, _, use in _realtime_rows(real_time_range) if use]
        with _historical_store() as store:
            snapshot = db_snapshot(valuation_date, labels, store)
    return curve_set(snapshot)


_fixings_cache: Dict[tuple, Dict[datetime.date, float]] = {}


def _fixings_from_range(fixing_range, valuation_date: datetime.date) -> Optional[Dict[datetime.date, float]]:
    """フィキシング範囲(日付 / TONA(%) の2列)→ {日付: レート}(評価日より前の日だけ)。省略時はNone。

    評価日当日以降の実績は使わない(当日分は公表前。過去の評価日でも同じ扱いにする)。
    見出し行・空行など日付や数値として解釈できない行は読み飛ばす。範囲は渡されたが中身が
    空(参照先ブックが閉じている等)の場合は空辞書を返し、過去起算なら price_swap 側で
    「不足」エラーになる。同じ内容なら前回の結果を使い回す(全UDFが毎回渡すため)。
    """
    if fixing_range is None:
        return None
    key = (valuation_date, tuple(tuple(r) if r is not None else () for r in fixing_range))
    if key in _fixings_cache:
        return _fixings_cache[key]
    fixings: Dict[datetime.date, float] = {}
    for row in fixing_range:
        if not row or len(row) < 2 or row[0] in (None, "") or row[1] in (None, ""):
            continue
        try:
            d = _to_date(row[0])
            if d < valuation_date:
                fixings[d] = float(row[1])
        except (TypeError, ValueError):
            continue
    _fixings_cache.clear()  # 直近1件だけ保持
    _fixings_cache[key] = fixings
    return fixings


def _params(
    start, tenor, end_date, fix_freq, fix_dcf, float_freq, float_dcf, roll_conv,
    notional, pay_rec, fix_rate, fixings, spot: datetime.date,
) -> SwapParams:
    """spot: 評価日のスポット日(startがテナー入力のときの起点)。"""
    return SwapParams(
        start=_start_date(start, spot), fix_freq=fix_freq, fix_dcf=fix_dcf, float_freq=float_freq,
        float_dcf=float_dcf, roll_conv=roll_conv, notional=float(notional), pay_rec=pay_rec,
        tenor=None if _is_blank(tenor) else tenor, end=_to_date(end_date),
        fix_rate=None if _is_blank(fix_rate) else float(fix_rate), fixings=fixings,
    )


@xw.func
@xw.arg("real_time_range", ndim=2)
@xw.arg("fixing_range", ndim=2)
def FairRate(
    start, tenor, end_date, fix_freq, fix_dcf, float_freq, float_dcf, roll_conv,
    real_time_range, valuation_date=None, fixing_range=None,
) -> float:
    """指定コンベンションのパーレート(%)を返す。tenorかend_dateのどちらかは空欄にする。

    real_time_range は RealTime!B2:D45(B列=採否フラグ[1/空欄=使う, 0=使わない],
    C列=Ticker, D列=Value)。valuation_date は Main!評価日(空欄=今日)。

    引数名は ``end`` だとVBAの予約語(Endステートメント)と衝突し、xlwingsが
    生成する ``xlwings_udfs`` モジュールがコンパイルできなくなるため ``end_date``。
    """
    curves = _market(real_time_range, valuation_date)
    params = _params(
        start, tenor, end_date, fix_freq, fix_dcf, float_freq, float_dcf, roll_conv, 1.0, "PAY",
        None, _fixings_from_range(fixing_range, curves.valuation_date), curves.base.spot_date,
    )
    return price(curves.base, curves.valuation_date, params).target_fixrate


@xw.func
@xw.arg("real_time_range", ndim=2)
@xw.arg("fixing_range", ndim=2)
def SwapPV(
    start, tenor, end_date, fix_freq, fix_dcf, float_freq, float_dcf, roll_conv,
    notional, pay_rec, fix_rate, real_time_range, valuation_date=None, fixing_range=None,
) -> float:
    """PV(入力fix_rateが空欄ならパーレートで計算、常に0近辺)。"""
    curves = _market(real_time_range, valuation_date)
    params = _params(
        start, tenor, end_date, fix_freq, fix_dcf, float_freq, float_dcf, roll_conv, notional,
        pay_rec, fix_rate, _fixings_from_range(fixing_range, curves.valuation_date), curves.base.spot_date,
    )
    return price(curves.base, curves.valuation_date, params).pv


@xw.func
@xw.arg("real_time_range", ndim=2)
@xw.arg("fixing_range", ndim=2)
def SwapDelta(
    start, tenor, end_date, fix_freq, fix_dcf, float_freq, float_dcf, roll_conv,
    notional, pay_rec, fix_rate, real_time_range, valuation_date=None, fixing_range=None,
) -> float:
    """カーブに使っている全テナーを+1bpしたときのデルタ(百万円単位)。
    fix_rateが空欄ならパーレートに固定してから計算する。"""
    curves = _market(real_time_range, valuation_date)
    params = _params(
        start, tenor, end_date, fix_freq, fix_dcf, float_freq, float_dcf, roll_conv, notional,
        pay_rec, fix_rate, _fixings_from_range(fixing_range, curves.valuation_date), curves.base.spot_date,
    )
    return parallel_delta(curves, resolve_params(curves, params)) / 1_000_000.0


@xw.func
@xw.arg("real_time_range", ndim=2)
@xw.arg("fixing_range", ndim=2)
def SwapAnnuityDelta(
    start, tenor, end_date, fix_freq, fix_dcf, float_freq, float_dcf, roll_conv,
    notional, pay_rec, real_time_range, valuation_date=None, fixing_range=None,
) -> float:
    """アニュイティデルタ(解析的近似、参考値、百万円単位)。"""
    curves = _market(real_time_range, valuation_date)
    params = _params(
        start, tenor, end_date, fix_freq, fix_dcf, float_freq, float_dcf, roll_conv, notional,
        pay_rec, None, _fixings_from_range(fixing_range, curves.valuation_date), curves.base.spot_date,
    )
    result = price(curves.base, curves.valuation_date, params)
    return annuity_delta(result.annuity, float(notional), pay_rec) / 1_000_000.0


@xw.func
@xw.arg("real_time_range", ndim=2)
@xw.arg("fixing_range", ndim=2)
def NotionalForDelta(
    target_delta_mm, start, tenor, end_date, fix_freq, fix_dcf, float_freq, float_dcf, roll_conv,
    pay_rec, real_time_range, valuation_date=None, fixing_range=None,
) -> float:
    """目標delta(百万円単位)から必要なnotionalを逆算する(アニュイティデルタ基準)。"""
    curves = _market(real_time_range, valuation_date)
    params = _params(
        start, tenor, end_date, fix_freq, fix_dcf, float_freq, float_dcf, roll_conv, 1.0,
        pay_rec, None, _fixings_from_range(fixing_range, curves.valuation_date), curves.base.spot_date,
    )
    result = price(curves.base, curves.valuation_date, params)
    target_delta_yen = float(target_delta_mm) * 1_000_000.0
    return solve_notional_for_target_delta(target_delta_yen, result.annuity, pay_rec)


# RiskGrid が pricing表から読む列(見出し名)
_RISK_COLUMNS = (
    "risk_flag", "notional", "start", "tenor", "end", "fix rate", "fix freq", "fix dcf",
    "float freq", "float dcf", "roll conv", "pay/rec",
)


def _is_flagged(value) -> bool:
    try:
        return float(value) == 1.0
    except (TypeError, ValueError):
        return False


@xw.func
@xw.arg("pricing_table", ndim=2)
@xw.arg("real_time_range", ndim=2)
@xw.arg("fixing_range", ndim=2)
def RiskGrid(pricing_table, real_time_range, valuation_date=None, fixing_range=None) -> List[list]:
    """
    risk_flag=1 の全行について、テナーごとに+1bpしたときのデルタ(百万円単位)の合計を、
    RealTimeシートのテナー行の並びで返す(Ticker/Deltaの2列、1行目は見出し)。

    pricing_table: pricingシートの見出し行(1行目)から最終行まで(例: pricing!$A$1:$T$201)。
        列は見出し名で探す(risk_flag, notional, start, tenor, end, fix rate, fix freq,
        fix dcf, float freq, float dcf, roll conv, pay/rec)。
    バンプするのはカーブに使っているテナーだけで、それ以外の行(フラグ0、会合スワップ等)は空欄。
    合計値(全テナーの和)はExcelのSUMで出す。
    """
    rows = [list(r) if r is not None else [] for r in (pricing_table or [])]
    if not rows:
        raise ValueError("pricing表が空です")
    header = [str(h).strip().lower() if h is not None else "" for h in rows[0]]
    missing = [c for c in _RISK_COLUMNS if c not in header]
    if missing:
        raise ValueError(f"pricing表に見出しがありません: {', '.join(missing)}")
    col = {c: header.index(c) for c in _RISK_COLUMNS}

    curves = _market(real_time_range, valuation_date)
    fixings = _fixings_from_range(fixing_range, curves.valuation_date)
    totals = {label: 0.0 for label in curves.bump_labels}
    for n, row in enumerate(rows[1:], start=2):
        row = row + [None] * (len(header) - len(row))
        if not _is_flagged(row[col["risk_flag"]]):
            continue
        try:
            params = _params(
                *(row[col[c]] for c in ("start", "tenor", "end", "fix freq", "fix dcf",
                                        "float freq", "float dcf", "roll conv", "notional",
                                        "pay/rec", "fix rate")),
                fixings, curves.base.spot_date,
            )
            deltas = bucketed_delta(curves, resolve_params(curves, params))
        except Exception as e:
            raise ValueError(f"pricing表の{n}行目(risk_flag=1)を計算できません: {e}") from e
        for label, value in deltas.items():
            totals[label] += value

    result = [["Ticker", "Delta"]]
    for ticker, _, _ in _realtime_rows(real_time_range):
        value = totals.get(ticker.lower())
        result.append([ticker, value / 1_000_000.0 if value is not None else ""])
    return result


@xw.func
@xw.arg("real_time_range", ndim=2)
def MarketInfo(real_time_range, valuation_date=None) -> str:
    """評価日のカーブの元データの説明(例: 「RealTime 2026-09-28 / 34テナー」)。"""
    curves = _market(real_time_range, valuation_date)
    info = f"{curves.snapshot.source} {curves.valuation_date} / カーブに使用 {len(curves.bump_labels)}テナー"
    selected = {t.lower() for t, _, use in _realtime_rows(real_time_range) if use}
    unused = sorted(selected - set(curves.bump_labels))
    if unused:
        info += f" / 値なし・未使用: {', '.join(unused)}"
    return info


def _meeting_periods(meeting_range) -> List[MeetingPeriod]:
    """RealTimeの会合スワップ行 [Ticker, Value, 起算日, 満期日] → 期間の列(上から順)。

    値か日付が欠けた行(LSEGで未提示のM10以降など)で打ち切る。途中の期間が欠けたまま先の期間を
    使うと、欠けた期間全体が直前の期間のフォワードで埋まってしまうため。
    """
    periods = []
    for row in meeting_range or []:
        if row is None or len(row) < 4 or _is_blank(row[0]):
            continue
        ticker, value, start, end = row[:4]
        if any(_is_blank(v) for v in (value, start, end)) or isinstance(value, str):
            break
        periods.append(MeetingPeriod(str(ticker).strip().lower(), _to_date(start), _to_date(end), float(value)))
    return periods


_meeting_cache: Dict[tuple, BootstrapResult] = {}


def _meeting_curve(real_time_range, meeting_range, valuation_date) -> BootstrapResult:
    valuation_date = _resolve_valuation_date(valuation_date)
    if valuation_date != _today():
        raise ValueError("会合スワップのカーブはリアルタイム(評価日=今日)だけに対応しています")
    on_rate = next(
        (float(value) for ticker, value, _ in _realtime_rows(real_time_range)
         if ticker.lower() in ON_KEYS and not _is_blank(value) and not isinstance(value, str)),
        None,
    )
    if on_rate is None:
        raise ValueError("RealTimeシートのO/Nの値がありません")
    periods = _meeting_periods(meeting_range)
    key = (valuation_date, on_rate, tuple(periods))
    if key not in _meeting_cache:
        _meeting_cache.clear()  # 直近1件だけ保持
        _meeting_cache[key] = build_meeting_curve(valuation_date, on_rate, periods)
    return _meeting_cache[key]


@xw.func
@xw.arg("tenors", ndim=2)
@xw.arg("real_time_range", ndim=2)
@xw.arg("meeting_range", ndim=2)
def BojImpliedRates(tenors, real_time_range, meeting_range, valuation_date=None) -> List[list]:
    """会合スワップ(RD・M1〜)とO/Nで引いた階段状のカーブから、スポット起算のパーレート(%)を返す
    (tenorsと同じ行数の1列。標準コンベンション PA/act365f/STD = RealTimeのスポットと同じ)。

    tenors: テナーの列(例: BOJ!A21:A33)。real_time_range: RealTime!B2:D45(O/Nを読む)。
    meeting_range: RealTime!C36:F45(Ticker/Value/起算日/満期日)。valuation_date: Main!評価日。
    カーブの引き方は swap_pricing/meeting_curve.py。最後の会合期間より先のテナーはその行だけ
    エラーの文字列になる。
    """
    boot = _meeting_curve(real_time_range, meeting_range, valuation_date)
    result = []
    for row in tenors or []:
        label = row[0] if row else None
        if _is_blank(label):
            result.append([""])
            continue
        params = SwapParams(
            start=boot.spot_date, fix_freq="PA", fix_dcf="act/365fixed", float_freq="PA",
            float_dcf="act/365fixed", roll_conv="STD", notional=1.0, pay_rec="PAY",
            tenor=str(label).strip().lower(),
        )
        try:
            result.append([price(boot, boot.valuation_date, params).target_fixrate])
        except (RuntimeError, ValueError) as exc:
            result.append([f"エラー: {exc}"])
    return result


def _historical_store() -> CurveStore:
    """ヒストリカルの日次カーブDB(朝バッチ swap_pricing/curve_batch.py が作成)。
    with 文で使うと、計算中はDB接続を1本だけ開いて使い回す。"""
    store = CurveStore()
    if not store.exists():
        raise ValueError(
            "ヒストリカルカーブDBがありません。historical_data.xlsxを更新・保存してから "
            "python -m swap_pricing.curve_batch を実行してください"
        )
    return store


@xw.func
def HistoricalOutright(tenor, fix_freq, fix_dcf, float_freq, float_dcf, roll_conv) -> List[list]:
    """
    スポット起点+tenorのヒストリカル・パーレート推移(Date/Rateの2列スピル配列)。
    HistoricalRate の ROLLING・start空欄 と同じ(Historicalシートの既存数式のために残す)。
    カーブ(スプレッド)やフライは、この結果をExcel側で引き算するだけで求まる。
    """
    with _historical_store() as store:
        series = historical_swap_rate_series(
            ROLLING, fix_freq, fix_dcf, float_freq, float_dcf, roll_conv, tenor=tenor, curves=store,
        )
    rows = [["Date", "Rate"]]
    rows += [[p.as_of_date, p.par_rate] for p in series]
    return rows


def _date_or_imm(value):
    """ヒストリカル用のstart/end列の値 → 日付、IMMコード・テナーの文字列、または None(空欄)。
    IMMコードとテナー(startのフォワードスタート)は文字列のまま渡し、日付への変換は
    historical_pricer が基準日のスポット日を決めてから行う。"""
    if isinstance(value, str) and (is_imm_code(value) or is_tenor(value)):
        return value.strip().upper()
    return _to_date(value)


@xw.func
def HistoricalRate(
    start, tenor, end_date, fix_freq, fix_dcf, float_freq, float_dcf, roll_conv, mode,
    base_date=None, from_date=None, to_date=None,
) -> List[list]:
    """
    pricingシートの1行分のコンベンションのヒストリカル・パーレート推移
    (Date/Rate/Start/Endの4列スピル配列、1行目は見出し)。

    mode: "FIXED"(同じ日付の取引をそのまま過去日で評価)/
          "ROLLING"(基準日のスポット日からの距離を保って各過去日へ平行移動)。
          詳細は swap_pricing/historical_pricer.py。
    base_date: ROLLINGの基準日(空欄=今日)。通常は Main!評価日 を渡す。
    from_date/to_date: 計算する過去日の範囲(空欄=DBにある全期間)。
    過去起算になる日のTONA実績は、DBに取り込み済みのもの(Fixingシート由来)を使う。
    """
    with _historical_store() as store:
        series = historical_swap_rate_series(
            mode, fix_freq, fix_dcf, float_freq, float_dcf, roll_conv,
            start=_date_or_imm(start), tenor=tenor or None, end=_date_or_imm(end_date),
            base_date=_to_date(base_date), from_date=_to_date(from_date), to_date=_to_date(to_date),
            fixings=store.fixings(), curves=store,
        )
    rows = [["Date", "Rate", "Start", "End"]]
    rows += [[p.as_of_date, p.par_rate, p.start_date, p.maturity_date] for p in series]
    return rows


_MAX_LEGS = 3
_LEG_LABELS = ("A", "B", "C")


def _specs_from_range(conventions) -> List[Optional[SwapSpec]]:
    """コンベンション範囲(最大3行 × start/tenor/end/fix freq/fix dcf/float freq/float dcf/
    roll conv の8列)→ 脚ごとの SwapSpec。start・tenor・endが全て空の行は脚なし(None)。"""
    rows = [list(r) if r is not None else [] for r in (conventions or [])]
    if len(rows) > _MAX_LEGS:
        raise ValueError(f"脚は最大{_MAX_LEGS}本(3行)までです: {len(rows)}行")
    specs: List[Optional[SwapSpec]] = []
    for row in rows:
        start, tenor, end, fix_freq, fix_dcf, float_freq, float_dcf, roll_conv = (row + [None] * 8)[:8]
        if all(_is_blank(v) for v in (start, tenor, end)):
            specs.append(None)
            continue
        specs.append(SwapSpec(
            fix_freq, fix_dcf, float_freq, float_dcf, roll_conv,
            start=_date_or_imm(start), tenor=None if _is_blank(tenor) else str(tenor).strip(),
            end=_date_or_imm(end),
        ))
    return specs


class _WithRealTimeLatest:
    """ヒストリカルDBの日付に、今日(RealTimeシートのライブのレート)を最新日として加えたカーブの
    供給元。今日のカーブはpricingシートと同じ market のカーブ一式を使う(DBに今日の引け値が
    あっても、今日はライブのレートを優先する)。日付だけを求める呼び出しではカーブを作らない。"""

    def __init__(self, store: CurveStore, real_time_range, today: datetime.date):
        self._store = store
        self._real_time_range = real_time_range
        self._today = today

    def dates(self) -> List[datetime.date]:
        return [d for d in self._store.dates() if d < self._today] + [self._today]

    def load(self, as_of_date: datetime.date, until=None):
        if as_of_date == self._today:
            return _market(self._real_time_range, self._today).base
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

    conventions: 最大3行 × 8列(start, tenor, end, fix freq, fix dcf, float freq,
        float dcf, roll conv。pricingシートと同じ並び)。start・tenor・endが全て空の行は
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
    today = _today()
    base = _to_date(base_date) or today
    with _historical_store() as store:
        curves = store
        if (
            real_time_range is not None and base == today
            and CALENDAR.isBusinessDay(ql.Date.from_date(today))
        ):
            curves = _WithRealTimeLatest(store, real_time_range, today)
        series = historical_multi_rate_series(
            active, mode, base_date=base, from_date=_to_date(from_date),
            to_date=_to_date(to_date), fixings=store.fixings() if active else None, curves=curves,
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
