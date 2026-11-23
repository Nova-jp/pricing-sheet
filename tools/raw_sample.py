"""
生シート(会社のファイルを貼るシート)の見本の値を、git管理のCSV(templates/raw_samples/)とブックの間で受け渡す。

    python -m tools.raw_sample load PositionSheet.xlsm   # CSV → ブックの生シート(A1から値だけ。空の生シートにだけ)
    python -m tools.raw_sample dump PositionSheet.xlsm   # ブックの生シート → CSV(差分を出してから上書き)

雛形(templates/<ブック>)の生シートは今までどおり空(tools/template.py の RECEIVE_AREAS)。見本は雛形とは別に持ち、
雛形から作ったブックで動きを確かめるときに load で貼る。CSVに置くのは作り物の値だけ(本物の会社のデータは置かない。
templates/raw_samples/README.md)。

CSVはシートのA1からの格子そのまま(1行目は会社のファイルの見出し)。セルの値の書き方:
- 空欄は空、数値はそのまま(整数は小数点なし)、文字列はそのまま
- 日付は 2026-11-27(時刻があれば 2026-11-27T15:00:00)。load で日付の書式のセルにする
- "=" で始まる文字列は数式として貼られてしまうので扱わない(dump・load とも止める)

Excelは非表示の別のインスタンスで、マクロ無効・再計算せずに保存する(開いているブックは扱わない)。
"""

import argparse
import csv
import datetime as dt
import difflib
import io
import os
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_DIR = ROOT / "templates" / "raw_samples"

# ブック → 見本を持つ生シート(シートの並び)
RAW_SHEETS: Dict[str, List[str]] = {
    "PositionSheet.xlsm": ["Raw_IR", "Raw_IRVOL", "Raw_Swaption", "Raw_BondOption", "Raw_JGB"],
}

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
_NUMBER = re.compile(r"^-?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$")
_EXCEL_EPOCH = dt.datetime(1899, 12, 30)
DATE_FORMAT = "yyyy-mm-dd"


class SampleError(Exception):
    pass


def cell_to_text(value) -> str:
    """Excelのセルの値(pywin32の .Value) → CSVの文字列。"""
    if value is None:
        return ""
    if isinstance(value, dt.datetime):
        value = value.replace(tzinfo=None)
        return value.date().isoformat() if value.time() == dt.time(0) else value.isoformat(timespec="seconds")
    if isinstance(value, bool):
        raise SampleError(f"真偽値は扱いません: {value!r}")
    if isinstance(value, (int, float)):
        return str(int(value)) if float(value).is_integer() else repr(float(value))
    text = str(value)
    if text.startswith("="):
        raise SampleError(f"'=' で始まる文字列は扱いません: {text!r}")
    return text


def text_to_cell(text: str):
    """CSVの文字列 → (Value2 に入れる値, 日付か)。空欄は (None, False)。"""
    if text == "":
        return None, False
    if _DATE.match(text) or _DATETIME.match(text):
        days = (dt.datetime.fromisoformat(text) - _EXCEL_EPOCH) / dt.timedelta(days=1)
        return days, True
    if _NUMBER.match(text):
        number = float(text)
        return (int(number) if number.is_integer() and "." not in text and "e" not in text.lower() else number), False
    if text.startswith("="):
        raise SampleError(f"'=' で始まる文字列は扱いません: {text!r}")
    return text, False


def grid_to_csv(grid: Sequence[Sequence]) -> str:
    rows = [[cell_to_text(v) for v in row] for row in grid]
    while rows and not any(rows[-1]):  # 末尾の空行は持たない
        rows.pop()
    width = max((max((i + 1 for i, t in enumerate(r) if t), default=0) for r in rows), default=0)
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    for row in rows:
        writer.writerow(row[:width] + [""] * (width - len(row)))
    return out.getvalue()


def csv_to_grid(text: str):
    """CSV → (Value2 の格子, 日付のセルの (行, 列) の一覧。0始まり)。行の長さは揃える。"""
    rows = list(csv.reader(io.StringIO(text)))
    width = max((len(r) for r in rows), default=0)
    grid, dates = [], []
    for i, row in enumerate(rows):
        values = []
        for j, cell in enumerate(row + [""] * (width - len(row))):
            value, is_date = text_to_cell(cell)
            values.append(value)
            if is_date:
                dates.append((i, j))
        grid.append(values)
    return grid, dates


def sample_path(book: str, sheet: str) -> Path:
    return SAMPLE_DIR / Path(book).stem / f"{sheet}.csv"


# --- Excel(非表示の別インスタンス) ---

