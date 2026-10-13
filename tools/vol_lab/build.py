"""
VolLab.xlsxの構成(シート・数式・グラフ・設定)をopenpyxlで組み立てる(データは入れない)。

    python -m tools.vol_lab --build   # 直下にVolLab.xlsxを作る(既にあれば --force が必要)

作ったブックから python -m tools.template で templates/VolLab.xlsx(雛形)を作ってコミットする。
たたき台の段階では、構成の変更はこのファイルを直して作り直す(Excelで直接いじった構成は
作り直すと消える。設定値の実験はExcel上で自由にしてよい)。

■ 数式の約束
  - Excel 2007以前からある関数だけを使う(openpyxlで書くと新しい関数は _xlfn. の接頭辞が要るため)。
  - 配列の計算(母集団の平均・標準偏差・パーセンタイル等)は旧式の配列数式(CSE、1セル)にする。
    スピル(動的配列)は使わない(openpyxlでは正しく書けない)。
  - 日付ごとの表は最大 MAX_ROWS 行まで数式を入れておく(データが短ければ空欄、グラフ用の列は
    #N/A にしてグラフに出さない)。グラフは散布図(x=日付)にして、#N/Aの点を無視させる
    (データの長さが変わってもグラフの範囲を直さなくてよい)。
  - 銘柄の番号 j は grid.py と同じ((満期の番号-1)*12+原資産の番号)。
"""

from datetime import date
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, Reference, ScatterChart, Series
from openpyxl.chart.data_source import StrRef
from openpyxl.chart.marker import Marker
from openpyxl.chart.series import SeriesLabel
from openpyxl.chart.shapes import GraphicalProperties
from openpyxl.drawing.line import LineProperties
from openpyxl.formatting.rule import ColorScaleRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.workbook.defined_name import DefinedName
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.formula import ArrayFormula

from tools.vol_lab.grid import EXPIRIES, LABELS, TAILS, TRACK_EXPIRIES, TRACK_LABELS, months
from tools.vol_lab.meetings import MEETING_DATES

MAX_ROWS = 3000  # 日付の上限(約12年)。超えたら設定シートに警告を出す
DATA_FIRST = 3  # データシートのデータの開始行(fill.FIRST_ROW と同じ)
DATA_LAST = DATA_FIRST + MAX_ROWS - 1

S_README = "説明"
S_SET = "設定"
S1 = "①IVvsRV"
S2 = "②マトリックス"
S3 = "③ボラコーン"
S4 = "④ブレークイーブン"
S5 = "⑤事後VRP"
S6 = "⑥期間構造"
S7 = "⑦期待仮説"
S8 = "⑧会合イベント"
S9 = "⑨GARCH"
S_MEET = "会合日"
D_IV, D_DF, D_CUM, D_FWD, D_DAYS, D_TRACK = "IV", "dF", "dF2累積", "Fwd", "残存", "追跡"
DATA_SHEETS = (D_IV, D_DF, D_CUM, D_FWD, D_DAYS, D_TRACK)

DATE_FMT = "yyyy-mm-dd"
BOLD = Font(bold=True)
TITLE = Font(bold=True, size=14)
NOTE = Font(color="595959", size=9)
HEAD_FILL = PatternFill("solid", fgColor="D9E1F2")
INPUT_FILL = PatternFill("solid", fgColor="FFF2CC")

N = "年営業日数"


def q(sheet: str) -> str:
    return f"'{sheet}'"


def col(n: int) -> str:
    return get_column_letter(n)


# ---------------------------------------------------------------- 共通の部品

class Book:
    def __init__(self) -> None:
        self.wb = Workbook()
        self.wb.remove(self.wb.active)

    def sheet(self, name: str):
        return self.wb.create_sheet(name)

    def name(self, name: str, ref: str) -> None:
        self.wb.defined_names[name] = DefinedName(name, attr_text=ref)


def title(ws, text: str, note: Optional[str] = None) -> None:
    ws["A1"] = text
    ws["A1"].font = TITLE
    if note:
        ws["A2"] = note
        ws["A2"].font = NOTE


def header(ws, row: int, first_col: int, names: Sequence[str]) -> None:
    for n, text in enumerate(names):
        c = ws.cell(row=row, column=first_col + n, value=text)
        c.font = BOLD
        c.fill = HEAD_FILL
        c.alignment = Alignment(horizontal="center", wrap_text=True)


def notes(ws, cell: str, lines: Iterable[str]) -> None:
    c = ws[cell]
    for n, line in enumerate(lines):
        target = ws.cell(row=c.row + n, column=c.column, value=line)
        target.font = NOTE


def fmt(ws, ref: str, number_format: str) -> None:
    for row in ws[ref]:
        for c in row:
            c.number_format = number_format


def widths(ws, mapping: dict) -> None:
    for letter, w in mapping.items():
        ws.column_dimensions[letter].width = w


def list_validation(ws, cells: str, formula: str) -> None:
    dv = DataValidation(type="list", formula1=formula, allow_blank=True)
    ws.add_data_validation(dv)
    dv.add(cells)


def series(ws_title: str, x: Optional[Tuple[int, int, int]], y: Tuple[int, int, int], label_cell: str,
           *, markers: bool = False, color: Optional[str] = None, dash: Optional[str] = None,
           width: int = 15875, sheet=None) -> Series:
    """x, y: (列, 開始行, 終了行)。label_cell: 系列名のセル(例 '$C$45')。"""
    yref = Reference(sheet, min_col=y[0], min_row=y[1], max_row=y[2])
    xref = Reference(sheet, min_col=x[0], min_row=x[1], max_row=x[2]) if x else None
    s = Series(yref, xref) if xref is not None else Series(yref)
    s.tx = SeriesLabel(strRef=StrRef(f"{q(ws_title)}!{label_cell}"))
    if markers:
        s.marker = Marker(symbol="circle", size=4)
        if color:
            s.marker.graphicalProperties = GraphicalProperties(solidFill=color, ln=LineProperties(solidFill=color))
        s.graphicalProperties = GraphicalProperties(ln=LineProperties(noFill=True))
    else:
        s.marker = Marker(symbol="none")
        s.graphicalProperties = GraphicalProperties(ln=LineProperties(solidFill=color, w=width, prstDash=dash))
    s.smooth = False
    return s


def time_chart(chart_title: str, y_title: str, width: float = 30, height: float = 11) -> ScatterChart:
    ch = ScatterChart()
    ch.title = chart_title
    ch.style = 2
    ch.width, ch.height = width, height
    ch.x_axis.number_format = "yy/mm"
    ch.x_axis.majorGridlines = None
    ch.x_axis.tickLblPos = "low"
    ch.x_axis.delete = False
    ch.y_axis.delete = False
    ch.y_axis.title = y_title
    ch.legend.position = "b"
    return ch


def category_chart(chart_title: str, y_title: str, width: float = 20, height: float = 10) -> LineChart:
    ch = LineChart()
    ch.title = chart_title
    ch.style = 2
    ch.width, ch.height = width, height
    ch.x_axis.delete = False
    ch.y_axis.delete = False
    ch.y_axis.title = y_title
    ch.legend.position = "b"
    return ch


# よく使う式の部品(行の番号 i を入れたセルを渡す)
def data_at(name: str, i: str, j: str) -> str:
    """データシートの値。空欄は""(INDEXは空欄を0で返すため確かめる)。"""
    return f'IF({i}>件数,"",IF(INDEX({name},{i},{j})="","",INDEX({name},{i},{j})))'


def rv_from_cum(i_expr: str, j: str, window: str) -> str:
    """累積の差から求める窓windowのRV(bp/年)。i_expr行で終わる窓。"""
    return (f"SQRT({N}*(INDEX(累積データ,{i_expr},{j})-INDEX(累積データ,{i_expr}-{window},{j}))/{window})")


# ---------------------------------------------------------------- 説明・設定

def build_readme(book: Book) -> None:
    ws = book.sheet(S_README)
    title(ws, "ボラ分析のたたき台(VolLab)")
    lines = [
        "■ 使い方",
        "  1. 朝バッチ(python -m engine.common.morning_batch)でDBを更新する",
        "  2. ブックを閉じた状態で python -m tools.vol_lab を実行する(DBのデータをデータシートに書き込む。約1〜2分)",
        "  3. ブックを開く(開くと全体が再計算される)。設定シートの黄色いセルを変えて試す",
        "",
        "■ 用語",
        "  IV  : スワップションのATMノーマルvol(bp/年、LSEG計算系列)",
        "  RV  : 実現ボラ。Fwdスワップレートの日次変化(bp)の二乗平均(平均はゼロとする)を年率化 = √(年営業日数 × Σ変化² / 日数)",
        "  Fwd : 満期 x 原資産のオプションの原資産(評価日のスポット日+満期 を起算日とする原資産年数のOIS)",
        "        1日の変化は「前日と当日で同じ起算日・満期日のFwd」の差(ロールダウンを混ぜない)",
        "  (B) 満期一定型 : 毎日起算日を付け直す(1Mx10Yという区画の今のボラ)。①②③④⑥⑦⑧⑨で使う",
        "  (A) オプション追跡型 : 取引日に決めた起算日・満期日を満期まで固定(そのオプションを持った人が経験したボラ)。⑤で使う",
        "",
        "■ シート",
        "  ① IVvsRV       : 注目銘柄のIVと、複数の窓のRV・EWMAの推移。IV−RVのzスコア・パーセンタイル",
        "  ② マトリックス   : 基準日の満期x原資産の格子(IV/RV/IV−RV/zスコア等を切り替え、色付け)。割高・割安の順位",
        "  ③ ボラコーン     : 窓の長さごとのRVの分布(最小〜最大)に、満期をそろえたIVを重ねる(Burghardt-Lane)",
        "  ④ ブレークイーブン : 1日の織り込み値幅(前日のIV/√年営業日数)と実際の変動、仮想のデルタヘッジ・ストラドルの累積損益",
        "  ⑤ 事後VRP      : 取引日のIVと、その後満期までに実際に実現したボラ(A方式)の差=ボラ・リスクプレミアム",
        "  ⑥ 期間構造      : IVの期間構造、総分散、隣り合う満期の間のフォワードボラ、ロールダウン",
        "  ⑦ 期待仮説      : 過去に織り込まれたフォワードボラが、実際にその時が来たときのIVを当てたか(Campa-Chang型)",
        "  ⑧ 会合イベント   : 会合をまたぐ満期の総分散から、会合1回分の織り込み変動を推定(試験的)",
        "  ⑨ GARCH       : GARCH(1,1)を手で動かす。ソルバーで最尤推定し、満期までの予測ボラをIVと比べる",
        "  会合日         : 日銀会合の決定公表日(会合の最終日)。足りなければ下に追加する",
        "  IV〜追跡       : データシート(python -m tools.vol_lab が3行目以降を書き換える。手で編集しない)",
        "",
        "■ 注意",
        "  - 年営業日数(既定252)で年率化する。日本の営業日は年245日前後なので、√(252/245)≒1.4%ほどRVが大きめに出る",
        "  - IVの年率の時間軸(暦日/営業日)は設定で切り替える(⑥⑦⑧のフォワード分散の計算に効く)",
        "  - 母集団(パーセンタイル・zスコアの比較対象の期間)は設定の母集団開始・終了。空欄なら全期間〜基準日",
        "  - フォワードボラは、満期ごとに原資産のFwdが違う(1Mx10Yと3Mx10Yは別のFwd)ことを無視した近似",
        "  - これはたたき台。本番(SwapAnalysis)に入れるときはUDFとして作り直す",
    ]
    for n, line in enumerate(lines, start=3):
        ws.cell(row=n, column=1, value=line)
    widths(ws, {"A": 140})


