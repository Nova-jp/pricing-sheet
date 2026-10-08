"""
作業用のブック(リポジトリ直下。gitignore済み)から、LSEGのデータを含まない雛形(templates/)を作る・検査する。

    python -m tools.template          # 直下のブック → templates/ に雛形を書き出す
    python -m tools.template --check  # templates/ の雛形を検査する(pre-commitフック・GitHub Actionsが実行)

場所ごとに消すのではなく、規則で一律に消す(LSEGのデータはほぼ全て数式の計算結果として入るため):
- 数式のセルの計算結果(キャッシュ値)を消す。数式と、手入力の値(コンベンション・フラグ等)は残す
- スピル範囲(t="array" の ref)のうち先頭以外のセルの値を消す(スピル結果は値のセルとして保存される)
- RECEIVE_AREAS に宣言した受信領域(LSEGアドインが値として書き込む範囲)を消す
- グラフのキャッシュ、外部リンクのキャッシュ、RTDのキャッシュ(volatileDependencies)を消す
- ローカルの絶対パスと、作成者・更新者の名前を消す。開いたときに全再計算する設定にする

検査は「雛形にもう一度同じ処理をかけても変わらないこと」で行う(作る処理と検査の判断が食い違わない)。
これに、知らない部品が無いこと、ローカルのパスが無いこと、配列UDFのセルが動的配列(cm属性)のままで
あることを加える。標準ライブラリだけで動く(QuantLib・Excelは不要)。
"""

import argparse
import ast
import io
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = ROOT / "templates"

# 雛形にするブック → {シート名: その行以降を消す行番号}(LSEGアドインが値として書き込む受信領域)
RECEIVE_AREAS: Dict[str, Dict[str, int]] = {
    "PricingSheet.xlsm": {},
    "SwapAnalysis.xlsm": {},
    "historical_data.xlsx": {"Raw": 3, "RawFixing": 3, "RawVol": 3},
}

# 雛形に入ってよい部品(これ以外があれば検査で失敗させ、消す対象か許可する対象かを人が判断する)
ALLOWED_PARTS = [re.compile(p) for p in (
    r"\[Content_Types\]\.xml",
    r"_rels/\.rels",
    r"docProps/(core|app)\.xml",
    r"xl/workbook\.xml",
    r"xl/_rels/workbook\.xml\.rels",
    r"xl/worksheets/sheet\d+\.xml",
    r"xl/worksheets/_rels/sheet\d+\.xml\.rels",
    r"xl/theme/theme\d+\.xml",
    r"xl/styles\.xml",
    r"xl/sharedStrings\.xml",
    r"xl/metadata\.xml",
    r"xl/calcChain\.xml",
    r"xl/vbaProject\.bin",
    r"xl/customProperty\d+\.bin",  # LSEGアドインの関数の呼び出し条件(RIC一覧・期間・出力範囲)
    r"xl/printerSettings/printerSettings\d+\.bin",
    r"xl/webextensions/(taskpanes|webextension\d+)\.xml",
    r"xl/webextensions/_rels/taskpanes\.xml\.rels",
    r"xl/drawings/drawing\d+\.xml",
    r"xl/drawings/_rels/drawing\d+\.xml\.rels",
    r"xl/charts/(chart|style|colors)\d+\.xml",
    r"xl/charts/_rels/chart\d+\.xml\.rels",
    r"xl/externalLinks/externalLink\d+\.xml",
    r"xl/externalLinks/_rels/externalLink\d+\.xml\.rels",
)]

# 丸ごと削除する部品(Excelが開いたときに作り直す)
REMOVED_PARTS = {"xl/volatileDependencies.xml"}  # RTD(RtGet等)の受信値のキャッシュ

# ローカルのパスの検査から除く部品。vbaProject.binにはxlwingsアドインへの参照(絶対パス)が
# バイナリで入っていて消せない(既知・CLAUDE.md参照)
PATH_CHECK_EXEMPT = {"xl/vbaProject.bin"}
LOCAL_PATH_PATTERNS = (re.compile(rb"[a-z]:\\users\\", re.I), re.compile(rb"file:///", re.I))

UDF_MODULES = (ROOT / "swap_pricing" / "udf.py", ROOT / "swap_analysis" / "udf.py")

_CELL = re.compile(r'<c r="([A-Z]+)(\d+)"([^>]*?)(?:/>|>(.*?)</c>)', re.S)
_REF = re.compile(r"([A-Z]+)(\d+)")

