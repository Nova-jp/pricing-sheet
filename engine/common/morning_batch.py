"""
ヒストリカル日次カーブDB(data/historical.db)を作るバッチ。朝に手動で実行する。

使い方:
  1. historical_data.xlsx をExcelで開き、LSEGのデータ更新が終わったら保存する
     (このバッチは保存済みの値を読む。Excelで開いたままでもよい)
  2. python -m engine.common.morning_batch
     オプション: --xlsx パス / --config パス / --db パス / --settings パス / --rebuild(全日付を作り直す)

■ ヒストリカルを取り始める日は settings.toml の [history](engine/common/settings.py)
  ④〜⑥のLSEGからの取得は、設定の開始日以降でDBに足りない分を取る(新しいPCでも、普通に流すだけで過去分が揃う)。
  何日以降を取得済みかは DBの fetch_coverage に記録する。開始日を早めると、次の実行で足りない過去分だけを取る。
  開始日を遅らせても、取得済みの古い分は消さない。スワップのMid・TONAは historical_data.xlsx の設定のまま。

■ DBに入れるのは昨日までの引けだけ(2026-10-27、ユーザー指示)
  どの取得も、実行した日の前日(until = 今日 − 1日)までの日付の値だけを保存する。今日の値はブックがライブで
  見るので、DBに入れるとライブと二重になる(場が引けた後に流したときなど)。今日の引け値が欲しくなったら、
  ブック側の機能としてその場でLSEGから取る(DBには入れない)。

処理:
  ① historical_data.xlsx の Mid(日付×テナーのミッド)と Fixing(TONA実績)を読み、
     DBの quotes / fixings に取り込む(同じ日付は置き換え。過去に取り込んだ日付は残る)。
     (historical_data.xlsx の Vol系のシートは使わない。ボラは⑥⑦でTraditionのプレミアムから作る)
  ② DBにある全日付について、historical_curve.toml で選んだテナーでカーブを構築し、
     暦日ごとのDFを curves に保存する(形式は historical_store.py)。
     - "O/N" はその日のTONA実績(フィキシング対象日=その日)を使う
     - その日に値が無いテナーは外し、あるテナーだけで引く(使用/不使用テナーを記録)
     - 日本の非営業日(ql.Japan)は作らない。LSEGには祝日にも一部テナーの値が
       入ることがある(実データで1テナーだけの日もあった)が、TONAも市場も無い日の
       カーブは意味を持たないため
     - 入力(構築方法・使うレート)のハッシュが前回と同じ日は作り直さない。
       テナー選択や構築方法を変えれば、該当する日が自動で作り直される
  ③ (欠番。以前のNeonからのASWの取得。ASWは⑨で計算する)
  ④ LSEG(Python API、engine/common/jgb_source.py)からJGBの銘柄情報と引け値(みずほ、無い日は大和・JCLで補う)を
     取得し、jgb_bonds / jgb_closes に保存する(現物オプション BondOption.xlsm・国債先物のベーシス用)。引け値は直近の
     CLOSE_LOOKBACK_DAYS 日分を取り直す(配信元の訂正を拾う。DBの最終日がそれより古ければその日から)。
     設定の開始日(jgb)までの過去分が足りなければ、全銘柄について一緒に取る(sync_jgbs)。LSEG Workspaceが
     起動していない等で失敗しても、ここだけスキップする。--skip-jgb で行わない。
  ⑤ LSEG(engine/common/futures_source.py)から長期国債先物の限月ごとの清算値を取得し、fut_settles に
     保存する(設定の開始日(futures)の期近から。取り終えた限月は取らず、取引中の限月は保存済みの最終日から取り直す)。
     失敗してもここだけスキップする。--skip-jgb で④と一緒に行わない。
  ⑥ LSEG(Python API、engine/common/swaption_source.py)から TraditionのスワップションATMプレミアムを
     差分で取得し、swaption_premiums に保存する(前回の最終日の少し前から。初回と、格子に足した銘柄と、足りない
     過去分は設定の開始日(swaption)から。Traditionの系列は2022-01-12より前が無い)。
     失敗してもここだけスキップする。--skip-vol で行わない。
  ⑦ プレミアムと②のその日のカーブから、営業日ベースのノーマルvol(engine/common/option/swaption/convert.py)を計算し、
     vol_quotes に保存する(分析ブックのPCA・VolHV用。カーブには使わない)。入力(カーブの入力ハッシュ・
     プレミアム・計算方法)が前回と同じ日は計算し直さない。プレミアムのある最終日までで、プレミアムかカーブの
     無い日の vol_quotes は消す(以前のLSEG計算系列 =R の値を残さない)。プレミアムが1件も無ければ消さない。
  ⑧ ②のカーブから、スワップションの原資産(⑦と同じ契約、engine/common/option/swaption/forward.py)の格子の全銘柄の
     Fwd・年金と、同じ契約の前の日付のカーブでのFwdを計算し、swaption_forwards に保存する(分析ブックの
     VolHV・VolLevelが読む)。入力(その日と前の日付のカーブの入力ハッシュ・銘柄・計算方法)が前回と同じ日は
     計算し直さない。カーブの無くなった日の行は消す。
  ⑨ ②のカーブと④の引け値から、JGBの全銘柄のASW(現物の複利 − MMS、PA/SA。engine/common/asw.py)を計算し、
     asw_quotes に保存する(ASWシートが読む)。カーブと引け値のそろう日だけで、入力(その日のカーブの入力ハッシュ・
     引け値・銘柄情報・計算方法)が前回と同じ日は計算し直さない。計算していない日の行は消す(以前Neonから取得した
     値も残さない)。

カーブの引き方は curve.CURVE_BUILDERS から名前で選ぶだけで、このバッチは引き方に依存しない。
"""