SETTINGS: List[Tuple[str, str, object, str, Optional[str]]] = [
    # (名前, 表示, 既定値, 説明, 表示形式)
    (N, "年営業日数", 252, "RV・EWMA・ブレークイーブンの年率化に使う", "0"),
    ("比較窓", "比較窓(営業日)", 21, "IV−RV・zスコア・マトリックスで使うRVの窓", "0"),
    ("RV窓1", "RV窓1", 10, "①に表示する窓", "0"),
    ("RV窓2", "RV窓2", 21, "", "0"),
    ("RV窓3", "RV窓3", 63, "", "0"),
    ("RV窓4", "RV窓4", 126, "", "0"),
    ("RV窓5", "RV窓5", 252, "", "0"),
    ("EWMA係数", "EWMAのλ", 0.94, "RiskMetricsの日次の標準は0.94(実効的な記憶は約1/(1−λ)=17日)", "0.00"),
    ("注目満期", "注目銘柄の満期", "1Y", "①③④⑤⑥⑦⑧⑨の対象(③⑥⑦⑧は原資産だけを使う)", None),
    ("注目原資産", "注目銘柄の原資産", "10Y", "", None),
    ("基準日", "基準日", None, "空欄=最新日。②③⑥⑧⑨の「今日」", DATE_FMT),
    ("比較日", "比較日", None, "任意。②⑥でIVの変化を見る", DATE_FMT),
    ("母集団開始", "母集団の開始日", None, "空欄=データの最初。パーセンタイル・zスコア・推定の対象期間", DATE_FMT),
    ("母集団終了", "母集団の終了日", None, "空欄=基準日", DATE_FMT),
    ("表示開始日", "グラフの表示開始日", None, "空欄=データの最初", DATE_FMT),
    ("IV時間軸", "IVの年率の時間軸", "暦日", "暦日=残存暦日/365、営業日=残存営業日/年営業日数(⑥⑦⑧)", None),
    ("ロール月数", "ロールダウンの期間(月)", 1, "⑥: この期間が過ぎたとき、表面が変わらなければIVはいくらか", "0"),
    ("表示_RV窓1", "①に表示: RV窓1", 0, "1=表示、0=非表示", "0"),
    ("表示_RV窓2", "①に表示: RV窓2", 1, "", "0"),
    ("表示_RV窓3", "①に表示: RV窓3", 1, "", "0"),
    ("表示_RV窓4", "①に表示: RV窓4", 0, "", "0"),
    ("表示_RV窓5", "①に表示: RV窓5", 0, "", "0"),
    ("表示_EWMA", "①に表示: EWMA", 1, "", "0"),
    ("表示_会合除く", "①に表示: 会合日を除くRV(比較窓)", 0, "", "0"),
    ("表示_比較窓", "①に表示: RV(比較窓)", 0, "", "0"),
]

DERIVED: List[Tuple[str, str, str, Optional[str]]] = [
    ("件数", "データの日数", f"COUNT(日付)", "0"),
    ("最新日", "データの最新日", 'IF(件数=0,"",INDEX(日付,件数))', DATE_FMT),
    ("基準行", "基準日の行", "IF(件数=0,1,IF(基準日=\"\",件数,MATCH(基準日,日付有効,1)))", "0"),
    ("実効基準日", "基準日(実際の日付)", 'IF(件数=0,"",INDEX(日付,基準行))', DATE_FMT),
    ("比較行", "比較日の行", 'IF(比較日="","",MATCH(比較日,日付有効,1))', "0"),
    ("注目満期番号", "注目満期の番号", "MATCH(注目満期,満期一覧,0)", "0"),
    ("注目原資産番号", "注目原資産の番号", "MATCH(注目原資産,原資産一覧,0)", "0"),
    ("注目番号", "注目銘柄の番号 j", "(注目満期番号-1)*12+注目原資産番号", "0"),
    ("注目銘柄", "注目銘柄", '注目満期&"x"&注目原資産', None),
    ("注目満期月数", "注目満期の月数", "INDEX(満期月数,注目満期番号)", "0"),
    ("母集団開始行", "母集団の開始行", 'IF(母集団開始="",1,IFERROR(MATCH(母集団開始-1,日付有効,1)+1,1))', "0"),
    ("母集団終了行", "母集団の終了行", 'IF(母集団終了="",基準行,MIN(基準行,MATCH(母集団終了,日付有効,1)))', "0"),
    ("表示開始行", "表示の開始行", 'IF(表示開始日="",1,IFERROR(MATCH(表示開始日-1,日付有効,1)+1,1))', "0"),
    ("比較開始行", "比較窓のRVが計算できる母集団の開始行", "MAX(比較窓+1,母集団開始行)", "0"),
    ("上限警告", "警告", f'IF(件数>{MAX_ROWS},"データが上限({MAX_ROWS}日)を超えています(build.pyのMAX_ROWSを増やす)","")', None),
]


def build_settings(book: Book) -> None:
    ws = book.sheet(S_SET)
    title(ws, "設定", "黄色のセルを変えると全シートが再計算される。名前付きセル(数式から名前で参照)")
    header(ws, 3, 1, ["項目", "値", "説明"])
    row = 4
    for name, label, default, note, number_format in SETTINGS:
        ws.cell(row=row, column=1, value=label)
        c = ws.cell(row=row, column=2, value=default)
        c.fill = INPUT_FILL
        if number_format:
            c.number_format = number_format
        ws.cell(row=row, column=3, value=note).font = NOTE
        book.name(name, f"{q(S_SET)}!$B${row}")
        row += 1
    row += 1
    header(ws, row, 1, ["自動計算", "値", ""])
    row += 1
    for name, label, formula, number_format in DERIVED:
        ws.cell(row=row, column=1, value=label)
        c = ws.cell(row=row, column=2, value=f"={formula}")
        if number_format:
            c.number_format = number_format
        book.name(name, f"{q(S_SET)}!$B${row}")
        row += 1

    # 軸の一覧(E〜F: 満期と月数、H: 原資産、J: IVの時間軸)
    header(ws, 3, 5, ["満期", "月数"])
    for n, e in enumerate(EXPIRIES):
        ws.cell(row=4 + n, column=5, value=e)
        ws.cell(row=4 + n, column=6, value=months(e))
    header(ws, 3, 8, ["原資産"])
    for n, t in enumerate(TAILS):
        ws.cell(row=4 + n, column=8, value=t)
    header(ws, 3, 10, ["時間軸"])
    ws["J4"], ws["J5"] = "暦日", "営業日"
    last_e, last_t = 3 + len(EXPIRIES), 3 + len(TAILS)
    book.name("満期一覧", f"{q(S_SET)}!$E$4:$E${last_e}")
    book.name("満期月数", f"{q(S_SET)}!$F$4:$F${last_e}")
    book.name("原資産一覧", f"{q(S_SET)}!$H$4:$H${last_t}")

    rows = {name: 4 + n for n, (name, *_rest) in enumerate(SETTINGS)}
    list_validation(ws, f"B{rows['注目満期']}", "=満期一覧")
    list_validation(ws, f"B{rows['注目原資産']}", "=原資産一覧")
    list_validation(ws, f"B{rows['IV時間軸']}", f"={q(S_SET)}!$J$4:$J$5")
    widths(ws, {"A": 34, "B": 14, "C": 70, "E": 8, "F": 6, "H": 8, "J": 8})


# ---------------------------------------------------------------- データシート・会合日

def build_data_sheets(book: Book) -> None:
    specs = [
        (D_IV, "スワップションATMノーマルvol(bp/年)。DBのvol_quotes", LABELS),
        (D_DF, "満期一定型(B)のFwdの日次変化(bp)", LABELS),
        (D_CUM, "dFの二乗の累積(bp²)。最初の日=0。窓のRV = √(年営業日数×(累積[t]−累積[t−窓])/窓)", LABELS),
        (D_FWD, "満期一定型(B)のFwdスワップレート(%)", LABELS),
        (D_DAYS, "その日から各満期のオプションの満期日までの日数(営業日=ql.Japan、暦日)",
         [f"営業日:{e}" for e in EXPIRIES] + [f"暦日:{e}" for e in EXPIRIES]),
        (D_TRACK, "オプション追跡型(A)。その日に取引したオプションの、満期日までの日次変化の二乗和(bp²)と日数。"
                  "満期日までデータがそろう取引日だけ",
         [f"二乗和:{n}" for n in TRACK_LABELS] + [f"日数:{n}" for n in TRACK_LABELS]),
    ]
    for name, text, columns in specs:
        ws = book.sheet(name)
        ws["A1"] = f"{text}  ※ python -m tools.vol_lab が3行目以降を書き換える(手で編集しない)"
        ws["A1"].font = NOTE
        header(ws, 2, 1, ["日付", *columns])
        ws.column_dimensions["A"].width = 11
        ws.freeze_panes = "B3"
    last = col(1 + len(LABELS))
    book.name("日付", f"{q(D_IV)}!$A${DATA_FIRST}:$A${DATA_LAST}")
    book.name("日付有効", f"{q(D_IV)}!$A${DATA_FIRST}:INDEX({q(D_IV)}!$A${DATA_FIRST}:$A${DATA_LAST},MAX(1,COUNT({q(D_IV)}!$A${DATA_FIRST}:$A${DATA_LAST})))")
    book.name("IVデータ", f"{q(D_IV)}!$B${DATA_FIRST}:${last}${DATA_LAST}")
    book.name("dFデータ", f"{q(D_DF)}!$B${DATA_FIRST}:${last}${DATA_LAST}")
    book.name("累積データ", f"{q(D_CUM)}!$B${DATA_FIRST}:${last}${DATA_LAST}")
    book.name("Fwdデータ", f"{q(D_FWD)}!$B${DATA_FIRST}:${last}${DATA_LAST}")
    n_exp = len(EXPIRIES)
    book.name("残存営業日", f"{q(D_DAYS)}!$B${DATA_FIRST}:${col(1 + n_exp)}${DATA_LAST}")
    book.name("残存暦日", f"{q(D_DAYS)}!${col(2 + n_exp)}${DATA_FIRST}:${col(1 + 2 * n_exp)}${DATA_LAST}")
    n_track = len(TRACK_LABELS)
    book.name("追跡二乗和", f"{q(D_TRACK)}!$B${DATA_FIRST}:${col(1 + n_track)}${DATA_LAST}")
    book.name("追跡日数", f"{q(D_TRACK)}!${col(2 + n_track)}${DATA_FIRST}:${col(1 + 2 * n_track)}${DATA_LAST}")


