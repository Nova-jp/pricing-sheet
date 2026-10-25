"""
ヒストリカル日次カーブDB(data/historical.db)を作るバッチ。朝に手動で実行する。

使い方:
  1. historical_data.xlsx をExcelで開き、LSEGのデータ更新が終わったら保存する
     (このバッチは保存済みの値を読む。Excelで開いたままでもよい)
  2. python -m engine.common.morning_batch
     オプション: --xlsx パス / --config パス / --db パス / --rebuild(全日付を作り直す)
     / --jgb-history-from 2020-01-01(10年債の引け値の過去分を埋める。初回・抜けた日があるときに1回)

処理:
  ① historical_data.xlsx の Mid(日付×テナーのミッド)と Fixing(TONA実績)を読み、
     DBの quotes / fixings に取り込む(同じ日付は置き換え。過去に取り込んだ日付は残る)。
     Volシート(スワップションATMノーマルvol)があれば vol_quotes にも取り込む
     (分析ブックのPCA用。カーブには使わない)
  ② DBにある全日付について、historical_curve.toml で選んだテナーでカーブを構築し、
     暦日ごとのDFを curves に保存する(形式は historical_store.py)。
     - "O/N" はその日のTONA実績(フィキシング対象日=その日)を使う
     - その日に値が無いテナーは外し、あるテナーだけで引く(使用/不使用テナーを記録)
     - 日本の非営業日(ql.Japan)は作らない。LSEGには祝日にも一部テナーの値が
       入ることがある(実データで1テナーだけの日もあった)が、TONAも市場も無い日の
       カーブは意味を持たないため
     - 入力(構築方法・使うレート)のハッシュが前回と同じ日は作り直さない。
       テナー選択や構築方法を変えれば、該当する日が自動で作り直される
  ③ Neon(engine/common/neon_source.py)からASWスプレッド(JGB銘柄別)を差分取得し、
     asw_quotes に保存する(カーブには使わない参照データ。vol_quotesと同じ位置づけ)。
     Neonに繋がらない場合もここだけスキップし、①②の結果には影響しない。
  ④ LSEG(Python API、engine/common/jgb_source.py)からJGBの銘柄情報と引け値(大和、=DWAT)を取得し、
     jgb_bonds / jgb_closes に保存する(現物オプション BondOption.xlsm 用)。LSEG Workspaceが
     起動していない等で失敗しても、ここだけスキップする(③と同じ)。--skip-jgb で行わない。
     --jgb-history-from を付けたときは、10年債(先物の受渡適格銘柄の候補)の引け値の過去分も取る
     (大和、無い日はみずほ・JCLで補う。jgb_source.fetch_close_history)。
  ⑤ LSEG(engine/common/futures_source.py)から長期国債先物の限月ごとの清算値を取得し、fut_settles に
     保存する(2020年から。取り終えた限月は取らず、取引中の限月は保存済みの最終日から取り直す)。
     失敗してもここだけスキップする。--skip-jgb で④と一緒に行わない。

カーブの引き方は curve.CURVE_BUILDERS から名前で選ぶだけで、このバッチは引き方に依存しない。
"""

import argparse
import hashlib
import json
import sys
import tomllib
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

import openpyxl
import QuantLib as ql

from engine.common import futures_source, jgb_futures, jgb_source, neon_source
from engine.common.conventions import CALENDAR
from engine.common.curve import get_curve_builder
from engine.common.historical_store import GRID_DAYS, HistoricalStore, discount_grid
from engine.common.paths import HISTORICAL_DB_PATH, REPO_ROOT

DEFAULT_XLSX = REPO_ROOT / "historical_data.xlsx"
DEFAULT_CONFIG = REPO_ROOT / "historical_curve.toml"
ON_TENOR = "O/N"
_DATE_HEADER = "日付"
_VOL_HEADER = "満期x原資産"
VOL_SHEET = "Vol"
FUTURES_HISTORY_START = date(2020, 1, 1)  # 先物の清算値を取り始める日(ユーザー指示)


class CurveConfig(NamedTuple):
    builder: str
    tenors: List[str]


class BatchSummary(NamedTuple):
    built: List[date]
    unchanged: int
    holidays: List[date]  # 日本の非営業日のため作らなかった日
    with_missing: Dict[date, List[str]]  # テナーが欠けたまま構築した日
    failed: Dict[date, str]


def load_config(path: Path) -> CurveConfig:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    tenors = [str(t).strip().upper() for t in raw.get("tenors", [])]
    if not tenors:
        raise ValueError(f"{path}: tenors が空です")
    builder = str(raw.get("builder", "")).strip()
    get_curve_builder(builder)  # 未対応の名前はここでエラー
    return CurveConfig(builder=builder, tenors=tenors)


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


def read_vol_sheet(path: Path) -> Dict[date, Dict[str, float]]:
    """historical_data.xlsx の Volシート → {日付: {満期x原資産: ノーマルvol(bp)}}。
    シートが無い(古い雛形)ときは空。銘柄の表記はシートの見出しのまま(例 1Yx10Y)。"""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        if VOL_SHEET not in wb.sheetnames:
            return {}
        header, rows = _rows_after_date_header(wb[VOL_SHEET], _VOL_HEADER)
        labels = [str(h).strip() if h not in (None, "") else None for h in header[1:]]
        return _date_matrix(rows, labels)
    finally:
        wb.close()


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