import argparse
import hashlib
import json
import sys
import tomllib
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

import openpyxl
import QuantLib as ql

from engine.common import asw, futures_source, jgb_futures, jgb_source, swaption_source
from engine.common.conventions import CALENDAR
from engine.common.curve import get_curve_builder
from engine.common.historical_store import GRID_DAYS, HistoricalStore, discount_grid
from engine.common.option.swaption import forward as swaption_forward
from engine.common.option.swaption.convert import PremiumConverter
from engine.common.paths import HISTORICAL_DB_PATH, REPO_ROOT
from engine.common.settings import DEFAULT_SETTINGS, load_settings

DEFAULT_XLSX = REPO_ROOT / "historical_data.xlsx"
DEFAULT_CONFIG = REPO_ROOT / "historical_curve.toml"
ON_TENOR = "O/N"
_DATE_HEADER = "日付"
# fetch_coverage(取得済みの範囲の記録)のデータ名。引け値は配信元の優先順位も名前に含め、順位を変えたら
# 開始日からの過去分を新しい順位で1回取り直す(古い順位で取った値を残さない)
JGB_COVERAGE = "jgb_closes/" + ",".join(source for source, _ in jgb_source.HISTORY_SOURCES)
SWAPTION_COVERAGE = "swaption_premiums"
# プレミアムの差分取得で、前回の最終日より何日前から取り直すか(LSEG側の修正を拾うため)
PREMIUM_OVERLAP_DAYS = 10
# JGBの引け値を毎朝、昨日から何日前まで取り直すか
CLOSE_LOOKBACK_DAYS = 10
# volの計算方法の名前(式・営業日の数え方を変えたら変える。全日付を計算し直す)
VOL_METHOD = "trdt-fwd-straddle/bd-incl-today/252"


class CurveConfig(NamedTuple):
    builder: str
    tenors: List[str]


class BatchSummary(NamedTuple):
    built: List[date]
    unchanged: int
    holidays: List[date]  # 日本の非営業日のため作らなかった日
    with_missing: Dict[date, List[str]]  # テナーが欠けたまま構築した日
    failed: Dict[date, str]
    fetch_failures: Tuple[str, ...] = ()  # LSEGからの取得の失敗(スキップした取得)。実行の記録を失敗にする


def load_config(path: Path) -> CurveConfig:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    tenors = [str(t).strip().upper() for t in raw.get("tenors", [])]
    if not tenors:
        raise ValueError(f"{path}: tenors が空です")
    builder = str(raw.get("builder", "")).strip()
    get_curve_builder(builder)  # 未対応の名前はここでエラー
    return CurveConfig(builder=builder, tenors=tenors)


def last_close_date(today: Optional[date] = None) -> date:
    """DBに入れる最後の日付(今日の前日)。"""
    return (today or date.today()) - timedelta(days=1)


def _as_date(value) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _as_rate(value) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None  # 空文字・"#N/A"等のエラー値は欠損扱い


def _rows_after_date_header(ws, header_text: str = _DATE_HEADER):
    """A列が header_text の見出し行を探し、(見出し行, 以降の行のイテレータ) を返す。"""
    rows = ws.iter_rows(values_only=True)
    for row in rows:
        if row and isinstance(row[0], str) and row[0].strip() == header_text:
            return row, rows
    raise ValueError(f"{ws.title}シートに「{header_text}」の見出し行がありません")