def build_meetings(book: Book) -> None:
    ws = book.sheet(S_MEET)
    title(ws, "日銀会合の決定公表日(会合の最終日)",
          "出典: 日本銀行「金融政策決定会合の開催予定日・過去の会合」。臨時会合や新しい日程は下に追加する(A4:A400)")
    header(ws, 3, 1, ["公表日"])
    for n, d in enumerate(MEETING_DATES, start=4):
        ws.cell(row=n, column=1, value=d).number_format = DATE_FMT
    ws.column_dimensions["A"].width = 12
    book.name("会合日一覧", f"{q(S_MEET)}!$A$4:$A$400")


# ---------------------------------------------------------------- 日付ごとの表の共通部分

def time_rows(top: int) -> Tuple[int, int]:
    """見出しの行 top → (データの最初の行, 最後の行)。"""
    return top + 1, top + MAX_ROWS


def write_rows(ws, top: int, columns: Sequence[Tuple[str, str, Optional[str]]]) -> None:
    """columns: (見出し, 数式のひな形, 表示形式)。ひな形の {r}=行、{p}=前の行、{F}/{L}=データの最初/最後の行。
    A列は行の番号 i(1始まり)。"""
    first, last = time_rows(top)
    header(ws, top, 1, ["i"] + [h for h, _, _ in columns])
    for r in range(first, last + 1):
        ws.cell(row=r, column=1, value=r - top)
        for n, (_, template, number_format) in enumerate(columns, start=2):
            c = ws.cell(row=r, column=n, value="=" + template.format(r=r, p=r - 1, F=first, L=last))
            if number_format:
                c.number_format = number_format


def colmap(columns: Sequence[Tuple[str, str, Optional[str]]]) -> dict:
    """見出し → 列の文字(A列はi)。"""
    return {h: col(n) for n, (h, _, _) in enumerate(columns, start=2)}


# ---------------------------------------------------------------- ① IV vs RV

def build_s1(book: Book) -> None:
    ws = book.sheet(S1)
    title(ws, "① IV vs RV(注目銘柄)", "RVは満期一定型(B)のFwdの日次変化から。窓・λ・表示する系列は設定シート")
    top = 46
    first, last = time_rows(top)
    rng = lambda c: f"${c}${first}:${c}${last}"  # noqa: E731
    jj = "注目番号"
    show = "$V{r}"
    cols = [
        ("日付", 'IF($A{r}>件数,"",INDEX(日付,$A{r}))', DATE_FMT),                                     # B
        ("IV", data_at("IVデータ", "$A{r}", jj), "0.0"),                                                # C
        ("dF(bp)", data_at("dFデータ", "$A{r}", jj), "0.00"),                                           # D
        ("累積", f'IF($A{{r}}>件数,"",INDEX(累積データ,$A{{r}},{jj}))', "0.0"),                          # E
        ("会合", 'IF($B{r}="","",IF(COUNTIF(会合日一覧,$B{r})>0,1,0))', "0"),                           # F
        ("累積(会合除く)", 'IF($A{r}>件数,"",N(G{p})+IF(AND($F{r}=0,$D{r}<>""),$D{r}^2,0))', "0.0"),    # G
        ("日数(会合除く)", 'IF($A{r}>件数,"",N(H{p})+IF(AND($F{r}=0,$D{r}<>""),1,0))', "0"),            # H
    ]
    for k in range(1, 6):                                                                               # I〜M
        cols.append((f"RV窓{k}",
                     f'IF(OR($A{{r}}>件数,$A{{r}}<=RV窓{k}),"",SQRT({N}*($E{{r}}-INDEX($E${{F}}:$E${{L}},$A{{r}}-RV窓{k}))/RV窓{k}))',
                     "0.0"))
    cols += [
        ("RV比較窓", 'IF(OR($A{r}>件数,$A{r}<=比較窓),"",SQRT(' + N + '*($E{r}-INDEX($E${F}:$E${L},$A{r}-比較窓))/比較窓))', "0.0"),  # N
        ("RV比較窓(会合除く)",
         'IF(OR($A{r}>件数,$A{r}<=比較窓),"",IFERROR(SQRT(' + N + '*($G{r}-INDEX($G${F}:$G${L},$A{r}-比較窓))'
         '/($H{r}-INDEX($H${F}:$H${L},$A{r}-比較窓))),""))', "0.0"),                                    # O
        ("EWMA分散(日次bp²)",
         'IF(OR($A{r}>件数,$A{r}<2),"",IF($A{r}=2,INDEX($E${F}:$E${L},MIN(比較窓+1,件数))/(MIN(比較窓+1,件数)-1),'
         'EWMA係数*P{p}+(1-EWMA係数)*IF($D{r}="",P{p},$D{r}^2)))', "0.00"),                             # P
        ("EWMA", 'IF(P{r}="","",SQRT(' + N + '*P{r}))', "0.0"),                                          # Q
        ("IV−RV", 'IF(OR($C{r}="",$N{r}=""),"",$C{r}-$N{r})', "0.0"),                                   # R
        ("IV−RV(母集団)", 'IF(R{r}="","",IF(AND($A{r}>=母集団開始行,$A{r}<=母集団終了行),R{r},""))', "0.0"),  # S
        ("zスコア", 'IF(OR(R{r}="",$F$5="",$F$5=0),"",(R{r}-$D$5)/$F$5)', "0.00"),                       # T
        ("IV(母集団)", 'IF($C{r}="","",IF(AND($A{r}>=母集団開始行,$A{r}<=母集団終了行),$C{r},""))', "0.0"),  # U
        ("表示", "AND($A{r}<=件数,$A{r}>=表示開始行)", None),                                             # V
        ("日付(グラフ)", "IF(" + show + ",$B{r},NA())", DATE_FMT),                                       # W
        ("IV(グラフ)", "IF(AND(" + show + ',$C{r}<>""),$C{r},NA())', "0.0"),                            # X
    ]
    for k, c in zip(range(1, 6), "IJKLM"):                                                              # Y〜AC
        cols.append((f"RV窓{k}(グラフ)", f'IF(AND({show},表示_RV窓{k}=1,${c}{{r}}<>""),${c}{{r}},NA())', "0.0"))
    cols += [
        ("EWMA(グラフ)", f'IF(AND({show},表示_EWMA=1,$Q{{r}}<>""),$Q{{r}},NA())', "0.0"),                 # AD
        ("会合除く(グラフ)", f'IF(AND({show},表示_会合除く=1,$O{{r}}<>""),$O{{r}},NA())', "0.0"),          # AE
        ("比較窓(グラフ)", f'IF(AND({show},表示_比較窓=1,$N{{r}}<>""),$N{{r}},NA())', "0.0"),             # AF
        ("IV−RV(グラフ)", f'IF(AND({show},$R{{r}}<>""),$R{{r}},NA())', "0.0"),                          # AG
        ("z(グラフ)", f'IF(AND({show},$T{{r}}<>""),$T{{r}},NA())', "0.00"),                              # AH
    ]
    write_rows(ws, top, cols)
    # 系列名(グラフの凡例)は見出しの行に式で入れる
    for k, c in zip(range(1, 6), ["Y", "Z", "AA", "AB", "AC"]):
        ws[f"{c}{top}"] = f'="RV "&RV窓{k}&"日"'
    ws[f"AD{top}"] = '="EWMA λ="&EWMA係数'
    ws[f"AE{top}"] = '="RV "&比較窓&"日(会合除く)"'
    ws[f"AF{top}"] = '="RV "&比較窓&"日"'
    ws[f"AG{top}"] = '="IV−RV "&比較窓&"日"'
    ws[f"AH{top}"] = "zスコア"

    at = lambda c: f"INDEX({rng(c)},基準行)"  # noqa: E731
    summary = [
        ("A2", "銘柄"), ("B2", "=注目銘柄"), ("C2", "基準日"), ("D2", "=実効基準日"),
        ("E2", "IV"), ("F2", f"={at('C')}"), ("G2", "RV比較窓"), ("H2", f"={at('N')}"),
        ("I2", "IV−RV"), ("J2", f"={at('R')}"), ("K2", "zスコア"), ("L2", f"={at('T')}"),
        ("M2", "IV−RVの%ile"), ("N2", f'=IF(OR(J2="",COUNT({rng("S")})=0),"",COUNTIF({rng("S")},"<="&J2)/COUNT({rng("S")}))'),
        ("O2", "IVの%ile"), ("P2", f'=IF(OR(F2="",COUNT({rng("U")})=0),"",COUNTIF({rng("U")},"<="&F2)/COUNT({rng("U")}))'),
        ("A3", "窓(営業日)"), ("B3", "=RV窓1"), ("C3", "=RV窓2"), ("D3", "=RV窓3"), ("E3", "=RV窓4"), ("F3", "=RV窓5"),
        ("G3", "EWMA"), ("H3", '="比較窓"&比較窓&"日(会合除く)"'),
        ("A4", "RV"), ("B4", f"={at('I')}"), ("C4", f"={at('J')}"), ("D4", f"={at('K')}"), ("E4", f"={at('L')}"),
        ("F4", f"={at('M')}"), ("G4", f"={at('Q')}"), ("H4", f"={at('O')}"),
        ("A5", "ブレークイーブン(bp/日)"), ("B5", f'=IF(F2="","",F2/SQRT({N}))'),
        ("C5", "母集団のIV−RV 平均"), ("D5", f'=IFERROR(AVERAGE({rng("S")}),"")'),
        ("E5", "標準偏差"), ("F5", f'=IFERROR(STDEV({rng("S")}),"")'), ("G5", "件数"), ("H5", f'=COUNT({rng("S")})'),
    ]
    for ref, v in summary:
        ws[ref] = v
        if not str(v).startswith("="):
            ws[ref].font = BOLD
    fmt(ws, "D2:D2", DATE_FMT)
    for ref in ("F2", "H2", "J2", "B4", "C4", "D4", "E4", "F4", "G4", "H4", "D5", "F5"):
        ws[ref].number_format = "0.0"
    ws["L2"].number_format = "0.00"
    ws["B5"].number_format = "0.00"
    for ref in ("N2", "P2"):
        ws[ref].number_format = "0%"
    widths(ws, {"A": 12, "B": 11, "C": 9, "D": 11, "M": 11, "O": 9})

    x = (23, first, last)  # W
    ch = time_chart("IV vs RV(bp/年)", "bp/年")
    ch.series.append(series(S1, x, (24, first, last), f"$X${top}", color="C00000", width=22225, sheet=ws))
    palette = ["A6A6A6", "4472C4", "70AD47", "FFC000", "7030A0"]
    for k, c in enumerate(range(25, 30)):
        ch.series.append(series(S1, x, (c, first, last), f"${col(c)}${top}", color=palette[k], sheet=ws))
    ch.series.append(series(S1, x, (30, first, last), f"$AD${top}", color="000000", dash="dash", sheet=ws))
    ch.series.append(series(S1, x, (31, first, last), f"$AE${top}", color="ED7D31", sheet=ws))
    ch.series.append(series(S1, x, (32, first, last), f"$AF${top}", color="5B9BD5", sheet=ws))
    ws.add_chart(ch, "A7")
    ch2 = time_chart("IV−RV(bp/年)", "bp/年", width=15, height=7.5)
    ch2.series.append(series(S1, x, (33, first, last), f"$AG${top}", color="4472C4", sheet=ws))
    ws.add_chart(ch2, "A30")
    ch3 = time_chart("IV−RVのzスコア(母集団の平均・標準偏差)", "z", width=15, height=7.5)
    ch3.series.append(series(S1, x, (34, first, last), f"$AH${top}", color="7030A0", sheet=ws))
    ws.add_chart(ch3, "J30")
    ws.freeze_panes = f"C{first}"