def sync_asw_quotes(store: HistoricalStore, log=print) -> None:
    """Neonから差分のASWスプレッドを取得してasw_quotesに保存する。
    Neonに繋がらない・取得に失敗した場合もバッチ全体は止めず、ここだけスキップする。"""
    since = store.asw_quotes_max_updated_at()
    try:
        rows = neon_source.fetch_asw_quotes(since=since)
    except Exception as e:
        log(f"ASW取得: 失敗のためスキップ ({e})")
        return
    if rows:
        store.save_asw_quotes(rows)
    log(f"ASW取得: {len(rows)}行({'差分' if since else '全件'})")


def sync_jgbs(store: HistoricalStore, log=print) -> None:
    """LSEGからJGBの銘柄情報と直近の引け値を取得して保存する。失敗してもバッチ全体は止めない。"""
    try:
        bonds, closes = jgb_source.fetch_jgbs()
    except Exception as e:
        log(f"JGB取得: 失敗のためスキップ ({e})")
        return
    store.save_jgb_bonds(bonds)
    store.save_jgb_closes(closes)
    dates = sorted({c.as_of_date for c in closes})
    log(f"JGB取得: 銘柄 {len(bonds)} / 引け値 {len(closes)}({', '.join(str(d) for d in dates)})")


def sync_jgb_history(store: HistoricalStore, start: date, log=print, end: Optional[date] = None) -> None:
    """10年債(jgb_bonds のうち JB で start 以降に償還)の start〜end の引け値の過去分を保存する。"""
    end = end or date.today()
    bonds = [
        jgb_source.JgbRecord(r.code, r.ric, r.isin, r.coupon, r.maturity)
        for r in store.jgb_master(end)
        if r.code.startswith(jgb_futures.DELIVERABLE_PREFIX) and r.maturity >= start
    ]
    try:
        closes = jgb_source.fetch_close_history(bonds, start, end)
    except Exception as e:
        log(f"引け値の過去分: 失敗のためスキップ ({e})")
        return
    store.save_jgb_closes(closes)
    log(f"引け値の過去分: 10年債 {len(bonds)}銘柄 / {len(closes)}件({start}〜{end})")


def sync_futures(store: HistoricalStore, log=print, today: Optional[date] = None) -> None:
    """長期国債先物の限月ごとの清算値を差分で保存する。失敗してもバッチ全体は止めない。"""
    today = today or date.today()
    saved = store.fut_last_dates()
    plan: Dict[jgb_futures.Contract, date] = {}
    first = jgb_futures.front_contract(FUTURES_HISTORY_START)
    final = jgb_futures.front_contract(today).next().next()  # 取引中は期近から3限月
    for contract in jgb_futures.contracts_between(first, final):
        last = saved.get(contract.label)
        if last is not None and last >= jgb_futures.last_trade_date(contract):
            continue  # 取り終えた限月
        plan[contract] = last or FUTURES_HISTORY_START  # 保存済みの最終日から取り直す(清算値の訂正に備える)
    try:
        result = futures_source.fetch_settles(plan, today)
    except Exception as e:
        log(f"先物取得: 失敗のためスキップ ({e})")
        return
    store.save_fut_settles(result.settles)
    contracts = sorted({label for _, label, _ in result.settles})
    log(f"先物取得: 清算値 {len(result.settles)}件({', '.join(contracts)})")
    for w in result.warnings:
        log(f"  警告: {w}")


def run(
    xlsx_path: Path = DEFAULT_XLSX,
    config_path: Path = DEFAULT_CONFIG,
    db_path: Path = HISTORICAL_DB_PATH,
    rebuild: bool = False,
    log=print,
    skip_jgb: bool = False,
    jgb_history_from: Optional[date] = None,
) -> BatchSummary:
    config = load_config(config_path)
    builder = get_curve_builder(config.builder)
    store = HistoricalStore(db_path)

    mid, fixings = read_historical_workbook(xlsx_path)
    store.save_quotes(mid)
    store.save_fixings(fixings)
    vols = read_vol_sheet(xlsx_path)
    store.save_vol_quotes(vols)
    log(f"取り込み: Mid {len(mid)}日分 / TONA実績 {len(fixings)}日分 / Vol {len(vols)}日分")
    sync_asw_quotes(store, log)
    if not skip_jgb:
        sync_jgbs(store, log)
        if jgb_history_from is not None:
            sync_jgb_history(store, jgb_history_from, log)
        sync_futures(store, log)

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

    summary = BatchSummary(built, unchanged, holidays, with_missing, failed)
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
    return summary


def main(argv=None) -> int:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")  # NEON_CONNECTION_STRING(sync_asw_quotes用)

    parser = argparse.ArgumentParser(description="historical_data.xlsx からヒストリカル日次カーブDBを作る")
    parser.add_argument("--xlsx", type=Path, default=DEFAULT_XLSX)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=HISTORICAL_DB_PATH)
    parser.add_argument("--rebuild", action="store_true", help="入力が同じ日も含めて全日付を作り直す")
    parser.add_argument(
        "--skip-jgb", action="store_true", help="LSEGからのJGBの銘柄情報・引け値と先物の清算値の取得を行わない",
    )
    parser.add_argument(
        "--jgb-history-from", type=date.fromisoformat, metavar="YYYY-MM-DD",
        help="10年債の引け値の過去分をこの日から取る(初回・抜けた日を埋めるとき)",
    )
    args = parser.parse_args(argv)
    summary = run(
        args.xlsx, args.config, args.db, args.rebuild, skip_jgb=args.skip_jgb, jgb_history_from=args.jgb_history_from,
    )
    return 1 if summary.failed else 0


if __name__ == "__main__":
    sys.exit(main())
