"""
SwapAnalysis.xlsm に VolLevel シート(ボラの水準とFwdの水準の散布図による割高・割安)を組み立てる、
1回だけ使う構築スクリプト(Windows・Excel・xlwingsのCOM)。構成は docs/architecture.md の「VolLevel」。

    python -m tools.build_vol_level [ブックのパス] [--replace]

■ 何をするか
  - ブックのファイルを backups/ に写してから、Excelで開いた(開いていれば、そのブックに接続した)ブックに
    VolLevelシートを VolHV の後ろに足し、文字・数式(.Formula2)・名前(VLV_…)・入力規則・条件付き書式・
    散布図・列のグループを入れる。書くのは数式と入力欄の初期値だけで、UDFのセルを値で上書きしない。
  - 保存はしない(Excelで F9 → 確認してから手で保存し、python -m tools.template で雛形を作る)。
  - --replace: 既にある VolLevel シートと VLV_ の名前を消してから作り直す。

■ 中身は layout() に集める(Excel無しでテストできるように。tests/test_build_vol_level.py)
"""

import argparse
import shutil
import sys
from datetime import date
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
SHEET = "VolLevel"
AFTER = "VolHV"

PAIRS = ("1Yx10Y", "1Yx2Y", "10Yx10Y")  # Settings の初期値(B4:B6)
GRID_EXPIRIES = ("1M", "3M", "6M", "1Y", "2Y", "5Y", "10Y", "20Y")  # A36:A43
GRID_TAILS = ("1Y", "2Y", "3Y", "5Y", "7Y", "10Y", "15Y", "20Y")  # B35:I35
# カーブのテナーとフラグ(AP:AQ。FwdMonitor・BOJと同じ並び。0=使わない、空欄=O/N)
CURVE_TENORS = (
    ("O/N", None), ("1W", 1), ("1M", 1), ("2M", 1), ("3M", 1), ("4M", 1), ("5M", 1), ("6M", 1), ("7M", 1),
    ("8M", 1), ("9M", 1), ("10M", 1), ("11M", 1), ("12M", 1), ("15M", 1), ("18M", 1), ("21M", 1), ("24M", 1),
    ("36M", 1), ("4Y", 1), ("5Y", 1), ("6Y", 1), ("7Y", 1), ("8Y", 1), ("9Y", 1), ("10Y", 1), ("11Y", 1),
    ("12Y", 1), ("15Y", 1), ("20Y", 1), ("25Y", 1), ("30Y", 1), ("35Y", 1), ("40Y", 1), ("RD", 0), ("M1", 0),
    ("M2", 0), ("M3", 0), ("M4", 0), ("M5", 0), ("M6", 0), ("M7", 0), ("M8", 0), ("M9", 0),
)

# ペアごとの列(計算領域): [Date, Fwd, IV, Resid, Hist, Recent] の先頭列と、Summaryの列、図の点の列(X, Y)
BLOCK_COLS = ("BF", "BM", "BT")
SUMMARY_COLS = ("B", "C", "D")
POINT_COLS = (("CA", "CB"), ("CD", "CE"), ("CG", "CH"))

SUMMARY_ROWS = (  # (行, 見出し, 書式)
    (12, "Latest date (DB)", "yyyy-mm-dd"),
    (13, "IV (DB, bp)", "0.00"),
    (14, "Fwd (DB, %)", "0.000"),
    (15, "Residual (DB, bp)", "0.00"),
    (16, "Z (DB)", "0.00"),
    (17, "IV (Live, bp)", "0.00"),
    (18, "Fwd (Live, %)", "0.000"),
    (19, "Fit at live Fwd (bp)", "0.00"),
    (20, "Residual (Live, bp)", "0.00"),
    (21, "Z (Live)", "0.00"),
    (22, "Pctl of live residual", "0%"),
    (23, "Slope (bp vol per 1% Fwd)", "0.00"),
    (24, "Intercept (bp)", "0.00"),
    (25, "R2", "0.00"),
    (26, "Residual SD (bp)", "0.00"),
    (27, "Days", "0"),
)

KEEP = 'ISNUMBER(v)*(d>=IF(VLV_From="",0,VLV_From))'  # IVのある日、かつFrom以降