# ---------------------------------------------------------------- ② マトリックス

INDICATORS = ["IV", "RV(比較窓)", "IV−RV", "IV÷RV", "IV−RVのzスコア", "IV−RVのパーセンタイル", "IVのパーセンタイル",
              "IV変化(比較日比)"]


def build_s2(book: Book) -> None:
    ws = book.sheet(S2)
    title(ws, "② マトリックス(基準日の満期x原資産)",
          "RVは比較窓。zスコア・パーセンタイルは母集団(設定)の期間の、同じ銘柄の毎日のIV−RV・IVの分布に対する位置")
    ws["A3"], ws["B3"] = "表示する指標", "IV−RVのzスコア"
    ws["A3"].font = BOLD
    ws["B3"].fill = INPUT_FILL
    ws["C3"] = f"=MATCH(B3,$AD$3:$AD${2 + len(INDICATORS)},0)"
    ws["D3"] = '="基準日 "&TEXT(実効基準日,"yyyy-mm-dd")&" / 比較窓 "&比較窓&"日 / 母集団 "&TEXT(INDEX(日付,比較開始行),"yyyy-mm-dd")&"〜"&TEXT(INDEX(日付,母集団終了行),"yyyy-mm-dd")'
    header(ws, 2, 30, ["指標の一覧"])
    for n, name in enumerate(INDICATORS, start=3):
        ws.cell(row=n, column=30, value=name)
    list_validation(ws, "B3", f"=$AD$3:$AD${2 + len(INDICATORS)}")

    # 192銘柄の縦の一覧(40行目が見出し)
    top = 40
    first, last = top + 1, top + len(LABELS)
    names = ["番号", "銘柄", "満期", "原資産", "IV", "RV", "IV−RV", "IV÷RV", "zスコア", "IV−RV %ile", "IV %ile",
             "IV変化(比較日比)", "母集団の日数"]
    header(ws, top, 1, names)
    for n, name in enumerate(LABELS):
        r = first + n
        e, t = name.split("x")
        ws.cell(row=r, column=1, value=n + 1)
        ws.cell(row=r, column=2, value=name)
        ws.cell(row=r, column=3, value=e)
        ws.cell(row=r, column=4, value=t)
        j = f"$A{r}"
        iv = f"INDEX(IVデータ,基準行,{j})"
        ws[f"E{r}"] = f'=IF({iv}="","",{iv})'
        ws[f"F{r}"] = f'=IF(基準行<=比較窓,"",{rv_from_cum("基準行", j, "比較窓")})'
        ws[f"G{r}"] = f'=IF(OR(E{r}="",F{r}=""),"",E{r}-F{r})'
        ws[f"H{r}"] = f'=IF(OR(E{r}="",F{r}="",F{r}=0),"",E{r}/F{r})'
        iv_r = f"INDEX(IVデータ,比較開始行,{j}):INDEX(IVデータ,母集団終了行,{j})"
        c_r = f"INDEX(累積データ,比較開始行,{j}):INDEX(累積データ,母集団終了行,{j})"
        c_l = f"INDEX(累積データ,比較開始行-比較窓,{j}):INDEX(累積データ,母集団終了行-比較窓,{j})"
        sp = f"({iv_r}-SQRT({N}*({c_r}-{c_l})/比較窓))"
        guard = "比較開始行>母集団終了行"
        ws[f"I{r}"] = ArrayFormula(f"I{r}", f'=IF(OR(G{r}="",{guard}),"",(G{r}-AVERAGE(IF({iv_r}<>"",{sp})))/STDEV(IF({iv_r}<>"",{sp})))')
        ws[f"J{r}"] = ArrayFormula(f"J{r}", f'=IF(OR(G{r}="",{guard}),"",SUM(IF({iv_r}<>"",IF({sp}<=G{r},1,0)))/SUM(IF({iv_r}<>"",1,0)))')
        ws[f"K{r}"] = ArrayFormula(f"K{r}", f'=IF(OR(E{r}="",{guard}),"",SUM(IF({iv_r}<>"",IF({iv_r}<=E{r},1,0)))/SUM(IF({iv_r}<>"",1,0)))')
        cmp_iv = f"INDEX(IVデータ,比較行,{j})"
        ws[f"L{r}"] = f'=IF(OR(比較行="",E{r}=""),"",IF({cmp_iv}="","",E{r}-{cmp_iv}))'
        ws[f"M{r}"] = ArrayFormula(f"M{r}", f'=IF({guard},0,SUM(IF({iv_r}<>"",1,0)))')
        for c, f in zip("EFGHIJKL", ["0.0", "0.0", "0.0", "0.00", "0.00", "0%", "0%", "0.0"]):
            ws[f"{c}{r}"].number_format = f

    # 上: 選んだ指標の格子(B〜M)、右: zスコアの格子(P〜AA)
    def grid(first_col: int, header_text: str, value) -> str:
        ws.cell(row=6, column=first_col - 1, value=header_text).font = BOLD
        for n, t in enumerate(TAILS):
            ws.cell(row=6, column=first_col + n, value=t).font = BOLD
        for m, e in enumerate(EXPIRIES):
            ws.cell(row=7 + m, column=first_col - 1, value=e).font = BOLD
            for n in range(len(TAILS)):
                jn = m * len(TAILS) + n + 1
                c = ws.cell(row=7 + m, column=first_col + n, value=value(jn))
        return f"{col(first_col)}7:{col(first_col + len(TAILS) - 1)}{6 + len(EXPIRIES)}"

    chosen = ",".join(f"${c}${first}:${c}${last}" for c in "EFGHIJKL")
    area1 = grid(2, "=B3", lambda jn: f'=INDEX(CHOOSE($C$3,{chosen}),{jn})')
    area2 = grid(16, "IV−RVのzスコア", lambda jn: f"=INDEX($I${first}:$I${last},{jn})")
    fmt(ws, area1, "0.0")
    fmt(ws, area2, "0.00")
    ws.conditional_formatting.add(area1, ColorScaleRule(start_type="min", start_color="5B9BD5", mid_type="percentile",
                                                        mid_value=50, mid_color="FFFFFF", end_type="max", end_color="F8696B"))
    ws.conditional_formatting.add(area2, ColorScaleRule(start_type="num", start_value=-2, start_color="5B9BD5",
                                                        mid_type="num", mid_value=0, mid_color="FFFFFF",
                                                        end_type="num", end_value=2, end_color="F8696B"))
    notes(ws, "A24", ["赤=IVが相対的に高い(割高側)、青=低い(割安側)。zスコアの格子は±2で色が最も濃くなる"])

    # 割高・割安の順位(zスコア)
    header(ws, 26, 1, ["順位", "割高(z大)", "z", "IV", "RV"])
    header(ws, 26, 7, ["順位", "割安(z小)", "z", "IV", "RV"])
    zr, br, er, fr = (f"${c}${first}:${c}${last}" for c in "IBEF")
    for k in range(1, 6):
        r = 26 + k
        for c0, fn in ((1, "LARGE"), (7, "SMALL")):
            a, b, c, d, e = (col(c0 + n) for n in range(5))
            ws[f"{a}{r}"] = k
            ws[f"{c}{r}"] = f'=IFERROR({fn}({zr},{k}),"")'
            ws[f"{b}{r}"] = f'=IF({c}{r}="","",INDEX({br},MATCH({c}{r},{zr},0)))'
            ws[f"{d}{r}"] = f'=IF({b}{r}="","",INDEX({er},MATCH({b}{r},{br},0)))'
            ws[f"{e}{r}"] = f'=IF({b}{r}="","",INDEX({fr},MATCH({b}{r},{br},0)))'
            ws[f"{c}{r}"].number_format = "0.00"
            ws[f"{d}{r}"].number_format = "0.0"
            ws[f"{e}{r}"].number_format = "0.0"
    widths(ws, {"A": 14, "B": 12, "N": 3, "O": 14, "AD": 20})


# ---------------------------------------------------------------- ③ ボラコーン

CONE_QUANTILES = [("最小", None), ("10%", 0.1), ("25%", 0.25), ("中央値", 0.5), ("75%", 0.75), ("90%", 0.9), ("最大", None)]