Parts = List[Tuple[str, bytes]]


def _col_number(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - ord("A") + 1
    return n


def _range_bounds(ref: str) -> Tuple[int, int, int, int]:
    """"A28:D494" → (行の始め, 行の終わり, 列の始め, 列の終わり)。"""
    first, _, last = ref.partition(":")
    c1, r1 = _REF.fullmatch(first).groups()
    c2, r2 = _REF.fullmatch(last or first).groups()
    return int(r1), int(r2), _col_number(c1), _col_number(c2)


def _spill_cells(xml: str) -> Set[Tuple[int, int]]:
    """配列数式(t="array")の ref 範囲に入るセル(行, 列)。先頭セル(数式のあるセル)は数式として扱われる。"""
    cells = set()
    for m in _CELL.finditer(xml):
        f = re.match(r"<f([^>]*)", m.group(4) or "")
        if f and 't="array"' in f.group(1):
            ref = re.search(r'ref="([^"]+)"', f.group(1))
            if ref:
                r1, r2, c1, c2 = _range_bounds(ref.group(1))
                cells.update((r, c) for r in range(r1, r2 + 1) for c in range(c1, c2 + 1))
    return cells


def clean_sheet(xml: str, clear_from_row: Optional[int] = None) -> str:
    """シートのセルから計算結果・スピル結果・受信領域の値を消す(数式と手入力の値は残す)。"""
    spill = _spill_cells(xml)

    def in_spill(row: int, col: int) -> bool:
        return (row, col) in spill

    def repl(m: re.Match) -> str:
        letters, row_text, attrs, body = m.group(1), m.group(2), m.group(3), m.group(4) or ""
        row, col = int(row_text), _col_number(letters)
        f = re.search(r"<f[^>]*/>|<f[^>]*>.*?</f>", body, re.S)
        if f:
            # 計算結果(v)と値の型(t)・値のメタデータ(vm)を消し、数式だけ残す
            attrs = re.sub(r'\s(t|vm)="[^"]*"', "", attrs)
            return f'<c r="{letters}{row_text}"{attrs}>{f.group(0)}</c>'
        if (clear_from_row is not None and row >= clear_from_row) or in_spill(row, col):
            style = re.search(r'\ss="[^"]*"', attrs)
            return f'<c r="{letters}{row_text}"{style.group(0)}/>' if style else ""
        return m.group(0)

    return _CELL.sub(repl, xml)


def _used_string_indices(sheets: List[str]) -> Set[int]:
    used = set()
    for xml in sheets:
        for m in _CELL.finditer(xml):
            if 't="s"' in m.group(3):
                v = re.search(r"<v>(\d+)</v>", m.group(4) or "")
                if v:
                    used.add(int(v.group(1)))
    return used


def clean_shared_strings(xml: str, used: Set[int]) -> str:
    """どのセルからも参照されない文字列を空にする(番号がずれないよう要素は残す)。"""
    index = iter(range(10 ** 9))

    def repl(m: re.Match) -> str:
        return m.group(0) if next(index) in used else "<si><t></t></si>"

    return re.sub(r"<si>.*?</si>|<si/>", repl, xml, flags=re.S)


def clean_chart(xml: str) -> str:
    return re.sub(r"<c:(numCache|strCache)>.*?</c:\1>", "", xml, flags=re.S)


def clean_external_link(xml: str) -> str:
    # 空の <sheetData sheetId="0"/> を巻き込まないよう、開始タグは「/>」で終わらないものだけ
    xml = re.sub(r'(<sheetData sheetId="\d+")[^>/]*>.*?</sheetData>', r"\1/>", xml, flags=re.S)
    return re.sub(r"<xxl21:alternateUrls>.*?</xxl21:alternateUrls>", "", xml, flags=re.S)


def clean_workbook(xml: str) -> str:
    xml = re.sub(r"<mc:AlternateContent[^>]*><mc:Choice[^>]*><x15ac:absPath[^>]*/></mc:Choice></mc:AlternateContent>",
                 "", xml)
    if "fullCalcOnLoad" not in xml:
        xml = re.sub(r"<calcPr\b", '<calcPr fullCalcOnLoad="1"', xml, count=1)
    return xml


def clean_core_properties(xml: str) -> str:
    return re.sub(r"<(dc:creator|cp:lastModifiedBy)>[^<]*</\1>", r"<\1></\1>", xml)


def _remove_part_references(name: str, xml: str) -> str:
    """[Content_Types].xml と rels から、削除する部品・ローカルの絶対パスへの参照を消す。"""
    for part in REMOVED_PARTS:
        if name == "[Content_Types].xml":
            xml = re.sub(r'<Override PartName="/%s"[^>]*/>' % re.escape(part), "", xml)
        elif name == "xl/_rels/workbook.xml.rels":
            target = part.removeprefix("xl/")
            xml = re.sub(r'<Relationship [^>]*Target="/?(xl/)?%s"[^>]*/>' % re.escape(target), "", xml)
    if name.startswith("xl/externalLinks/_rels/"):
        xml = re.sub(r'<Relationship [^>]*Target="file:///[^"]*"[^>]*/>', "", xml)
    return xml


def _sheet_paths(parts: Dict[str, bytes]) -> Dict[str, str]:
    """シート名 → 部品名(xl/worksheets/sheetN.xml)。"""
    workbook = parts["xl/workbook.xml"].decode("utf-8")
    rels = parts["xl/_rels/workbook.xml.rels"].decode("utf-8")
    targets = {}
    for rel in re.findall(r"<Relationship [^>]*>", rels):
        rid = re.search(r'Id="([^"]+)"', rel).group(1)
        targets[rid] = "xl/" + re.search(r'Target="([^"]+)"', rel).group(1).lstrip("/").removeprefix("xl/")
    return {name: targets[rid] for name, rid in re.findall(r'<sheet name="([^"]+)"[^>]*r:id="([^"]+)"', workbook)}


def make_template(parts: Parts, receive_areas: Dict[str, int]) -> Parts:
    """ブックの部品の並び → 雛形の部品の並び(順序は保つ)。"""
    contents = dict(parts)
    sheet_paths = _sheet_paths(contents)
    unknown = set(receive_areas) - set(sheet_paths)
    if unknown:
        raise ValueError(f"受信領域に宣言したシートがありません: {sorted(unknown)}")
    clear_rows = {sheet_paths[s]: row for s, row in receive_areas.items()}

    cleaned: Dict[str, str] = {}
    for name, data in parts:
        if not name.endswith((".xml", ".rels")) or name in REMOVED_PARTS:
            continue
        xml = data.decode("utf-8")
        if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name):
            xml = clean_sheet(xml, clear_rows.get(name))
        elif re.fullmatch(r"xl/charts/chart\d+\.xml", name):
            xml = clean_chart(xml)
        elif re.fullmatch(r"xl/externalLinks/externalLink\d+\.xml", name):
            xml = clean_external_link(xml)
        elif name == "xl/workbook.xml":
            xml = clean_workbook(xml)
        elif name == "docProps/core.xml":
            xml = clean_core_properties(xml)
        cleaned[name] = _remove_part_references(name, xml)

    if "xl/sharedStrings.xml" in cleaned:
        sheets = [v for k, v in cleaned.items() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", k)]
        cleaned["xl/sharedStrings.xml"] = clean_shared_strings(
            cleaned["xl/sharedStrings.xml"], _used_string_indices(sheets))

    return [(name, cleaned[name].encode("utf-8") if name in cleaned else data)
            for name, data in parts if name not in REMOVED_PARTS]


def array_udf_names(modules=UDF_MODULES) -> Set[str]:
    """配列を返すUDF(戻り値の型が List[...] の @xw.func)の名前。udf.py から読むので一覧の手入力は不要。"""
    names = set()
    for path in modules:
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if (isinstance(node, ast.FunctionDef) and node.returns is not None
                    and ast.unparse(node.returns).startswith("List[")
                    and any("func" in ast.unparse(d) for d in node.decorator_list)):
                names.add(node.name)
    return names


def check_template(parts: Parts, receive_areas: Dict[str, int], array_udfs: Set[str]) -> List[str]:
    """雛形の問題点の一覧(空なら合格)。"""
    problems = []
    contents = dict(parts)
    for name in contents:
        if name in REMOVED_PARTS:
            problems.append(f"削除すべき部品が残っています(python -m tools.template で作り直す): {name}")
        elif not any(p.fullmatch(name) for p in ALLOWED_PARTS):
            problems.append(f"知らない部品があります(データが入る場所か確認し、tools/template.pyに追加): {name}")
        if name not in PATH_CHECK_EXEMPT and any(p.search(contents[name]) for p in LOCAL_PATH_PATTERNS):
            problems.append(f"ローカルのパスが入っています: {name}")
    try:
        remade = dict(make_template(parts, receive_areas))
    except (ValueError, KeyError, AttributeError) as exc:
        return problems + [f"雛形の処理に失敗しました: {exc}"]
    for name, data in parts:
        if name in remade and remade[name] != data:
            problems.append(f"データが残っています(python -m tools.template で作り直す): {name}")
    for name, xml in ((n, d.decode("utf-8")) for n, d in parts if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)):
        for m in _CELL.finditer(xml):
            f = re.search(r"<f[^>]*>([^<]*)</f>", m.group(4) or "")
            if f and 'cm="' not in m.group(3) and any(re.search(r"\b%s\(" % u, f.group(1)) for u in array_udfs):
                problems.append(f"配列UDFのセルが動的配列ではありません(.Formula2で入れ直す): {name} {m.group(1)}{m.group(2)}")
    return problems