def _col(letters: str, offset: int) -> str:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    n += offset
    out = ""
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def _pair_cells(k: int) -> Dict[str, str]:
    """ペアk(0〜2)の計算領域の列・Summaryのセル(数式で使う絶対参照)。"""
    b = BLOCK_COLS[k]
    cols = {name: _col(b, i) for i, name in enumerate(("date", "fwd", "iv", "res", "hist", "rec"))}
    s = SUMMARY_COLS[k]
    return {
        "pair": f"$B${4 + k}",
        **{name: f"{c}4" for name, c in cols.items()},
        **{f"{name}#": f"${c}$4#" for name, c in cols.items()},
        **{f"s{row}": f"${s}${row}" for row, _, _ in SUMMARY_ROWS},
    }


def layout() -> Tuple[List[Tuple[str, object, str]], Dict[str, str]]:
    """
    シートの中身。
      cells: [(セル, 値または数式, 書式)](数式は'='で始まる。書式は空文字なら付けない)
      names: {名前: RefersTo}(ブックの名前。並びは定義する順で、後の名前は前の名前を使ってよい)
    """
    cells: List[Tuple[str, object, str]] = []

    def put(cell: str, value, fmt: str = "") -> None:
        cells.append((cell, value, fmt))

    # 取得モード・題
    put("A1", "Rates")
    put("B1", "Live")
    put("C1", "Updated (vol)")
    put("D1", '=LiveInfo("vol",$AA$3#)', "yyyy-mm-dd hh:mm:ss")
    put("E1", "Updated (swap)")
    put("F1", '=LiveInfo("swap",$AE$3#)', "yyyy-mm-dd hh:mm:ss")
    put("H1", "Live: reads the latest vols and rates on every Shift+F9. Fixed: keeps the ones last read on this "
              "sheet. Only the live points use them; history is from the DB (morning batch).")
    put("A2", "VolLevel (normal vol vs forward level: rich / cheap = residual of a linear fit)")

    # Settings
    put("A3", "Settings")
    for k in range(3):
        put(f"A{4 + k}", f"Pair {k + 1}")
        put(f"B{4 + k}", PAIRS[k])
    put("A7", "From")
    put("B7", None, "yyyy-mm-dd")
    put("A8", "Recent days (bd)")
    put("B8", 20)

    # Summary
    put("A11", "Summary")
    for row, label, _ in SUMMARY_ROWS:
        put(f"A{row}", label)
    for k in range(3):
        p = _pair_cells(k)
        s = SUMMARY_COLS[k]
        pair = p["pair"]

        def blank_or(expr: str) -> str:
            return f'=IF({pair}="","",{expr})'

        put(f"{s}11", blank_or(f"UPPER({pair})"))
        formulas = {
            12: f"TAKE({p['date#']},-1)",
            13: f"TAKE({p['iv#']},-1)",
            14: f"TAKE({p['fwd#']},-1)",
            15: f"TAKE({p['res#']},-1)",
            16: f"{p['s15']}/{p['s26']}",
            17: f"XLOOKUP({pair},INDEX(VLV_LiveVol,,1),INDEX(VLV_LiveVol,,3),NA())",
            18: f"INDEX($BD$4#,{k + 1})",
            19: f"{p['s23']}*{p['s18']}+{p['s24']}",
            20: f"{p['s17']}-{p['s19']}",
            21: f"{p['s20']}/{p['s26']}",
            22: f"SUM(--({p['res#']}<={p['s20']}))/ROWS({p['res#']})",
            23: f"SLOPE({p['iv#']},{p['fwd#']})",
            24: f"INTERCEPT({p['iv#']},{p['fwd#']})",
            25: f"RSQ({p['iv#']},{p['fwd#']})",
            26: f"STDEV.S({p['res#']})",
            27: f"ROWS({p['iv#']})",
        }
        for row, _, fmt in SUMMARY_ROWS:
            put(f"{s}{row}", blank_or(formulas[row]), fmt)

    # Rich / cheap map
    put("A31", "Rich / cheap map (each option against its own fit; red = rich, blue = cheap)")
    put("A32", "Grid value")
    put("B32", "Z")
    put("A33", "Grid point")
    put("B33", "Live")
    put("A35", "Expiry \\ Tail")
    for i, e in enumerate(GRID_EXPIRIES):
        put(f"A{36 + i}", e)
    for j, t in enumerate(GRID_TAILS):
        put(f"{_col('B', j)}35", t)
    put("B36", "=MAKEARRAY(ROWS($A$36:$A$43),COLUMNS($B$35:$I$35),"
               "LAMBDA(i,j,VLV_Cell(INDEX($A$36:$A$43,i),INDEX($B$35:$I$35,j))))", "0.00")

    # 計算領域: ライブのvol・レート、カーブ入力、テナー表
    put("AA2", "Live vol (Name / RIC / Value)")
    put("AA3", '=IF($B$1="Fixed",LiveSnapshot("vol"),LiveQuotes("vol"))')
    put("AE2", "Live rates (Ticker / Value / Start / End)")
    put("AE3", '=IF($B$1="Fixed",LiveSnapshot("swap"),LiveQuotes("swap"))')
    put("AJ2", "Curve inputs (Flag / Ticker / Value / Start / End)")
    put("AJ3", "=LET(q,$AE$3#,t,$AP$4:$AP$47,"
               "HSTACK($AQ$4:$AQ$47,t,INDEX(q,XMATCH(t,INDEX(q,,1)),{2,3,4})))")
    put("AP2", "Curve tenors (Flag: 1 or blank = use, 0 = exclude)")
    put("AP3", "Ticker")
    put("AQ3", "Flag")
    for i, (tenor, flag) in enumerate(CURVE_TENORS):
        put(f"AP{4 + i}", tenor)
        put(f"AQ{4 + i}", flag)

    # 計算領域: ペアのヒストリカルとライブのFwd
    put("AS2", "VolHistory of the pairs (Date / IV / Fwd / dF)")
    put("AS3", "=VolHistory($B$4:$B$6)")
    put("BD2", "Fwd (Live, %)")
    put("BD4", "=VolFwdLive($B$4:$B$6,TAKE($AJ$3#,,3))")

    # 計算領域: ペアごとの [Date, Fwd, IV, Resid, Hist, Recent](From以降でIVのある日)
    for k in range(3):
        p = _pair_cells(k)
        pair = p["pair"]
        put(f"{BLOCK_COLS[k]}2", f"Pair {k + 1} (From onwards, days with IV)")
        for name, head in (("date", "Date"), ("fwd", "Fwd"), ("iv", "IV"), ("res", "Resid"),
                           ("hist", "Hist"), ("rec", "Recent")):
            put(f"{p[name][:-1]}3", head)

        def filt(expr: str) -> str:
            return (f'=IF({pair}="",NA(),LET(d,VLV_Date,v,VLV_Col({pair},"IV"),'
                    f"FILTER({expr},{KEEP})))")

        put(p["date"], filt("d"), "yyyy-mm-dd")
        put(p["fwd"], filt(f'VLV_Col({pair},"Fwd")'), "0.000")
        put(p["iv"], filt("v"), "0.00")
        put(p["res"], f'=IF({pair}="",NA(),{p["iv#"]}-({p["s23"]}*{p["fwd#"]}+{p["s24"]}))', "0.00")
        put(p["hist"], f"=LET(n,ROWS({p['iv#']}),IF(SEQUENCE(n)<=n-VLV_Recent,{p['iv#']},NA()))", "0.00")
        put(p["rec"], f"=LET(n,ROWS({p['iv#']}),IF(SEQUENCE(n)>n-VLV_Recent,{p['iv#']},NA()))", "0.00")

    # 計算領域: 図の点(4行=ライブ、5行=DBの最新、6・7行=回帰直線の両端。Fwdの範囲はライブを含む)
    for k in range(3):
        p = _pair_cells(k)
        pair = p["pair"]
        x, y = POINT_COLS[k]
        put(f"{x}2", f"Pair {k + 1} points")
        put(f"{x}3", "Fwd")
        put(f"{y}3", "IV")
        put(f"{x}4", f'=IF({pair}="",NA(),IFERROR({p["s18"]}*1,NA()))', "0.000")
        put(f"{y}4", f'=IF({pair}="",NA(),IFERROR({p["s17"]}*1,NA()))', "0.00")
        put(f"{x}5", f'=IF({pair}="",NA(),{p["s14"]})', "0.000")
        put(f"{y}5", f'=IF({pair}="",NA(),{p["s13"]})', "0.00")
        for row, fn in ((6, "MIN"), (7, "MAX")):
            put(f"{x}{row}", f'=IF({pair}="",NA(),LET(x,{p["fwd#"]},l,IFERROR({p["s18"]}*1,{fn}(x)),'
                             f"{fn}({fn}(x),l)))", "0.000")
            put(f"{y}{row}", f"={p['s23']}*{x}{row}+{p['s24']}", "0.00")

    # 計算領域: マップの銘柄・そのライブのFwd・そのヒストリカル
    put("CK2", "Map options / Fwd (Live, %)")
    put("CK4", '=TOCOL(IF(($A$36:$A$43<>"")*($B$35:$I$35<>""),$A$36:$A$43&"x"&$B$35:$I$35,NA()),2)')
    put("CL4", "=VolFwdLive($CK$4#,TAKE($AJ$3#,,3))", "0.000")
    put("CN2", "VolHistory of the map options")
    put("CN3", "=VolHistory($CK$4#)")

    names: Dict[str, str] = {
        "VLV_From": f"={SHEET}!$B$7",
        "VLV_Recent": f"={SHEET}!$B$8",
        "VLV_GValue": f"={SHEET}!$B$32",
        "VLV_GPoint": f"={SHEET}!$B$33",
        "VLV_LiveVol": f"={SHEET}!$AA$3#",
        "VLV_Data": f"={SHEET}!$AS$3#",
        "VLV_Date": "=DROP(TAKE(VLV_Data,,1),1)",
        "VLV_GNames": f"={SHEET}!$CK$4#",
        "VLV_GFwdLive": f"={SHEET}!$CL$4#",
        "VLV_GData": f"={SHEET}!$CN$3#",
    }
    for k in range(3):  # 図の系列が引く列
        p = _pair_cells(k)
        for name, key in (("Fwd", "fwd#"), ("Hist", "hist#"), ("Rec", "rec#")):
            names[f"VLV_{name}{k + 1}"] = f"={SHEET}!{p[key]}"
    names["VLV_Col"] = "=LAMBDA(n,f,DROP(CHOOSECOLS(VLV_Data,XMATCH(n&\" \"&f,TAKE(VLV_Data,1))),1))"
    names["VLV_Cell"] = (
        '=LAMBDA(e,t,IF(OR(e="",t=""),"",IFERROR(LET(n,e&"x"&t,h,TAKE(VLV_GData,1),'
        "d,DROP(TAKE(VLV_GData,,1),1),"
        'f,DROP(CHOOSECOLS(VLV_GData,XMATCH(n&" Fwd",h)),1),'
        'v,DROP(CHOOSECOLS(VLV_GData,XMATCH(n&" IV",h)),1),'
        f"k,{KEEP},x,FILTER(f,k),y,FILTER(v,k),"
        "b,SLOPE(y,x),a,INTERCEPT(y,x),rs,y-(a+b*x),s,STDEV.S(rs),"
        'pv,IF(VLV_GPoint="Live",XLOOKUP(n,INDEX(VLV_LiveVol,,1),INDEX(VLV_LiveVol,,3)),TAKE(y,-1)),'
        'pf,IF(VLV_GPoint="Live",XLOOKUP(n,VLV_GNames,VLV_GFwdLive),TAKE(x,-1)),'
        "z,pv-(a+b*pf),"
        'SWITCH(VLV_GValue,"Z",z/s,"Residual",z,"Pctl",SUM(--(rs<=z))/ROWS(rs))),"n/a")))'
    )
    return cells, names