def build_s3(book: Book) -> None:
    ws = book.sheet(S3)
    title(ws, "③ ボラコーン(注目原資産)",
          "満期eの行 = e x 原資産のFwd(B)の、窓 = e相当の営業日数のRVの分布(母集団の期間)。IVがコーンのどこにいるか")
    names = ["満期", "月数", "窓(営業日)", "番号 j", "開始行", "件数"] + [n for n, _ in CONE_QUANTILES] + \
            ["現在のRV", "IV", "IVのコーン内%ile"]
    header(ws, 5, 1, names)
    for m, e in enumerate(TRACK_EXPIRIES):
        r = 6 + m
        k = EXPIRIES.index(e) + 1
        ws[f"A{r}"] = e
        ws[f"B{r}"] = months(e)
        ws[f"C{r}"] = f"=ROUND({N}*B{r}/12,0)"
        ws[f"D{r}"] = f"=({k}-1)*12+注目原資産番号"
        ws[f"E{r}"] = f"=MAX(C{r}+1,母集団開始行)"
        ws[f"F{r}"] = f"=MAX(0,母集団終了行-E{r}+1)"
        arr = (f"SQRT({N}*(INDEX(累積データ,$E{r},$D{r}):INDEX(累積データ,母集団終了行,$D{r})"
               f"-INDEX(累積データ,$E{r}-$C{r},$D{r}):INDEX(累積データ,母集団終了行-$C{r},$D{r}))/$C{r})")
        for n, (_, p) in enumerate(CONE_QUANTILES):
            c = col(7 + n)
            if p is None:
                fn = "MIN" if n == 0 else "MAX"
                body = f"{fn}({arr})"
            else:
                body = f"PERCENTILE({arr},{p})"
            ws[f"{c}{r}"] = ArrayFormula(f"{c}{r}", f'=IF($F{r}<2,"",{body})')
        ws[f"N{r}"] = f'=IF(基準行<=$C{r},"",{rv_from_cum("基準行", f"$D{r}", f"$C{r}")})'
        ws[f"O{r}"] = f'=IF(INDEX(IVデータ,基準行,$D{r})="","",INDEX(IVデータ,基準行,$D{r}))'
        ws[f"P{r}"] = ArrayFormula(f"P{r}", f'=IF(OR(O{r}="",$F{r}<2),"",SUM(IF({arr}<=O{r},1,0))/$F{r})')
        fmt(ws, f"G{r}:O{r}", "0.0")
        ws[f"P{r}"].number_format = "0%"
    last = 5 + len(TRACK_EXPIRIES)
    notes(ws, "A13", [
        "窓 = 年営業日数 × 月数 / 12。C列を直接書き換えてもよい(例: 1Mを10日にする)",
        "現在のRV < IV の位置関係だけでなく、IVがコーンの上の方(%ileが高い)なら割高、下の方なら割安の目安",
        "データが短いと長い窓のコーンは件数が少ない(F列)。母集団を長くするには履歴を延ばす",
    ])
    ch = category_chart("ボラコーン(bp/年)", "bp/年", width=24, height=11)
    colors = ["BFBFBF", "A6A6A6", "7F7F7F", "404040", "7F7F7F", "A6A6A6", "BFBFBF"]
    dashes = ["dash", "sysDot", None, None, None, "sysDot", "dash"]
    for n in range(len(CONE_QUANTILES)):
        ch.series.append(series(S3, None, (7 + n, 6, last), f"${col(7 + n)}$5", color=colors[n], dash=dashes[n], sheet=ws))
    ch.series.append(series(S3, None, (14, 6, last), "$N$5", color="4472C4", width=22225, sheet=ws))
    s = series(S3, None, (15, 6, last), "$O$5", color="C00000", width=28575, sheet=ws)
    s.marker.symbol = "circle"
    s.marker.size = 7
    ch.series.append(s)
    ch.set_categories(Reference(ws, min_col=1, min_row=6, max_row=last))
    ws.add_chart(ch, "A17")
    widths(ws, {"A": 8, "P": 14})


# ---------------------------------------------------------------- ④ ブレークイーブン

def build_s4(book: Book) -> None:
    ws = book.sheet(S4)
    title(ws, "④ ブレークイーブン(注目銘柄)",
          "前日のIVが織り込む1日の値幅 = IV/√年営業日数(bp/日)。実際の|dF|がこれを超えた日はガンマの買いが勝つ")
    top = 52
    first, last = time_rows(top)
    jj = "注目番号"
    cols = [
        ("日付", 'IF($A{r}>件数,"",INDEX(日付,$A{r}))', DATE_FMT),                                       # B
        ("前日のIV", 'IF(OR($A{r}>件数,$A{r}<2),"",IF(INDEX(IVデータ,$A{r}-1,' + jj + ')="","",INDEX(IVデータ,$A{r}-1,' + jj + ')))', "0.0"),  # C
        ("ブレークイーブン(bp/日)", 'IF(C{r}="","",C{r}/SQRT(' + N + '))', "0.00"),                       # D
        ("dF(bp)", data_at("dFデータ", "$A{r}", jj), "0.00"),                                             # E
        ("|dF|", 'IF(E{r}="","",ABS(E{r}))', "0.00"),                                                    # F
        ("会合", 'IF(B{r}="","",IF(COUNTIF(会合日一覧,B{r})>0,1,0))', "0"),                               # G
        ("母集団", "IF(AND($A{r}>=母集団開始行,$A{r}<=母集団終了行,$A{r}<=件数),1,0)", "0"),               # H
        ("超えた", 'IF(OR(D{r}="",F{r}=""),"",IF(F{r}>D{r},1,0))', "0"),                                 # I
        ("dF²", 'IF(E{r}="","",E{r}^2)', "0.00"),                                                       # J
        ("損益(½Γ=1)", 'IF(OR(D{r}="",E{r}=""),"",0.5*(E{r}^2-D{r}^2))', "0.00"),                        # K
        ("累積損益", 'IF($A{r}>件数,"",IF($A{r}<表示開始行,0,N(L{p})+N(K{r})))', "0.0"),                  # L
        ("表示", "AND($A{r}<=件数,$A{r}>=表示開始行)", None),                                             # M
        ("日付(グラフ)", "IF(M{r},B{r},NA())", DATE_FMT),                                                 # N
        ("ブレークイーブン", 'IF(AND(M{r},D{r}<>""),D{r},NA())', "0.00"),                                 # O
        ("|dF| 会合以外", 'IF(AND(M{r},F{r}<>"",G{r}=0),F{r},NA())', "0.00"),                            # P
        ("|dF| 会合日", 'IF(AND(M{r},F{r}<>"",G{r}=1),F{r},NA())', "0.00"),                              # Q
        ("累積損益(グラフ)", "IF(M{r},L{r},NA())", "0.0"),                                                 # R
    ]
    write_rows(ws, top, cols)
    rng = lambda c: f"${c}${first}:${c}${last}"  # noqa: E731
    H, G, I, J, D = rng("H"), rng("G"), rng("I"), rng("J"), rng("D")
    rows = [
        ("A3", "母集団の集計"), ("B3", "全体"), ("C3", "会合日"), ("D3", "会合日以外"), ("E3", "参考"),
        ("A4", "|dF|がブレークイーブンを超えた割合"),
        ("B4", f'=IFERROR(SUMIFS({I},{H},1)/COUNTIFS({H},1,{I},">=0"),"")'),
        ("C4", f'=IFERROR(SUMIFS({I},{H},1,{G},1)/COUNTIFS({H},1,{G},1,{I},">=0"),"")'),
        ("D4", f'=IFERROR(SUMIFS({I},{H},1,{G},0)/COUNTIFS({H},1,{G},0,{I},">=0"),"")'),
        ("E4", 0.3173), ("F4", "←正規分布ならP(|Z|>1)=31.7%"),
        ("A5", "変動のRMS(bp/日)"),
        ("B5", f'=IFERROR(SQRT(SUMIFS({J},{H},1)/COUNTIFS({H},1,{J},">=0")),"")'),
        ("C5", f'=IFERROR(SQRT(SUMIFS({J},{H},1,{G},1)/COUNTIFS({H},1,{G},1,{J},">=0")),"")'),
        ("D5", f'=IFERROR(SQRT(SUMIFS({J},{H},1,{G},0)/COUNTIFS({H},1,{G},0,{J},">=0")),"")'),
        ("E5", f'=IFERROR(AVERAGEIFS({D},{H},1),"")'), ("F5", "←平均ブレークイーブン"),
        ("A6", "日数"), ("B6", f'=COUNTIFS({H},1,{J},">=0")'), ("C6", f'=COUNTIFS({H},1,{G},1,{J},">=0")'),
        ("D6", f'=COUNTIFS({H},1,{G},0,{J},">=0")'),
        ("E6", f'=IF(件数=0,"",INDEX({rng("L")},件数))'), ("F6", "←累積損益(表示期間、bp²。½Γ=1のとき)"),
    ]
    for ref, v in rows:
        ws[ref] = v
    for ref in ("A3", "B3", "C3", "D3", "E3", "A4", "A5", "A6"):
        ws[ref].font = BOLD
    fmt(ws, "B4:E4", "0.0%")
    fmt(ws, "B5:E5", "0.00")
    ws["E6"].number_format = "0.0"
    ws["A2"] = '="銘柄 "&注目銘柄&"  ブレークイーブン = 前日のIV/√"&年営業日数&"。損益 = ½(dF² − ブレークイーブン²)(デルタヘッジしたストラドルを1日ごとに買い直す近似)"'
    ws["A2"].font = NOTE
    x = (14, first, last)
    ch = time_chart("1日の変動とブレークイーブン(bp/日)", "bp/日", width=30, height=11)
    ch.series.append(series(S4, x, (16, first, last), f"$P${top}", markers=True, color="A6A6A6", sheet=ws))
    ch.series.append(series(S4, x, (17, first, last), f"$Q${top}", markers=True, color="C00000", sheet=ws))
    ch.series.append(series(S4, x, (15, first, last), f"$O${top}", color="4472C4", width=22225, sheet=ws))
    ws.add_chart(ch, "A8")
    ch2 = time_chart("ガンマの累積損益(bp²、½Γ=1)", "bp²", width=30, height=8)
    ch2.series.append(series(S4, x, (18, first, last), f"$R${top}", color="70AD47", sheet=ws))
    ws.add_chart(ch2, "A31")
    widths(ws, {"A": 30, "B": 11})
    ws.freeze_panes = f"C{first}"


# ---------------------------------------------------------------- ⑤ 事後VRP