def _date_matrix(rows, labels) -> Dict[date, Dict[str, float]]:
    """見出し行の後の行(A列=日付、B列以降=銘柄ごとの値)→ {日付: {銘柄: 値}}。値の無い日は除く。"""
    result: Dict[date, Dict[str, float]] = {}
    for row in rows:
        as_of = _as_date(row[0]) if row else None
        if as_of is None:
            continue  # RIC行・空行
        values = {}
        for label, value in zip(labels, row[1:]):
            v = _as_rate(value)
            if label is not None and v is not None:
                values[label] = v
        if values:
            result[as_of] = values
    return result


def read_historical_workbook(path: Path) -> Tuple[Dict[date, Dict[str, float]], Dict[date, float]]:
    """historical_data.xlsx → ({日付: {テナー: ミッド%}}, {フィキシング対象日: TONA%})。"""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        header, rows = _rows_after_date_header(wb["Mid"])
        tenors = [str(h).strip().upper() if h not in (None, "") else None for h in header[1:]]
        mid = _date_matrix(rows, tenors)

        _, rows = _rows_after_date_header(wb["Fixing"])
        fixings: Dict[date, float] = {}
        for row in rows:
            d = _as_date(row[0]) if row else None
            rate = _as_rate(row[1]) if row and len(row) > 1 else None
            if d is not None and rate is not None:
                fixings[d] = rate
    finally:
        wb.close()
    if not mid:
        raise ValueError(
            f"{path.name}のMidシートにデータがありません。Excelで開いてLSEGの更新が"
            "終わってから保存し、もう一度実行してください"
        )
    return mid, fixings


def select_quotes(
    quotes: Dict[str, float], fixing: Optional[float], tenors: List[str],
) -> Tuple[Dict[str, float], List[str]]:
    """設定のテナーのうち、その日に値があるものだけを選ぶ → (使うレート, 欠けているテナー)。"""
    selected: Dict[str, float] = {}
    missing: List[str] = []
    for tenor in tenors:
        value = fixing if tenor == ON_TENOR else quotes.get(tenor)
        if value is None:
            missing.append(tenor)
        else:
            selected[tenor] = value
    return selected, missing