# 入力規則: (範囲, 一覧(Noneなら入力時の説明だけ), 説明の題, 説明)
VALIDATIONS = (
    ("B1", "Live,Fixed", "", ""),
    ("B4:B6", None, "Pair", "Expiry x tail, e.g. 1Yx10Y (grid of the vol DB). Leave blank to drop the pair."),
    ("B7", None, "From", "Start of the fit and the charts. Blank = all dates in the DB (from 2024-10-28)."),
    ("B8", None, "Recent days", "The last n days (bd) are drawn in orange, to see whether the cloud just "
                                "follows time (the hiking cycle)."),
    ("B32", "Z,Residual,Pctl", "Grid value", "Z = residual / SD of the residuals (+-2 = full colour). "
                                             "Residual in bp (+-5). Pctl = share of days at or below (0-1)."),
    ("B33", "Live,DB", "Grid point", "Live = today's live vol and Fwd. DB = the latest day in the DB."),
)

# 色(Excelの色は BGR の整数)
def _rgb(r: int, g: int, b: int) -> int:
    return r + g * 256 + b * 65536


GRAY, ORANGE, BLACK, RED, BLUE = (_rgb(170, 170, 170), _rgb(237, 125, 49), _rgb(0, 0, 0),
                                  _rgb(220, 30, 30), _rgb(68, 114, 196))