def _excel():
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    xl = win32com.client.DispatchEx("Excel.Application")
    xl.Visible = False
    xl.DisplayAlerts = False
    xl.AutomationSecurity = 3  # マクロ無効(xlwingsのUDFを呼ばない)
    return xl


def _open(xl, path: Path, read_only: bool):
    if (path.parent / ("~$" + path.name)).exists():
        raise SampleError(f"開かれています(閉じてから実行): {path}")
    last = None
    for _ in range(3):  # 非表示のExcelの初回のOpenは失敗することがある
        try:
            return xl.Workbooks.Open(str(path), UpdateLinks=0, ReadOnly=read_only)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2)
    raise SampleError(f"開けませんでした: {path}: {last}")


def _sheets_for(path: Path) -> List[str]:
    if path.name not in RAW_SHEETS:
        raise SampleError(f"見本を持つブックではありません: {path.name}(RAW_SHEETS: {', '.join(RAW_SHEETS)})")
    return RAW_SHEETS[path.name]


def load(path: Path) -> None:
    sheets = _sheets_for(path)
    grids = {}
    for sheet in sheets:
        csv_path = sample_path(path.name, sheet)
        if not csv_path.exists():
            raise SampleError(f"見本がありません: {csv_path}")
        grids[sheet] = csv_to_grid(csv_path.read_text(encoding="utf-8"))
    xl = _excel()
    wb = None
    try:
        wb = _open(xl, path, read_only=False)
        xl.Calculation = -4135  # 手動
        xl.CalculateBeforeSave = False
        for sheet in sheets:
            if xl.WorksheetFunction.CountA(wb.Worksheets(sheet).Cells) != 0:
                raise SampleError(f"{sheet}: 生シートが空ではありません(貼り直すなら先に消す)")
        for sheet in sheets:
            ws = wb.Worksheets(sheet)
            grid, dates = grids[sheet]
            if not grid:
                continue
            target = ws.Range(ws.Cells(1, 1), ws.Cells(len(grid), len(grid[0])))
            target.Value2 = grid
            for i, j in dates:
                ws.Cells(i + 1, j + 1).NumberFormat = DATE_FORMAT
            print(f"{sheet}: {len(grid)}行 × {len(grid[0])}列(日付 {len(dates)}セル)")
        wb.Save()
        print("保存しました:", path)
    finally:
        if wb is not None:
            wb.Close(SaveChanges=False)
        xl.Quit()


def dump(path: Path) -> None:
    sheets = _sheets_for(path)
    xl = _excel()
    wb = None
    texts = {}
    try:
        wb = _open(xl, path, read_only=True)
        for sheet in sheets:
            ws = wb.Worksheets(sheet)
            if xl.WorksheetFunction.CountA(ws.Cells) == 0:  # 雛形から作ったばかりのブック等。見本を空で消さない
                raise SampleError(f"{sheet}: 生シートが空です(書き出すものがありません)")
            if ws.Cells.HasFormula is not False:
                raise SampleError(f"{sheet}: 数式があります(生シートは値だけの前提)")
            used = ws.UsedRange
            last = used.Cells(used.Rows.Count, used.Columns.Count)
            value = ws.Range(ws.Cells(1, 1), last).Value  # A1から(左上の空行・空列も格子に含める)
            grid = value if isinstance(value, tuple) else ((value,),)
            texts[sheet] = grid_to_csv(grid)
    finally:
        if wb is not None:
            wb.Close(SaveChanges=False)
        xl.Quit()
    for sheet in sheets:
        csv_path = sample_path(path.name, sheet)
        old = csv_path.read_text(encoding="utf-8") if csv_path.exists() else ""
        new = texts[sheet]
        if old == new:
            print(f"同じ: {csv_path.relative_to(ROOT)}")
            continue
        sys.stdout.writelines(difflib.unified_diff(
            old.splitlines(True), new.splitlines(True), f"a/{csv_path.relative_to(ROOT).as_posix()}",
            f"b/{csv_path.relative_to(ROOT).as_posix()}"))
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        csv_path.write_text(new, encoding="utf-8", newline="\n")
        print(f"書き出し: {csv_path.relative_to(ROOT)}")
    print("CSVに置くのは作り物の値だけ(本物の会社のデータが入っていないか、git diffで確かめてからコミットする)")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("command", choices=("load", "dump"))
    parser.add_argument("book", help="ブックのパス(例 PositionSheet.xlsm)")
    args = parser.parse_args(argv)
    path = Path(os.path.abspath(args.book))
    try:
        (load if args.command == "load" else dump)(path)
    except SampleError as e:
        print("NG", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