def build_s5(book: Book) -> None:
    ws = book.sheet(S5)
    title(ws, "⑤ 事後VRP(注目銘柄、満期1Y以下)",
          "取引日のIVと、その後満期までに実際に実現したボラ(A: 起算日・満期日を固定したFwd)。VRP = IV − 実現")
    top = 52
    first, last = time_rows(top)
    jj = "注目番号"
    ok = "注目満期番号<=6"
    cols = [
        ("取引日", 'IF($A{r}>件数,"",INDEX(日付,$A{r}))', DATE_FMT),                                     # B
        ("IV", f'IF(NOT({ok}),"",' + data_at("IVデータ", "$A{r}", jj) + ")", "0.0"),                      # C
        ("二乗和", f'IF(NOT({ok}),"",' + data_at("追跡二乗和", "$A{r}", jj) + ")", "0.0"),                  # D
        ("日数", f'IF(NOT({ok}),"",' + data_at("追跡日数", "$A{r}", jj) + ")", "0"),                       # E
        ("実現ボラ", 'IF(OR(D{r}="",E{r}="",N(E{r})=0),"",SQRT(' + N + '*D{r}/E{r}))', "0.0"),              # F
        ("VRP", 'IF(OR(C{r}="",F{r}=""),"",C{r}-F{r})', "0.0"),                                          # G
        ("直前のRV(同じ長さ)", 'IF(OR($A{r}>件数,$A{r}<=$B$3),"",' + rv_from_cum("$A{r}", jj, "$B$3") + ")", "0.0"),  # H
        ("母集団", 'IF(AND($A{r}>=母集団開始行,$A{r}<=母集団終了行,G{r}<>""),1,0)', "0"),                   # I
        ("重ならない", "IF(I{r}=1,IF(MOD($A{r}-母集団開始行,$B$3)=0,1,0),0)", "0"),                        # J
        ("VRP(重ならない)", 'IF(J{r}=1,G{r},"")', "0.0"),                                                # K
        ("VRP(母集団)", 'IF(I{r}=1,G{r},"")', "0.0"),                                                    # L
        ("誤差²(IV)", 'IF(AND(I{r}=1,H{r}<>""),(C{r}-F{r})^2,"")', "0.0"),                                # M
        ("誤差²(直前RV)", 'IF(AND(I{r}=1,H{r}<>""),(H{r}-F{r})^2,"")', "0.0"),                            # N
        ("IV(比較用)", 'IF(AND(I{r}=1,H{r}<>""),C{r},"")', "0.0"),                                       # O
        ("直前RV(比較用)", 'IF(AND(I{r}=1,H{r}<>""),H{r},"")', "0.0"),                                    # P
        ("実現(比較用)", 'IF(AND(I{r}=1,H{r}<>""),F{r},"")', "0.0"),                                      # Q
        ("表示", "AND($A{r}<=件数,$A{r}>=表示開始行)", None),                                              # R
        ("日付(グラフ)", "IF(R{r},B{r},NA())", DATE_FMT),                                                  # S
        ("IV(取引日)", 'IF(AND(R{r},C{r}<>"",F{r}<>""),C{r},NA())', "0.0"),                               # T
        ("実現ボラ(満期まで)", 'IF(AND(R{r},F{r}<>""),F{r},NA())', "0.0"),                                 # U
        ("直前のRV", 'IF(AND(R{r},H{r}<>"",F{r}<>""),H{r},NA())', "0.0"),                                  # V
        ("VRP(グラフ)", 'IF(AND(R{r},G{r}<>""),G{r},NA())', "0.0"),                                       # W
    ]
    write_rows(ws, top, cols)
    rng = lambda c: f"${c}${first}:${c}${last}"  # noqa: E731

    def stats(row: int, label: str, r: str) -> None:
        ws[f"A{row}"] = label
        ws[f"B{row}"] = f"=COUNT({r})"
        ws[f"C{row}"] = f'=IFERROR(AVERAGE({r}),"")'
        ws[f"D{row}"] = f'=IFERROR(STDEV({r}),"")'
        ws[f"E{row}"] = f'=IFERROR(C{row}/(D{row}/SQRT(B{row})),"")'
        ws[f"F{row}"] = f'=IFERROR(COUNTIF({r},">0")/B{row},"")'
        fmt(ws, f"C{row}:E{row}", "0.00")
        ws[f"F{row}"].number_format = "0%"

    ws["A2"] = f'=IF({ok},"銘柄 "&注目銘柄,"事後VRPは満期1Y以下の銘柄だけ(設定の注目満期を変える)")'
    ws["A3"], ws["B3"] = "満期の営業日数(重ならない間隔・直前RVの窓)", f"=ROUND({N}*注目満期月数/12,0)"
    header(ws, 4, 1, ["", "件数", "平均VRP", "標準偏差", "t値", "VRP>0の割合"])
    stats(5, "全取引日(重なりあり。t値は過大)", rng("L"))
    stats(6, "重ならない取引日", rng("K"))
    ws["A7"], ws["B7"], ws["C7"] = "予測の誤差(RMSE、同じ取引日)", "IV", "直前のRV"
    ws["B8"] = f'=IFERROR(SQRT(AVERAGE({rng("M")})),"")'
    ws["C8"] = f'=IFERROR(SQRT(AVERAGE({rng("N")})),"")'
    ws["A8"] = "RMSE(bp/年)"
    ws["A9"] = "実現ボラとの相関"
    ws["B9"] = f'=IFERROR(CORREL({rng("O")},{rng("Q")}),"")'
    ws["C9"] = f'=IFERROR(CORREL({rng("P")},{rng("Q")}),"")'
    fmt(ws, "B8:C9", "0.00")
    for ref in ("A3", "A7", "B7", "C7"):
        ws[ref].font = BOLD
    x = (19, first, last)
    ch = time_chart("取引日のIV vs 満期までの実現ボラ(取引日の位置にプロット)", "bp/年", width=30, height=11)
    ch.series.append(series(S5, x, (20, first, last), f"$T${top}", color="C00000", sheet=ws))
    ch.series.append(series(S5, x, (21, first, last), f"$U${top}", color="4472C4", sheet=ws))
    ch.series.append(series(S5, x, (22, first, last), f"$V${top}", color="A6A6A6", dash="dash", sheet=ws))
    ws.add_chart(ch, "A11")
    ch2 = time_chart("VRP = IV − 実現ボラ", "bp/年", width=30, height=8)
    ch2.series.append(series(S5, x, (23, first, last), f"$W${top}", color="70AD47", sheet=ws))
    ws.add_chart(ch2, "A33")
    widths(ws, {"A": 34, "B": 11})
    ws.freeze_panes = f"C{first}"


# ---------------------------------------------------------------- ⑥ 期間構造

def build_s6(book: Book) -> None:
    ws = book.sheet(S6)
    title(ws, "⑥ 期間構造(注目原資産、基準日)",
          "総分散 w = IV² × τ。満期T1→T2のフォワードボラ = √((w2−w1)/(τ2−τ1))。満期ごとに原資産のFwdが違うことは無視した近似")
    ws["A3"] = '="原資産 "&注目原資産&" / 基準日 "&TEXT(実効基準日,"yyyy-mm-dd")&" / 時間軸 "&IV時間軸&" / ロール "&ロール月数&"か月"'
    names = ["満期", "月数", "番号 j", "IV", "残存営業日", "残存暦日", "τ(年)", "総分散 w", "フォワードボラ", "区間",
             "ロール後のτ", "ロール後のIV", "ロールダウン", "IV(比較日)", "IV変化", "フォワード(グラフ)", "IV(グラフ)", "比較日(グラフ)"]
    header(ws, 6, 1, names)
    f0, l0 = 7, 6 + len(EXPIRIES)
    for k, e in enumerate(EXPIRIES, start=1):
        r = 6 + k
        ws[f"A{r}"], ws[f"B{r}"] = e, months(e)
        ws[f"C{r}"] = f"=({k}-1)*12+注目原資産番号"
        ws[f"D{r}"] = f'=IF(INDEX(IVデータ,基準行,C{r})="","",INDEX(IVデータ,基準行,C{r}))'
        ws[f"E{r}"] = f"=INDEX(残存営業日,基準行,{k})"
        ws[f"F{r}"] = f"=INDEX(残存暦日,基準行,{k})"
        ws[f"G{r}"] = f'=IF(IV時間軸="営業日",E{r}/{N},F{r}/365)'
        ws[f"H{r}"] = f'=IF(D{r}="","",D{r}^2*G{r})'
        if k == 1:
            ws[f"I{r}"] = f"=D{r}"
            ws[f"J{r}"] = f'="0→"&A{r}'
        else:
            p = r - 1
            ws[f"I{r}"] = f'=IF(OR(H{r}="",H{p}=""),"",IF(H{r}-H{p}<=0,"負",SQRT((H{r}-H{p})/(G{r}-G{p}))))'
            ws[f"J{r}"] = f'=A{p}&"→"&A{r}'
        ws[f"K{r}"] = f"=G{r}-ロール月数/12"
        m = f"MATCH(K{r},$G${f0}:$G${l0},1)"
        hh, gg = f"$H${f0}:$H${l0}", f"$G${f0}:$G${l0}"
        interp = (f"SQRT((INDEX({hh},{m})+(INDEX({hh},{m}+1)-INDEX({hh},{m}))*(K{r}-INDEX({gg},{m}))"
                  f"/(INDEX({gg},{m}+1)-INDEX({gg},{m})))/K{r})")
        ws[f"L{r}"] = f'=IF(OR(D{r}="",K{r}<=0),"",IFERROR(IF(K{r}<$G${f0},$D${f0},{interp}),""))'
        ws[f"M{r}"] = f'=IF(L{r}="","",L{r}-D{r})'
        ws[f"N{r}"] = f'=IF(比較行="","",IF(INDEX(IVデータ,比較行,C{r})="","",INDEX(IVデータ,比較行,C{r})))'
        ws[f"O{r}"] = f'=IF(OR(D{r}="",N{r}=""),"",D{r}-N{r})'
        ws[f"P{r}"] = f"=IF(ISNUMBER(I{r}),I{r},NA())"
        ws[f"Q{r}"] = f'=IF(D{r}="",NA(),D{r})'
        ws[f"R{r}"] = f'=IF(N{r}="",NA(),N{r})'
        for c, f in zip("DGHILMNOPQR", ["0.0", "0.000", "0", "0.0", "0.000", "0.0", "0.00", "0.0", "0.0", "0.0", "0.0"]):
            ws[f"{c}{r}"].number_format = f
        ws[f"K{r}"].number_format = "0.000"
    ws["P6"], ws["Q6"], ws["R6"] = "フォワードボラ", "IV(基準日)", "IV(比較日)"
    notes(ws, "A25", [
        "フォワードボラ: 例えば1M→2Mの値は「1か月後から1か月間」に織り込まれたボラ。「負」は総分散が満期とともに減っている(期間構造の歪み)",
        "ロールダウン: 満期を残したまま時間が過ぎると、同じオプションは短い満期のIVで評価される(表面が変わらない仮定、総分散を線形補間)",
        "注意: 1Mx10Yと2Mx10Yは別のFwd(起算日が1か月違う)。原資産が長いほど近似はよい",
    ])
    ch = category_chart("IVとフォワードボラ(bp/年)", "bp/年", width=24, height=11)
    ch.series.append(series(S6, None, (17, f0, l0), "$Q$6", color="C00000", width=22225, sheet=ws))
    ch.series.append(series(S6, None, (16, f0, l0), "$P$6", color="4472C4", sheet=ws))
    ch.series.append(series(S6, None, (18, f0, l0), "$R$6", color="A6A6A6", dash="dash", sheet=ws))
    ch.set_categories(Reference(ws, min_col=1, min_row=f0, max_row=l0))
    ws.add_chart(ch, "A29")
    bar = BarChart()
    bar.title = "ロールダウン(bp/年)"
    bar.width, bar.height = 16, 11
    bar.x_axis.delete = False
    bar.y_axis.delete = False
    bar.legend = None
    bar.add_data(Reference(ws, min_col=13, min_row=6, max_row=l0), titles_from_data=True)
    bar.set_categories(Reference(ws, min_col=1, min_row=f0, max_row=l0))
    ws.add_chart(bar, "N29")
    widths(ws, {"J": 10})