def _read(source) -> Parts:
    """ファイルのパス、またはブックのバイト列 → 部品の並び。"""
    with zipfile.ZipFile(io.BytesIO(source) if isinstance(source, bytes) else source) as z:
        return [(info.filename, z.read(info)) for info in z.infolist()]


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True)


def _workbooks_outside_templates() -> List[str]:
    """templates/ 以外にあるgit管理下のブック。git ls-files はインデックス(次にコミットされる中身)を
    列挙するので、HEADとの差分の有無に関係なく見つかる(差分で探すと、HEADと同じ内容を
    add -f したブックを見逃す)。"""
    names = _git("ls-files", "-z").stdout.decode("utf-8", "replace").split("\0")
    return [n for n in names if n.lower().endswith((".xlsx", ".xlsm", ".xls")) and not n.startswith("templates/")]


def _write(path: Path, parts: Parts, source: Path) -> None:
    with zipfile.ZipFile(source) as zin:
        infos = {info.filename: info for info in zin.infolist()}
    tmp = path.with_suffix(path.suffix + ".tmp")
    with zipfile.ZipFile(tmp, "w") as zout:
        for name, data in parts:
            zout.writestr(infos[name], data, compress_type=zipfile.ZIP_DEFLATED)
    tmp.replace(path)


def make_all() -> int:
    TEMPLATE_DIR.mkdir(exist_ok=True)
    status = 0
    for book, areas in RECEIVE_AREAS.items():
        source = ROOT / book
        if not source.exists():
            print(f"スキップ(作業用のブックがありません): {book}")
            continue
        if (ROOT / f"~${book}").exists():
            print(f"エラー: {book} がExcelで開かれています。保存して閉じてから実行してください")
            status = 1
            continue
        parts = make_template(_read(source), areas)
        _write(TEMPLATE_DIR / book, parts, source)
        print(f"作成: templates/{book}")
    return status or check_all()