def input_hash(builder: str, selected: Dict[str, float]) -> str:
    payload = json.dumps(
        {"builder": builder, "grid_days": GRID_DAYS, "quotes": sorted(selected.items())},
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def sync_jgbs(store: HistoricalStore, start: date, log=print, until: Optional[date] = None) -> Optional[str]:
    """LSEGからJGBの銘柄情報と、start(設定の開始日)〜until(省略時は昨日)の引け値を、DBに足りない分だけ取得して
    保存する。失敗してもバッチ全体は止めず、失敗の内容を返す(成功なら None。実行の記録を失敗にする)。

    - 毎朝: 直近 CLOSE_LOOKBACK_DAYS 日分を取り直す(配信元の訂正を拾う)。DBの最終日がそれより古ければ
      (朝バッチを流さなかった期間)、その日から取り直す。DBが空なら start から全期間を1回で取る。
    - 過去分: fetch_coverage の記録(何日以降を取得済みか)が start より後か、記録が無ければ、
      start〜毎朝の取得の前日を、DBの銘柄情報(jgb_bonds。償還済みでも以前に保存した銘柄を含む)のうち start より後に
      償還する全銘柄について取る。取れたら記録を start にする(失敗したら翌朝に取り直す)。
    """
    until = until or last_close_date()
    last = store.jgb_closes_last_date()
    covered = store.fetch_coverage(JGB_COVERAGE)
    recent = until - timedelta(days=CLOSE_LOOKBACK_DAYS)
    if last is None:
        recent = start
    elif last < recent:
        recent = last
    recent = max(recent, start)
    try:
        bonds, closes = jgb_source.fetch_jgbs(recent, until)
    except Exception as e:
        log(f"JGB取得: 失敗のためスキップ ({e})")
        return f"JGB取得: {e}"
    store.save_jgb_bonds(bonds)
    store.save_jgb_closes(closes)
    latest = max((c.as_of_date for c in closes), default=None)
    count = sum(1 for c in closes if c.as_of_date == latest)
    log(f"JGB取得: 銘柄 {len(bonds)} / 引け値 {len(closes)}件({recent}〜、直近 {latest} は{count}銘柄)")
    if recent <= start:
        store.set_fetch_coverage(JGB_COVERAGE, start)
        return None
    if covered is not None and covered <= start:
        return None
    end = min(covered, recent) - timedelta(days=1) if covered is not None else recent - timedelta(days=1)
    bonds = [
        jgb_source.JgbRecord(r.code, r.ric, r.isin, r.coupon, r.maturity)
        for r in store.jgb_master(until) if r.maturity > start
    ]
    try:
        history = jgb_source.fetch_close_history(bonds, start, end)
    except Exception as e:
        log(f"引け値の過去分: 失敗のためスキップ、次の朝バッチで取り直す ({e})")
        return f"引け値の過去分: {e}"
    store.save_jgb_closes(history)
    store.set_fetch_coverage(JGB_COVERAGE, start)
    missing = len({b.code for b in bonds} - {c.code for c in history})
    log(f"引け値の過去分: {len(bonds)}銘柄 / {len(history)}件({start}〜{end}、期間内に値の無い銘柄 {missing})")
    return None


def sync_futures(store: HistoricalStore, start: date, log=print, today: Optional[date] = None) -> Optional[str]:
    """長期国債先物の限月ごとの清算値を、start(設定の開始日)の期近から昨日まで差分で保存する。
    失敗してもバッチ全体は止めず、失敗の内容を返す(成功なら None)。"""
    today = today or date.today()
    until = last_close_date(today)
    saved = store.fut_last_dates()
    plan: Dict[jgb_futures.Contract, date] = {}
    first = jgb_futures.front_contract(start)
    final = jgb_futures.front_contract(today).next().next()  # 取引中は期近から3限月
    for contract in jgb_futures.contracts_between(first, final):
        last = saved.get(contract.label)
        if last is not None and last >= jgb_futures.last_trade_date(contract):
            continue  # 取り終えた限月
        plan[contract] = last or start  # 保存済みの最終日から取り直す(清算値の訂正に備える)
    try:
        result = futures_source.fetch_settles(plan, until)
    except Exception as e:
        log(f"先物取得: 失敗のためスキップ ({e})")
        return f"先物取得: {e}"
    settles = [s for s in result.settles if s[0] <= until]
    store.save_fut_settles(settles)
    contracts = sorted({label for _, label, _ in settles})
    log(f"先物取得: 清算値 {len(settles)}件({', '.join(contracts)})")
    for w in result.warnings:
        log(f"  警告: {w}")
    return None


def sync_swaption_premiums(
    store: HistoricalStore, start: date, log=print, today: Optional[date] = None,
) -> Optional[str]:
    """LSEGからTraditionのスワップションATMプレミアムを、start(設定の開始日。Traditionの系列の最初の日
    swaption_source.FIRST_DATE より前は無い)から昨日まで差分で取得して保存する。失敗してもバッチ全体は止めず、
    失敗の内容を返す(成功なら None)。
    - 毎朝: 前回の最終日の PREMIUM_OVERLAP_DAYS 日前から取り直す
    - 格子に足した銘柄(DBに1行も無い銘柄)は、差分の前の分も start から取る(2026-11-09 原資産30Yの追加)
    - 取得済みの範囲(fetch_coverage。記録が無ければDBの最初の日付)が start より後なら、足りない過去分を全銘柄で取る
    """
    until = last_close_date(today)
    start = max(start, swaption_source.FIRST_DATE)
    last = store.swaption_premiums_last_date()
    covered = store.fetch_coverage(SWAPTION_COVERAGE) or store.swaption_premiums_first_date()
    recent = start if last is None else max(start, last - timedelta(days=PREMIUM_OVERLAP_DAYS))
    have = store.swaption_premium_labels()
    new = [] if last is None else [n for n in swaption_source.labels() if n not in have]
    backfill = last is not None and covered is not None and covered > start
    try:
        rows = swaption_source.fetch_premiums(recent, until)
        if new:
            rows += swaption_source.fetch_premiums(start, recent - timedelta(days=1), new)
            log(f"スワップションのプレミアム取得: 新しい銘柄 {len(new)}本は{start}から取得")
        if backfill:
            end = min(covered, recent) - timedelta(days=1)
            rows += swaption_source.fetch_premiums(start, end)
            log(f"スワップションのプレミアム取得: 過去分を{start}〜{end}で取得")
    except Exception as e:
        log(f"スワップションのプレミアム取得: 失敗のためスキップ ({e})")
        return f"スワップションのプレミアム取得: {e}"
    rows = [r for r in rows if r.as_of_date <= until]
    store.save_swaption_premiums(rows)
    if last is None or backfill:
        store.set_fetch_coverage(SWAPTION_COVERAGE, start)
    dates = sorted({r.as_of_date for r in rows})
    span = f"{dates[0]}〜{dates[-1]}" if dates else "なし"
    log(f"スワップションのプレミアム取得: {len(rows)}件 / {len(dates)}日分({span})")
    return None


def vol_input_hash(curve_hash: str, premiums: Dict[str, float]) -> str:
    payload = json.dumps({"method": VOL_METHOD, "curve": curve_hash, "premiums": sorted(premiums.items())})
    return hashlib.sha256(payload.encode()).hexdigest()


def build_vols(store: HistoricalStore, rebuild: bool = False, log=print) -> Dict[date, str]:
    """プレミアムとその日のカーブから vol_quotes を作る。失敗した日 → 内容 を返す。"""
    premiums = store.swaption_premiums()
    curve_hashes = store.input_hashes()
    existing = {} if rebuild else store.vol_input_hashes()
    keep: List[date] = []
    built = 0
    failed: Dict[date, str] = {}
    with store:
        for as_of in sorted(premiums):
            curve_hash = curve_hashes.get(as_of)
            if curve_hash is None:
                continue  # カーブの無い日(非営業日・構築の失敗)
            keep.append(as_of)
            h = vol_input_hash(curve_hash, premiums[as_of])
            if existing.get(as_of) == h:
                continue
            try:
                ql.Settings.instance().evaluationDate = ql.Date.from_date(as_of)
                converter = PremiumConverter(store.load(as_of).curve, as_of)
                vols = {}
                for label, premium in premiums[as_of].items():
                    v = converter.convert(label, premium)
                    if v is not None:
                        vols[label] = v.vol
            except Exception as e:  # 1日の失敗で全体を止めない
                failed[as_of] = str(e)
                continue
            store.save_vols(as_of, vols, h)
            built += 1
        # プレミアムが1件も無い(初回の取得に失敗した等)ときは何も消さない。消すのはプレミアムのある最終日まで
        # (2026-10-25に、初回の取得がLSEGの上限で失敗し、以前の vol_quotes を全て消してしまった)
        removed = store.delete_vols_except((k for k in keep if k not in failed), until=max(premiums)) if premiums else 0
    log(f"ボラ計算: 新規/更新 {built}日 / 変更なし {len(keep) - built - len(failed)}日 / "
        f"失敗 {len(failed)}日 / 古い行を削除 {removed}日")
    for d, msg in sorted(failed.items()):
        log(f"  {d}: ボラ計算の失敗 {msg}")
    return failed


def run(
    xlsx_path: Path = DEFAULT_XLSX,
    config_path: Path = DEFAULT_CONFIG,
    db_path: Path = HISTORICAL_DB_PATH,
    rebuild: bool = False,
    log=print,
    skip_jgb: bool = False,
    skip_vol: bool = False,
    today: Optional[date] = None,
    settings_path: Path = DEFAULT_SETTINGS,
    clock=datetime.now,
) -> BatchSummary:
    """朝バッチの本体。終わったら(例外で止まったときも)DBの batch_runs に実行を記録する。各ブックのControlシートの
    DataStatus は、データの日付ではなくこの記録の時刻で「今日の実行があるか・評価日を入れた後に実行したか」を判定する
    (営業日のカレンダーで最新の日付を推し量らず、途中で失敗した実行も見分けるため。docs/decisions.md の「評価日」)。
    全部をやり遂げた実行だけを成功とする(カーブの失敗の日がある・LSEGの取得を省略した実行は失敗として記録する)。"""
    store = HistoricalStore(db_path)
    try:
        summary = _run(
            store, xlsx_path, config_path, rebuild, log, skip_jgb, skip_vol, today, settings_path,
        )
    except BaseException as e:
        store.record_batch_run(clock(), False, f"{type(e).__name__}: {e}")
        raise
    problems = list(summary.fetch_failures)
    if summary.failed:
        problems.append(f"カーブの失敗 {len(summary.failed)}日")
    if skip_jgb:
        problems.append("JGB・先物の取得を省略")
    if skip_vol:
        problems.append("スワップションの取得を省略")
    run_record = store.record_batch_run(clock(), not problems, " / ".join(problems))
    log(f"実行の記録: {run_record.finished_at} {'成功' if run_record.ok else '失敗(' + run_record.message + ')'}")
    return summary


def _run(
    store: HistoricalStore,
    xlsx_path: Path,
    config_path: Path,
    rebuild: bool,
    log,
    skip_jgb: bool,
    skip_vol: bool,
    today: Optional[date],
    settings_path: Path,
) -> BatchSummary:
    config = load_config(config_path)
    history = load_settings(settings_path).history
    builder = get_curve_builder(config.builder)
    today = today or date.today()
    until = last_close_date(today)

    mid, fixings = read_historical_workbook(xlsx_path)
    mid = {d: v for d, v in mid.items() if d <= until}
    fixings = {d: v for d, v in fixings.items() if d <= until}
    store.save_quotes(mid)
    store.save_fixings(fixings)
    log(f"取り込み: Mid {len(mid)}日分 / TONA実績 {len(fixings)}日分({until}まで)")
    fetches = []
    if not skip_jgb:
        fetches.append(sync_jgbs(store, history.start_of("jgb"), log, until))
        fetches.append(sync_futures(store, history.start_of("futures"), log, today))
    if not skip_vol:
        fetches.append(sync_swaption_premiums(store, history.start_of("swaption"), log, today))

    all_quotes = store.quotes()
    all_fixings = store.fixings()
    existing = {} if rebuild else store.input_hashes()

    built: List[date] = []
    unchanged = 0
    holidays: List[date] = []
    with_missing: Dict[date, List[str]] = {}
    failed: Dict[date, str] = {}
    for as_of in sorted(all_quotes):
        if not CALENDAR.isBusinessDay(ql.Date.from_date(as_of)):
            holidays.append(as_of)
            continue
        selected, missing = select_quotes(all_quotes[as_of], all_fixings.get(as_of), config.tenors)
        if not any(t != ON_TENOR for t in selected):
            failed[as_of] = "使えるテナーがありません(O/N以外が全て欠損)"
            continue
        h = input_hash(config.builder, selected)
        if existing.get(as_of) == h:
            unchanged += 1
            continue
        try:
            result = builder(as_of, selected)
            dfs = discount_grid(result.curve, as_of)
        except Exception as e:  # 1日の失敗で全体を止めない(最後にまとめて報告)
            failed[as_of] = str(e)
            continue
        store.save_curve(
            as_of, config.builder, h, result.used_tenors, missing + result.skipped_tenors, dfs,
        )
        built.append(as_of)
        if missing:
            with_missing[as_of] = missing
        if len(built) % 50 == 0:
            log(f"  構築中... {len(built)}日分")

    summary = BatchSummary(built, unchanged, holidays, with_missing, failed, tuple(f for f in fetches if f))
    log(
        f"カーブ構築: 新規/更新 {len(built)}日 / 変更なし {unchanged}日 / "
        f"非営業日のため除外 {len(holidays)}日 / 失敗 {len(failed)}日"
    )
    if holidays:
        log(f"  非営業日: {', '.join(str(d) for d in holidays)}")
    for d, tenors in sorted(with_missing.items()):
        log(f"  {d}: 欠損テナーを除いて構築 ({', '.join(tenors)})")
    for d, msg in sorted(failed.items()):
        log(f"  {d}: 失敗 {msg}")
    build_vols(store, rebuild, log)
    swaption_forward.build(store, swaption_source.labels(), rebuild, log)
    asw.build(store, rebuild, log)
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="historical_data.xlsx からヒストリカル日次カーブDBを作る")
    parser.add_argument("--xlsx", type=Path, default=DEFAULT_XLSX)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=HISTORICAL_DB_PATH)
    parser.add_argument("--rebuild", action="store_true", help="入力が同じ日も含めて全日付を作り直す")
    parser.add_argument(
        "--skip-jgb", action="store_true", help="LSEGからのJGBの銘柄情報・引け値と先物の清算値の取得を行わない",
    )
    parser.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS, help="全体の設定(既定 settings.toml)")
    parser.add_argument("--skip-vol", action="store_true",
                        help="LSEGからのスワップションのプレミアムの取得を行わない(保存済みのプレミアムからのボラ計算は行う)")
    args = parser.parse_args(argv)
    summary = run(
        args.xlsx, args.config, args.db, args.rebuild, skip_jgb=args.skip_jgb, skip_vol=args.skip_vol,
        settings_path=args.settings,
    )
    return 1 if summary.failed else 0


if __name__ == "__main__":
    sys.exit(main())