# ---------------------------------------------------------------- ⑦ 期待仮説

def build_s7(book: Book) -> None:
    ws = book.sheet(S7)
    title(ws, "⑦ 期待仮説(注目原資産)",
          "取引日sのT1・T2のIVから、T1後からT2までのフォワードボラを出し、実際にT1後になったときの(T2−T1)のIVと比べる")
    ws["A3"], ws["B3"] = "近い満期 T1", "1M"
    ws["A4"], ws["B4"] = "遠い満期 T2", "2M"
    ws["A5"] = "比べる満期 T2−T1"
    ws["B5"] = ('=IFERROR(INDEX(満期一覧,MATCH(INDEX(満期月数,MATCH(B4,満期一覧,0))-INDEX(満期月数,MATCH(B3,満期一覧,0)),'
                '満期月数,0)),"格子に無い")')
    for ref in ("B3", "B4"):
        ws[ref].fill = INPUT_FILL
        list_validation(ws, ref, "=満期一覧")
    ws["C3"] = "=MATCH(B3,満期一覧,0)"
    ws["C4"] = "=MATCH(B4,満期一覧,0)"
    ws["C5"] = '=IFERROR(MATCH(B5,満期一覧,0),"")'
    for r in (3, 4, 5):
        ws[f"D{r}"] = f'=IF(C{r}="","",(C{r}-1)*12+注目原資産番号)'
    ws["C2"], ws["D2"] = "満期の番号", "銘柄の番号 j"
    top = 36
    first, last = time_rows(top)
    tau = lambda k: (f'IF($A{{r}}>件数,"",IF(IV時間軸="営業日",INDEX(残存営業日,$A{{r}},{k})/{N},'  # noqa: E731
                     f'INDEX(残存暦日,$A{{r}},{k})/365))')
    cols = [
        ("取引日", 'IF($A{r}>件数,"",INDEX(日付,$A{r}))', DATE_FMT),                                      # B
        ("IV(T1)", data_at("IVデータ", "$A{r}", "$D$3"), "0.0"),                                         # C
        ("IV(T2)", data_at("IVデータ", "$A{r}", "$D$4"), "0.0"),                                         # D
        ("τ1", tau("$C$3"), "0.000"),                                                                   # E
        ("τ2", tau("$C$4"), "0.000"),                                                                   # F
        ("フォワード分散", 'IF(OR(C{r}="",D{r}="",$C$5="",F{r}<=E{r}),"",(D{r}^2*F{r}-C{r}^2*E{r})/(F{r}-E{r}))', "0"),  # G
        ("フォワードボラ", 'IF(G{r}="","",IF(G{r}>0,SQRT(G{r}),""))', "0.0"),                             # H
        ("到来行", 'IF($A{r}>件数,"",$A{r}+INDEX(残存営業日,$A{r},$C$3))', "0"),                          # I
        ("到来日", 'IF(I{r}="","",IF(I{r}>件数,"",INDEX(日付,I{r})))', DATE_FMT),                          # J
        ("到来日のIV(T2−T1)", 'IF(OR(J{r}="",$C$5=""),"",IF(INDEX(IVデータ,I{r},$D$5)="","",INDEX(IVデータ,I{r},$D$5)))', "0.0"),  # K
        ("取引日のIV(T2−T1)", 'IF($C$5="","",' + data_at("IVデータ", "$A{r}", "$D$5") + ")", "0.0"),     # L
        ("母集団", 'IF(AND($A{r}>=母集団開始行,$A{r}<=母集団終了行,H{r}<>"",K{r}<>"",L{r}<>""),1,0)', "0"),  # M
        ("x=フォワード−取引日IV", 'IF(M{r}=1,H{r}-L{r},"")', "0.0"),                                    # N
        ("y=到来日IV−取引日IV", 'IF(M{r}=1,K{r}-L{r},"")', "0.0"),                                      # O
        ("誤差(フォワード)", 'IF(M{r}=1,K{r}-H{r},"")', "0.0"),                                         # P
        ("誤差(据え置き)", 'IF(M{r}=1,K{r}-L{r},"")', "0.0"),                                           # Q
        ("表示", "AND($A{r}<=件数,$A{r}>=表示開始行)", None),                                             # R
        ("日付(グラフ)", "IF(R{r},B{r},NA())", DATE_FMT),                                                 # S
        ("フォワードボラ(グラフ)", 'IF(AND(R{r},H{r}<>""),H{r},NA())', "0.0"),                           # T
        ("到来日のIV(グラフ)", 'IF(AND(R{r},K{r}<>""),K{r},NA())', "0.0"),                               # U
        ("取引日のIV(グラフ)", 'IF(AND(R{r},L{r}<>""),L{r},NA())', "0.0"),                               # V
        ("x(グラフ)", 'IF(N{r}="",NA(),N{r})', "0.0"),                                                  # W
        ("y(グラフ)", 'IF(O{r}="",NA(),O{r})', "0.0"),                                                  # X
    ]
    write_rows(ws, top, cols)
    rng = lambda c: f"${c}${first}:${c}${last}"  # noqa: E731
    header(ws, 7, 1, ["回帰 y = a + b·x", "件数", "傾き b", "切片 a", "決定係数", "RMSE(フォワード)", "RMSE(据え置き)", "平均誤差(フォワード)"])
    ws["A8"] = "母集団"
    ws["B8"] = f"=COUNT({rng('N')})"
    ws["C8"] = f'=IFERROR(SLOPE({rng("O")},{rng("N")}),"")'
    ws["D8"] = f'=IFERROR(INTERCEPT({rng("O")},{rng("N")}),"")'
    ws["E8"] = f'=IFERROR(RSQ({rng("O")},{rng("N")}),"")'
    ws["F8"] = f'=IFERROR(SQRT(SUMSQ({rng("P")})/COUNT({rng("P")})),"")'
    ws["G8"] = f'=IFERROR(SQRT(SUMSQ({rng("Q")})/COUNT({rng("Q")})),"")'
    ws["H8"] = f'=IFERROR(AVERAGE({rng("P")}),"")'
    fmt(ws, "C8:H8", "0.00")
    notes(ws, "A9", [
        "傾きb≒1: フォワードボラは将来のIVの偏りのない予測(期待仮説が成り立つ)。b<1: 期間構造の傾きほどはIVが動かない",
        "RMSE(フォワード) < RMSE(据え置き) なら、期間構造に将来のIVの情報がある。重なったサンプルなので回帰の有意性は過大",
        "T2のオプションの原資産(取引日+T2起算)と、到来日の(T2−T1)のオプションの原資産は同じFwd。T1とT2のFwdの違いは無視した近似",
    ])
    sc = ScatterChart()
    sc.title = "x = フォワード − 取引日IV / y = 到来日IV − 取引日IV"
    sc.style = 2
    sc.width, sc.height = 15, 11
    sc.x_axis.delete = False
    sc.y_axis.delete = False
    sc.x_axis.tickLblPos = "low"
    sc.legend = None
    sc.series.append(series(S7, (23, first, last), (24, first, last), f"$X${top}", markers=True, color="4472C4", sheet=ws))
    ws.add_chart(sc, "A13")
    ch = time_chart("フォワードボラ vs 到来日のIV(取引日の位置にプロット)", "bp/年", width=20, height=11)
    x = (19, first, last)
    ch.series.append(series(S7, x, (20, first, last), f"$T${top}", color="4472C4", sheet=ws))
    ch.series.append(series(S7, x, (21, first, last), f"$U${top}", color="C00000", sheet=ws))
    ch.series.append(series(S7, x, (22, first, last), f"$V${top}", color="A6A6A6", dash="dash", sheet=ws))
    ws.add_chart(ch, "I13")
    widths(ws, {"A": 22, "B": 11})


# ---------------------------------------------------------------- ⑧ 会合イベント

EVENT_EXPIRIES = EXPIRIES[:7]  # 1M〜2Y


def build_s8(book: Book) -> None:
    ws = book.sheet(S8)
    title(ws, "⑧ 会合イベント(注目原資産、基準日、試験的)",
          "総分散 w = σd²·τ + J²·(満期までの会合回数) を、使う満期で最小二乗(切片なし)。J = 会合1回の織り込み変動(bp)")
    names = ["満期", "満期の番号", "番号 j", "IV", "残存暦日", "満期日", "会合回数", "τ(年)", "総分散 w", "使う(1/0)",
             "y=w", "x1=τ", "x2=会合回数", "当てはめ w", "区間のフォワードボラ", "区間の会合回数"]
    header(ws, 6, 1, names)
    f0, l0 = 7, 6 + len(EVENT_EXPIRIES)
    for n, e in enumerate(EVENT_EXPIRIES):
        r = f0 + n
        k = EXPIRIES.index(e) + 1
        ws[f"A{r}"], ws[f"B{r}"] = e, k
        ws[f"C{r}"] = f"=(B{r}-1)*12+注目原資産番号"
        ws[f"D{r}"] = f'=IF(INDEX(IVデータ,基準行,C{r})="","",INDEX(IVデータ,基準行,C{r}))'
        ws[f"E{r}"] = f"=INDEX(残存暦日,基準行,B{r})"
        ws[f"F{r}"] = f"=実効基準日+E{r}"
        ws[f"G{r}"] = f'=COUNTIFS(会合日一覧,">"&実効基準日,会合日一覧,"<="&F{r})'
        ws[f"H{r}"] = f'=IF(IV時間軸="営業日",INDEX(残存営業日,基準行,B{r})/{N},E{r}/365)'
        ws[f"I{r}"] = f'=IF(D{r}="","",D{r}^2*H{r})'
        ws[f"J{r}"] = 1
        ws[f"J{r}"].fill = INPUT_FILL
        ws[f"K{r}"] = f'=IF(OR(I{r}="",J{r}<>1),0,I{r})'
        ws[f"L{r}"] = f'=IF(OR(I{r}="",J{r}<>1),0,H{r})'
        ws[f"M{r}"] = f'=IF(OR(I{r}="",J{r}<>1),0,G{r})'
        ws[f"N{r}"] = f'=IF(OR($B$17="",$B$18=""),"",H{r}*$B$18+G{r}*$B$17)'
        if n == 0:
            ws[f"O{r}"] = f"=D{r}"
            ws[f"P{r}"] = f"=G{r}"
        else:
            p = r - 1
            ws[f"O{r}"] = f'=IF(OR(I{r}="",I{p}=""),"",IF(I{r}-I{p}<=0,"負",SQRT((I{r}-I{p})/(H{r}-H{p}))))'
            ws[f"P{r}"] = f"=G{r}-G{p}"
        for c, f in zip("DFHIKLNO", ["0.0", DATE_FMT, "0.000", "0", "0", "0.000", "0", "0.0"]):
            ws[f"{c}{r}"].number_format = f
    lin = f"LINEST($K${f0}:$K${l0},$L${f0}:$M${l0},FALSE,FALSE)"
    rows = [
        ("A16", "推定結果"),
        ("A17", "J²(bp²/回)"), ("B17", f'=IFERROR(INDEX({lin},1,1),"")'),
        ("A18", "σd²(bp²/年)"), ("B18", f'=IFERROR(INDEX({lin},1,2),"")'),
        ("A19", "会合1回の織り込み変動 J(bp)"), ("B19", '=IF(B17="","",IF(B17<0,"推定不能(負)",SQRT(B17)))'),
        ("A20", "平常時のボラ σd(bp/年)"), ("B20", '=IF(B18="","",IF(B18<0,"推定不能(負)",SQRT(B18)))'),
        ("A21", "参考: 実際の会合日の変動RMS(④、注目銘柄、bp)"), ("B21", f"={q(S4)}!C5"),
        ("A22", "参考: 会合日以外の変動RMS(④、bp/日)"), ("B22", f"={q(S4)}!D5"),
    ]
    for ref, v in rows:
        ws[ref] = v
    ws["A16"].font = BOLD
    fmt(ws, "B17:B22", "0.00")
    notes(ws, "A24", [
        "会合回数は「基準日より後、満期日以前」の会合日シートの日付の数。使う列を0にした満期は推定から外れる",
        "会合はほぼ一定間隔(年8回)なので、τと会合回数は強く相関し、推定は不安定になりやすい(識別は1M・2M等の会合の有無の違いから)",
        "満期ごとに原資産のFwdが違うことは無視している。結果は目安として、④の実際の会合日の変動と見比べる",
    ])
    widths(ws, {"A": 34, "F": 11})