SCALE_LOW, SCALE_MID, SCALE_HIGH = _rgb(90, 138, 198), _rgb(255, 255, 255), _rgb(248, 105, 107)
SCALE = (  # 条件付き書式の3色(Grid value ごとの最小・中央・最大)
    (SCALE_LOW, '=IF(VLV_GValue="Pctl",0,IF(VLV_GValue="Residual",-5,-2))'),
    (SCALE_MID, '=IF(VLV_GValue="Pctl",0.5,0)'),
    (SCALE_HIGH, '=IF(VLV_GValue="Pctl",1,IF(VLV_GValue="Residual",5,2))'),
)

# xlwings(COM)の定数
XL_XY_SCATTER = -4169
XL_XY_SCATTER_LINES_NO_MARKERS = 75
XL_MARKER_CIRCLE = 8
XL_MARKER_NONE = -4142
XL_VALIDATE_INPUT_ONLY = 0
XL_VALIDATE_LIST = 3
XL_CONDITION_VALUE_FORMULA = 4
XL_FREE_FLOATING = 3


def _series(chart, book_name: str, name: str, x: str, y: str, color: int, size: int, line: bool = False):
    s = chart.SeriesCollection().NewSeries()
    s.Name = name
    try:
        s.XValues = x if x.startswith("=") else f"='{book_name}'!{x}"
        s.Values = y if y.startswith("=") else f"='{book_name}'!{y}"
    except Exception as exc:  # noqa: BLE001 - 名前がまだエラーの値だと受け付けないことがある
        print(f"注意: 図の系列 {name} の範囲を設定できませんでした({exc})。F9の後に手で X={x} Y={y} を指定してください")
    if line:
        s.ChartType = XL_XY_SCATTER_LINES_NO_MARKERS
        s.MarkerStyle = XL_MARKER_NONE
        s.Format.Line.ForeColor.RGB = color
        s.Format.Line.Weight = 1.5
    else:
        s.Format.Line.Visible = False
        s.MarkerStyle = XL_MARKER_CIRCLE
        s.MarkerSize = size
        s.MarkerForegroundColor = color
        s.MarkerBackgroundColor = color
    return s