def check_all(staged: bool = False) -> int:
    """templates/ の雛形を検査する。staged=True ならファイルではなくステージ済みの内容(コミットされる中身)を検査する。"""
    array_udfs = array_udf_names()
    status = 0
    for book, areas in RECEIVE_AREAS.items():
        if staged:
            shown = _git("show", f":templates/{book}")
            source = shown.stdout if shown.returncode == 0 else None
        else:
            source = TEMPLATE_DIR / book if (TEMPLATE_DIR / book).exists() else None
        if source is None:
            print(f"NG templates/{book}: ありません")
            status = 1
            continue
        problems = check_template(_read(source), areas, array_udfs)
        print(f"{'NG' if problems else 'OK'} templates/{book}")
        for p in problems:
            print(f"  - {p}")
        status |= bool(problems)
    if not staged:
        extra = sorted(p.name for p in TEMPLATE_DIR.glob("*") if p.name not in RECEIVE_AREAS)
        if extra:
            print(f"NG templates/ に未登録のファイルがあります(RECEIVE_AREASに追加する): {extra}")
            status = 1
    outside = _workbooks_outside_templates()
    if outside:
        print(f"NG 作業用のブックがgitに入っています(コミットしてよいのは templates/ だけ): {outside}")
        status = 1
    return status


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--check", action="store_true", help="templates/ の雛形を検査するだけ")
    parser.add_argument("--staged", action="store_true", help="--check でステージ済みの内容を検査する(pre-commitフック用)")
    args = parser.parse_args(argv)
    return check_all(staged=args.staged) if args.check else make_all()


if __name__ == "__main__":
    sys.exit(main())