# ---------------------------------------------------------------- ⑨ GARCH

def build_s9(book: Book) -> None:
    ws = book.sheet(S9)
    title(ws, "⑨ GARCH(1,1)(注目銘柄のdF)",
          "σ²[t] = ω + α·dF[t−1]² + β·σ²[t−1](日次bp²)。ω = (σ̄²/年営業日数)(1−α−β)。黄色の3つをソルバーで動かす")
    top = 46
    first, last = time_rows(top)
    params = [
        ("A3", "長期ボラ σ̄(bp/年)", "B3", 50), ("A4", "α(ショックへの反応)", "B4", 0.05), ("A5", "β(持続性)", "B5", 0.9),
    ]
    for la, text, ref, v in params:
        ws[la] = text
        ws[ref] = v
        ws[ref].fill = INPUT_FILL
    rng = lambda c: f"${c}${first}:${c}${last}"  # noqa: E731
    derived = [
        ("A6", "α+β(1に近いほどゆっくり平均に戻る)", "B6", "=B4+B5", "0.0000"),
        ("A7", "ω(日次bp²)", "B7", f"=(B3^2/{N})*(1-B6)", "0.0000"),
        ("A8", "対数尤度(ソルバーで最大化する)", "B8", f"=SUM({rng('F')})", "0.00"),
        ("A9", "推定に使った日数(母集団)", "B9", f"=SUM({rng('D')})", "0"),
        ("A10", "半減期(営業日)", "B10", '=IFERROR(LN(0.5)/LN(B6),"")', "0.0"),
        ("A11", "参考: 母集団の標本RV(bp/年)", "B11", "", "0.0"),  # 式はO列(dF²)を作ってから入れる
        ("A12", "翌営業日の予測分散(日次bp²)", "B12",
         f'=IF(件数<2,"",B7+B4*IF(INDEX({rng("C")},基準行)="",INDEX({rng("E")},基準行),INDEX({rng("C")},基準行)^2)'
         f'+B5*INDEX({rng("E")},基準行))', "0.000"),
        ("A13", "長期の分散(日次bp²)", "B13", f"=B3^2/{N}", "0.000"),
    ]
    for la, text, ref, v, f in derived:
        ws[la] = text
        ws[ref] = v
        ws[ref].number_format = f
    ws["A8"].font = BOLD
    notes(ws, "A15", [
        "■ ソルバーでの推定(最尤法)",
        " データ → ソルバー(無ければ ファイル → オプション → アドイン → Excelアドイン → ソルバーにチェック)",
        " 目的セル $B$8 を最大化 / 変数セル $B$3:$B$5",
        " 制約: $B$3>=1、$B$4>=0、$B$5>=0、$B$6<=0.9999 / 解決方法: GRG非線形",
        " 初期値の目安: σ̄ = B11(標本RV)、α=0.05、β=0.90",
        "■ 読み方",
        " α大: 大きく動いた翌日にボラ予測がすぐ上がる / β大: 一度上がったボラが長く続く / 半減期: 長期ボラとの差が半分になる日数",
        " 右の表: 基準日から各満期までの平均予測ボラ(期間構造)。満期ごとに原資産のFwdが違うことは無視(注目銘柄のdFで推定)",
    ])
    cols = [
        ("日付", 'IF($A{r}>件数,"",INDEX(日付,$A{r}))', DATE_FMT),                                       # B
        ("dF(bp)", data_at("dFデータ", "$A{r}", "注目番号"), "0.00"),                                    # C
        ("推定に使う", 'IF(AND($A{r}<=件数,$A{r}>=MAX(3,母集団開始行),$A{r}<=母集団終了行,C{r}<>""),1,0)', "0"),  # D
        ("σ²(日次bp²)", 'IF($A{r}>件数,"",IF($A{r}<=2,$B$13,$B$7+$B$4*IF(C{p}="",E{p},C{p}^2)+$B$5*E{p}))', "0.000"),  # E
        ("対数尤度", 'IF(D{r}=1,-0.5*(LN(2*PI())+LN(E{r})+C{r}^2/E{r}),"")', "0.000"),                     # F
        ("条件付きボラ(bp/年)", 'IF(E{r}="","",SQRT(' + N + '*E{r}))', "0.0"),                           # G
        ("IV", data_at("IVデータ", "$A{r}", "注目番号"), "0.0"),                                         # H
        ("EWMA", f"INDEX({q(S1)}!$Q$47:$Q${46 + MAX_ROWS},$A{{r}})", "0.0"),                               # I
        ("表示", "AND($A{r}<=件数,$A{r}>=表示開始行)", None),                                             # J
        ("日付(グラフ)", "IF(J{r},B{r},NA())", DATE_FMT),                                                 # K
        ("GARCH", 'IF(AND(J{r},G{r}<>""),G{r},NA())', "0.0"),                                           # L
        ("IV(グラフ)", 'IF(AND(J{r},H{r}<>""),H{r},NA())', "0.0"),                                       # M
        ("EWMA(グラフ)", 'IF(AND(J{r},I{r}<>""),I{r},NA())', "0.0"),                                     # N
    ]
    write_rows(ws, top, cols)
    # B11の標本RV用(dF²)。SUMPRODUCTで""を扱えないので別の列(O)に二乗を置く
    ws[f"O{top}"] = "dF²"
    ws[f"O{top}"].font = BOLD
    for r in range(first, last + 1):
        ws[f"O{r}"] = f'=IF(C{r}="",0,C{r}^2)'
    ws["B11"] = f'=IFERROR(SQRT({N}*SUMPRODUCT({rng("D")},{rng("O")})/B9),"")'

    # 予測の期間構造(J〜O、2〜18行)
    header(ws, 2, 10, ["満期", "残存営業日 H", "GARCH予測ボラ", "IV", "IV−GARCH", "EWMA(一定)"])
    for k, e in enumerate(EXPIRIES, start=1):
        r = 2 + k
        ws[f"J{r}"] = e
        ws[f"K{r}"] = f"=INDEX(残存営業日,基準行,{k})"
        avg = f"($B$13+($B$12-$B$13)*(1-$B$6^K{r})/((1-$B$6)*K{r}))"
        ws[f"L{r}"] = f'=IF(OR($B$12="",K{r}<=0),"",IF($B$6>=1,SQRT({N}*$B$12),SQRT({N}*{avg})))'
        iv = f"INDEX(IVデータ,基準行,({k}-1)*12+注目原資産番号)"
        ws[f"M{r}"] = f'=IF({iv}="","",{iv})'
        ws[f"N{r}"] = f'=IF(OR(L{r}="",M{r}=""),"",M{r}-L{r})'
        ws[f"O{r}"] = f"=INDEX({q(S1)}!$Q$47:$Q${46 + MAX_ROWS},基準行)"
        fmt(ws, f"L{r}:O{r}", "0.0")
    l0 = 2 + len(EXPIRIES)
    ch = category_chart("予測ボラの期間構造 vs IV(bp/年)", "bp/年", width=16, height=9)
    ch.series.append(series(S9, None, (13, 3, l0), "$M$2", color="C00000", width=22225, sheet=ws))
    ch.series.append(series(S9, None, (12, 3, l0), "$L$2", color="4472C4", sheet=ws))
    ch.series.append(series(S9, None, (15, 3, l0), "$O$2", color="000000", dash="dash", sheet=ws))
    ch.set_categories(Reference(ws, min_col=10, min_row=3, max_row=l0))
    ws.add_chart(ch, "Q2")
    x = (11, first, last)
    ch2 = time_chart("条件付きボラ(GARCH) vs IV vs EWMA(bp/年)", "bp/年", width=30, height=9)
    ch2.series.append(series(S9, x, (13, first, last), f"$M${top}", color="C00000", sheet=ws))
    ch2.series.append(series(S9, x, (12, first, last), f"$L${top}", color="4472C4", sheet=ws))
    ch2.series.append(series(S9, x, (14, first, last), f"$N${top}", color="A6A6A6", dash="dash", sheet=ws))
    ws.add_chart(ch2, "A25")
    widths(ws, {"A": 34, "B": 11, "J": 8, "K": 12, "L": 13})
    ws.freeze_panes = f"C{first}"


# ---------------------------------------------------------------- 全体

def build(path: Path) -> None:
    book = Book()
    build_readme(book)
    build_settings(book)
    build_s1(book)
    build_s2(book)
    build_s3(book)
    build_s4(book)
    build_s5(book)
    build_s6(book)
    build_s7(book)
    build_s8(book)
    build_s9(book)
    build_meetings(book)
    build_data_sheets(book)
    book.wb.calculation.fullCalcOnLoad = True
    book.wb.save(path)