def _add_charts(ws, book_name: str) -> None:
    anchor = ws.api.Range("F3")
    width, height, gap = 440, 330, 10
    for k in range(3):
        x, y = POINT_COLS[k]
        obj = ws.api.ChartObjects().Add(anchor.Left + k * (width + gap), anchor.Top, width, height)
        obj.Name = f"VLV_Chart{k + 1}"
        obj.Placement = XL_FREE_FLOATING
        chart = obj.Chart
        chart.ChartType = XL_XY_SCATTER
        while chart.SeriesCollection().Count:
            chart.SeriesCollection(1).Delete()
        _series(chart, book_name, "History", f"VLV_Fwd{k + 1}", f"VLV_Hist{k + 1}", GRAY, 3)
        _series(chart, book_name, "Recent", f"VLV_Fwd{k + 1}", f"VLV_Rec{k + 1}", ORANGE, 4)
        _series(chart, book_name, "Latest (DB)", f"='{SHEET}'!${x}$5", f"='{SHEET}'!${y}$5", BLACK, 7)
        _series(chart, book_name, "Live", f"='{SHEET}'!${x}$4", f"='{SHEET}'!${y}$4", RED, 8)
        _series(chart, book_name, "Fit", f"='{SHEET}'!${x}$6:${x}$7", f"='{SHEET}'!${y}$6:${y}$7", BLACK, 0,
                line=True)
        chart.PlotVisibleOnly = False  # 元データは畳んだ列にある(「非表示の行と列のデータを表示」)
        chart.HasTitle = True
        chart.ChartTitle.Text = f"Pair {k + 1}"
        try:  # 題をペアの名前のセルにつなぐ(できなければ固定の文字のまま)
            chart.ChartTitle.Formula = f"='{SHEET}'!${SUMMARY_COLS[k]}$11"
        except Exception:  # noqa: BLE001 - Excelの版による
            pass
        chart.HasLegend = True
        for axis, title in ((1, "Fwd (%)"), (2, "IV (bp/yr)")):
            ax = chart.Axes(axis)
            ax.HasTitle = True
            ax.AxisTitle.Text = title


def _add_color_scale(rng) -> None:
    fc = rng.FormatConditions.AddColorScale(3)
    for i, (color, formula) in enumerate(SCALE, start=1):
        try:
            crit = fc.ColorScaleCriteria(i)
        except TypeError:  # COMの呼び出し方の違い
            crit = fc.ColorScaleCriteria.Item(i)
        crit.Type = XL_CONDITION_VALUE_FORMULA
        crit.Value = formula
        crit.FormatColor.Color = color


def build(book_path: Path, replace: bool = False) -> None:
    import xlwings as xw  # Windowsだけで使う(テストは layout() だけを見る)

    backup_dir = ROOT / "backups"
    backup_dir.mkdir(exist_ok=True)
    backup = backup_dir / f"{book_path.stem}_backup_{date.today():%Y%m%d}_before_vol_level{book_path.suffix}"
    shutil.copy2(book_path, backup)
    print(f"バックアップ: {backup}")

    book = xw.Book(str(book_path))
    print(f"ブック: {book.fullname}")
    if SHEET in [s.name for s in book.sheets]:
        if not replace:
            raise SystemExit(f"{SHEET}シートが既にあります(作り直すなら --replace)")
        book.app.display_alerts = False
        try:
            book.sheets[SHEET].delete()
        finally:
            book.app.display_alerts = True
    for n in list(book.api.Names):
        if n.Name.split("!")[-1].startswith("VLV_"):
            n.Delete()

    ws = book.sheets.add(SHEET, after=book.sheets[AFTER])
    cells, names = layout()
    for name, refers_to in names.items():  # 数式より先に名前を作る(LAMBDAは前の名前を使う)
        book.api.Names.Add(name, refers_to)
    for cell, value, fmt in cells:
        rng = ws.api.Range(cell)
        if fmt:
            rng.NumberFormat = fmt
        if isinstance(value, str) and value.startswith("="):
            rng.Formula2 = value  # .Formula だと旧式の配列数式になり、スピルしない
        elif value is not None:
            rng.Value = value

    for address, items, title, message in VALIDATIONS:
        v = ws.api.Range(address).Validation
        v.Delete()
        if items:
            v.Add(XL_VALIDATE_LIST, 1, 1, items)
        else:
            v.Add(XL_VALIDATE_INPUT_ONLY)
        if title:
            v.InputTitle = title
            v.InputMessage = message
    _add_color_scale(ws.api.Range("B36:I43"))

    ws.api.Range("A2").Font.Bold = True
    for address in ("A3", "A11", "A31", "B11:D11"):
        ws.api.Range(address).Font.Bold = True
    ws.api.Columns("A").ColumnWidth = 26
    ws.api.Columns("B:D").ColumnWidth = 12
    ws.api.Columns("E").ColumnWidth = 3
    try:  # 図の系列に渡す名前(スピルの範囲)を一度計算しておく(UDFサーバ・フィードが止まっていても続ける)
        ws.api.Calculate()
    except Exception as exc:  # noqa: BLE001
        print(f"注意: シートの計算に失敗しました({exc})。図を作った後にF9で計算してください")
    _add_charts(ws, book.name)

    ws.api.Columns("AA:LZ").Group()
    ws.api.Outline.ShowLevels(0, 1)
    ws.activate()
    print(f"{SHEET}シートを作りました。F9(初回は Ctrl+Alt+F9)で計算し、確かめてから保存してください(未保存)。")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("book", nargs="?", default=str(ROOT / "SwapAnalysis.xlsm"))
    parser.add_argument("--replace", action="store_true", help="既にあるVolLevelシートを作り直す")
    args = parser.parse_args(argv)
    path = Path(args.book).resolve()
    if not path.exists():
        print(f"ブックがありません: {path}", file=sys.stderr)
        return 1
    build(path, replace=args.replace)
    return 0


if __name__ == "__main__":
    sys.exit(main())
