"""
ブック(xlsx/xlsm の部品の並び)の構造をテキストにする。雛形の差分をgitとPRで行単位に読むため。

書き出すもの: シートの並びと表示状態、定義名、セルの数式と手入力の値、入力規則、条件付き書式、
グラフが参照する範囲。書式(色・罫線・列幅)とVBAは書き出さない(差分の雑音になる/標準ライブラリで読めない)。

- 1行に1つ、シート名から書く(`pricing!A4:A103 = PricingRow(A4)`)。差分の1行だけで場所がわかるように
- 同じ数式を縦・横にコピーしたセルは1行にまとめる。「同じ」は参照をセルからの相対位置(R1C1)で比べる
  (Excelがまとめて保存する共有数式は、先頭のセルの数式をずらして展開する)
- `{= …}` は旧式の配列数式(CSE)。動的配列(スピル)は普通の数式と同じ `=` で書く(スピルの範囲は
  データで変わるので書かない)
- `_xlfn.` 等の接頭辞は読みやすさのため外す。`_xludf.`(保存したときにUDFが登録されていなかった)は残す
"""

import html
import posixpath
import re
from typing import Callable, Dict, List, Optional, Tuple

Parts = List[Tuple[str, bytes]]

_CELL = re.compile(r'<c r="([A-Z]+)(\d+)"([^>]*?)(?:/>|>(.*?)</c>)', re.S)
_FORMULA = re.compile(r"<f([^>]*)/>|<f([^>]*)>(.*?)</f>", re.S)

# 数式の中の参照(セル・列の範囲・行の範囲)。文字列リテラルと引用符付きのシート名の外だけを見る。
# 前後が英数字・_・.(定義名・関数名・LAMBDAの引数 _xlpm.x)、後ろが ( !(関数・シート名)なら参照ではない
_QUOTED = re.compile(r'"(?:[^"]|"")*"|\'(?:[^\']|\'\')*\'')
_REF = re.compile(
    r"(?<![A-Za-z0-9_.])(?:"
    r"(?P<cc1>\$?)(?P<c1>[A-Z]{1,3}):(?P<cc2>\$?)(?P<c2>[A-Z]{1,3})(?![0-9A-Za-z_(.!])"
    r"|(?P<rr1>\$?)(?P<r1>[0-9]{1,7}):(?P<rr2>\$?)(?P<r2>[0-9]{1,7})(?![0-9A-Za-z_(.!:])"
    r"|(?P<ac>\$?)(?P<col>[A-Z]{1,3})(?P<ar>\$?)(?P<row>[0-9]{1,7})(?![0-9A-Za-z_(.!])"
    r")"
)
_MAX_COL, _MAX_ROW = 16384, 1048576
_PREFIXES = re.compile(r"_xlfn\.|_xlws\.|_xlpm\.")


def col_number(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - ord("A") + 1
    return n


def col_letters(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(ord("A") + r) + s
    return s


def _map_refs(formula: str, fn: Callable[[re.Match], Optional[str]]) -> str:
    """引用の外にある参照だけを fn で置き換える(fn が None を返したらそのまま)。"""
    out, pos = [], 0
    for q in _QUOTED.finditer(formula):
        out.append(_REF.sub(lambda m: fn(m) or m.group(0), formula[pos:q.start()]))
        out.append(q.group(0))
        pos = q.end()
    out.append(_REF.sub(lambda m: fn(m) or m.group(0), formula[pos:]))
    return "".join(out)


def _is_ref(m: re.Match) -> bool:
    if m.group("col") is not None:
        return col_number(m.group("col")) <= _MAX_COL and 1 <= int(m.group("row")) <= _MAX_ROW
    if m.group("c1") is not None:
        return col_number(m.group("c1")) <= _MAX_COL and col_number(m.group("c2")) <= _MAX_COL
    return 1 <= int(m.group("r1")) <= _MAX_ROW and 1 <= int(m.group("r2")) <= _MAX_ROW


def relative_key(formula: str, row: int, col: int) -> str:
    """(row, col) のセルにある数式を、相対参照をセルからの距離にした形(R1C1)にする。コピーした数式は同じ形になる。"""
    def rel(absolute: str, value: int, base: int, axis: str) -> str:
        return f"{axis}{value}" if absolute else f"{axis}[{value - base}]"

    def fn(m: re.Match) -> Optional[str]:
        if not _is_ref(m):
            return None
        if m.group("col") is not None:
            return (rel(m.group("ar"), int(m.group("row")), row, "R")
                    + rel(m.group("ac"), col_number(m.group("col")), col, "C"))
        if m.group("c1") is not None:
            return (rel(m.group("cc1"), col_number(m.group("c1")), col, "C") + ":"
                    + rel(m.group("cc2"), col_number(m.group("c2")), col, "C"))
        return (rel(m.group("rr1"), int(m.group("r1")), row, "R") + ":"
                + rel(m.group("rr2"), int(m.group("r2")), row, "R"))

    return _map_refs(formula, fn)


def shift_formula(formula: str, d_row: int, d_col: int) -> str:
    """数式の相対参照を d_row 行・d_col 列ずらす(Excelがセルをコピーしたときと同じ)。"""
    if d_row == 0 and d_col == 0:
        return formula

    def moved(absolute: str, value: int, d: int, limit: int) -> Optional[int]:
        v = value if absolute else value + d
        return v if 1 <= v <= limit else None

    def fn(m: re.Match) -> Optional[str]:
        if not _is_ref(m):
            return None
        if m.group("col") is not None:
            c = moved(m.group("ac"), col_number(m.group("col")), d_col, _MAX_COL)
            r = moved(m.group("ar"), int(m.group("row")), d_row, _MAX_ROW)
            if c is None or r is None:
                return "#REF!"
            return f"{m.group('ac')}{col_letters(c)}{m.group('ar')}{r}"
        if m.group("c1") is not None:
            c1 = moved(m.group("cc1"), col_number(m.group("c1")), d_col, _MAX_COL)
            c2 = moved(m.group("cc2"), col_number(m.group("c2")), d_col, _MAX_COL)
            if c1 is None or c2 is None:
                return "#REF!"
            return f"{m.group('cc1')}{col_letters(c1)}:{m.group('cc2')}{col_letters(c2)}"
        r1 = moved(m.group("rr1"), int(m.group("r1")), d_row, _MAX_ROW)
        r2 = moved(m.group("rr2"), int(m.group("r2")), d_row, _MAX_ROW)
        if r1 is None or r2 is None:
            return "#REF!"
        return f"{m.group('rr1')}{r1}:{m.group('rr2')}{r2}"

    return _map_refs(formula, fn)


def _one_line(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")


def _show_formula(formula: str) -> str:
    return _one_line(_PREFIXES.sub("", formula))


def _shared_strings(xml: Optional[str]) -> List[str]:
    if xml is None:
        return []
    out = []
    for si in re.findall(r"<si>(.*?)</si>|<si/>", xml, flags=re.S):
        si = re.sub(r"<rPh\b.*?</rPh>", "", si, flags=re.S)  # ふりがな
        out.append(html.unescape("".join(re.findall(r"<t[^>]*>(.*?)</t>", si, flags=re.S))))
    return out


def _constant(attrs: str, body: str, strings: List[str]) -> Optional[str]:
    t = re.search(r'\st="([^"]*)"', attrs)
    t = t.group(1) if t else "n"
    if t == "inlineStr":
        return '"%s"' % _one_line(html.unescape("".join(re.findall(r"<t[^>]*>(.*?)</t>", body, flags=re.S))))
    v = re.search(r"<v>(.*?)</v>", body, flags=re.S)
    if v is None:
        return None
    value = html.unescape(v.group(1))
    if t == "s":
        index = int(value)
        return '"%s"' % _one_line(strings[index] if index < len(strings) else "")
    if t == "str":
        return '"%s"' % _one_line(value)
    if t == "b":
        return "TRUE" if value == "1" else "FALSE"
    return value  # 数値・エラー値


def _cells(sheet: str, xml: str, strings: List[str]) -> List[str]:
    """セルの数式・値を、同じ内容の縦横の並びをまとめた行にする。"""
    matches = list(_CELL.finditer(xml))
    masters: Dict[str, Tuple[str, int, int]] = {}
    for m in matches:
        f = _FORMULA.search(m.group(4) or "")
        if f and f.group(3) is not None and 't="shared"' in f.group(2):
            si = re.search(r'si="(\d+)"', f.group(2)).group(1)
            masters[si] = (html.unescape(f.group(3)), int(m.group(2)), col_number(m.group(1)))

    # (行, 列) → (表示の種類, 比べるための形, 先頭のセルで表示するための (数式, 元の行, 元の列) か値)
    entries: Dict[Tuple[int, int], Tuple[str, str, object]] = {}
    for m in matches:
        row, col, attrs, body = int(m.group(2)), col_number(m.group(1)), m.group(3), m.group(4) or ""
        f = _FORMULA.search(body)
        if f:
            fattrs = f.group(1) if f.group(1) is not None else f.group(2)
            text = html.unescape(f.group(3)) if f.group(3) is not None else ""
            if 't="shared"' in fattrs and not text:
                si = re.search(r'si="(\d+)"', fattrs)
                if si is None or si.group(1) not in masters:
                    continue
                text, src_row, src_col = masters[si.group(1)]
            elif not text and 't="dataTable"' not in fattrs:
                continue  # 揮発性の関数のスピル先に置かれる空の数式(<f ca="1"/>)。スピルの範囲はデータで変わる
            else:
                src_row, src_col = row, col
            if 't="dataTable"' in fattrs:
                kind, text = "=", "TABLE(%s)" % fattrs.strip()
            else:
                kind = "{=" if 't="array"' in fattrs and 'cm="' not in attrs else "="
            entries[(row, col)] = (kind, relative_key(text, src_row, src_col), (text, src_row, src_col))
        else:
            value = _constant(attrs, body, strings)
            if value is not None:
                entries[(row, col)] = (":", value, value)

    # 縦の並び(同じ列で行が続き、形が同じ) → それを横に並べた長方形
    by_col: Dict[int, List[int]] = {}
    for row, col in entries:
        by_col.setdefault(col, []).append(row)
    runs: Dict[Tuple[int, int, str, str], List[int]] = {}
    for col, rows in by_col.items():
        rows.sort()
        start = prev = rows[0]
        for row in rows[1:] + [None]:
            if row is not None and row == prev + 1 and entries[(row, col)][:2] == entries[(start, col)][:2]:
                prev = row
                continue
            kind, key, _ = entries[(start, col)]
            runs.setdefault((start, prev, kind, key), []).append(col)
            if row is not None:
                start = prev = row

    rects = []
    for (r1, r2, kind, key), cols in runs.items():
        cols.sort()
        c1 = prev = cols[0]
        for col in cols[1:] + [None]:
            if col is not None and col == prev + 1:
                prev = col
                continue
            rects.append((r1, c1, r2, prev, kind))
            if col is not None:
                c1 = prev = col

    lines = []
    for r1, c1, r2, c2, kind in sorted(rects):
        ref = f"{col_letters(c1)}{r1}" + ("" if (r1, c1) == (r2, c2) else f":{col_letters(c2)}{r2}")
        _, _, source = entries[(r1, c1)]
        if kind == ":":
            lines.append(f"{sheet}!{ref} : {source}")
        else:
            text, src_row, src_col = source
            shown = _show_formula(shift_formula(text, r1 - src_row, c1 - src_col))
            lines.append(f"{sheet}!{ref} = {shown}" if kind == "=" else f"{sheet}!{ref} {{= {shown}}}")
    return lines


def _sqref(text: str) -> str:
    return " ".join(sorted(text.split(), key=lambda r: (int(re.search(r"\d+", r).group(0)) if re.search(r"\d", r) else 0, r)))


def _validations(sheet: str, xml: str) -> List[str]:
    lines = []
    for attrs, body in re.findall(r"<dataValidation\b([^>]*?)(?:/>|>(.*?)</dataValidation>)", xml, flags=re.S):
        lines.append(_validation_line(sheet, attrs, body, re.search(r'sqref="([^"]*)"', attrs)))
    # 別のシートを参照する入力規則は拡張(x14)に入る
    for attrs, body in re.findall(r"<x14:dataValidation\b([^>]*?)>(.*?)</x14:dataValidation>", xml, flags=re.S):
        lines.append(_validation_line(sheet, attrs, body, re.search(r"<xm:sqref>(.*?)</xm:sqref>", body)))
    return sorted(lines)


def _validation_line(sheet: str, attrs: str, body: str, sqref: Optional[re.Match]) -> str:
    kind = re.search(r'type="([^"]*)"', attrs)
    op = re.search(r'operator="([^"]*)"', attrs)
    formulas = [html.unescape(f) for f in re.findall(
        r"<(?:x14:)?formula[12]>(?:<xm:f>)?(.*?)(?:</xm:f>)?</(?:x14:)?formula[12]>", body or "", flags=re.S)]
    head = " ".join(x for x in (kind.group(1) if kind else "any", op.group(1) if op else "") if x)
    return f"{sheet}!{_sqref(sqref.group(1)) if sqref else '?'} validation {head}: " + " | ".join(
        _show_formula(f) for f in formulas)


def _conditional_formats(sheet: str, xml: str) -> List[str]:
    lines = []
    for sqref, body in re.findall(r'<conditionalFormatting\b[^>]*sqref="([^"]*)"[^>]*>(.*?)</conditionalFormatting>',
                                  xml, flags=re.S):
        for attrs, rule in re.findall(r"<cfRule\b([^>]*?)(?:/>|>(.*?)</cfRule>)", body, flags=re.S):
            kind = re.search(r'type="([^"]*)"', attrs)
            op = re.search(r'operator="([^"]*)"', attrs)
            formulas = [_show_formula(html.unescape(f)) for f in re.findall(r"<formula>(.*?)</formula>", rule or "",
                                                                            flags=re.S)]
            head = " ".join(x for x in (kind.group(1) if kind else "", op.group(1) if op else "") if x)
            lines.append(f"{sheet}!{_sqref(sqref)} format {head}" + (": " + " | ".join(formulas) if formulas else ""))
    return sorted(lines)


def _rels(contents: Dict[str, bytes], part: str) -> Dict[str, str]:
    """部品の rels → {rId: 部品名}。"""
    base, name = posixpath.split(part)
    data = contents.get(posixpath.join(base, "_rels", name + ".rels"))
    if data is None:
        return {}
    out = {}
    for rel in re.findall(r"<Relationship [^>]*>", data.decode("utf-8")):
        if 'TargetMode="External"' in rel:
            continue
        rid = re.search(r'Id="([^"]+)"', rel).group(1)
        target = re.search(r'Target="([^"]+)"', rel).group(1)
        out[rid] = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(base, target))
    return out


def _charts(sheet: str, sheet_part: str, contents: Dict[str, bytes]) -> List[str]:
    lines = []
    for drawing in _rels(contents, sheet_part).values():
        if not re.fullmatch(r"xl/drawings/drawing\d+\.xml", drawing) or drawing not in contents:
            continue
        rels = _rels(contents, drawing)
        drawing_xml = contents[drawing].decode("utf-8")
        for rid in re.findall(r'<c:chart\b[^>]*r:id="([^"]+)"', drawing_xml):
            chart = rels.get(rid)
            if chart not in contents:
                continue
            xml = contents[chart].decode("utf-8")
            title = re.search(r"<c:title>(.*?)</c:title>", xml, flags=re.S)
            title_text = "".join(re.findall(r"<a:t>(.*?)</a:t>", title.group(1), flags=re.S)) if title else ""
            refs = [html.unescape(f) for f in re.findall(r"<c:f>(.*?)</c:f>", xml, flags=re.S)]
            lines.append(f'{sheet} chart "{_one_line(html.unescape(title_text))}": ' + " | ".join(refs))
    return lines


def book_text(parts: Parts) -> str:
    """ブックの部品の並び → 構造のテキスト(末尾は改行。同じブックからは必ず同じテキストになる)。"""
    contents = dict(parts)
    workbook = contents["xl/workbook.xml"].decode("utf-8")
    sheet_rels = _rels(contents, "xl/workbook.xml")
    sheets = [(html.unescape(name), attrs, sheet_rels.get(rid))
              for name, attrs, rid in re.findall(r'<sheet name="([^"]+)"([^>]*?)r:id="([^"]+)"', workbook)]
    sheet_names = [name for name, _, _ in sheets]
    strings = _shared_strings(contents["xl/sharedStrings.xml"].decode("utf-8")
                              if "xl/sharedStrings.xml" in contents else None)

    lines = ["# シートの並び"]
    for name, attrs, _ in sheets:
        state = re.search(r'state="([^"]*)"', attrs)
        lines.append(f"sheet {name}" + (f" ({state.group(1)})" if state else ""))

    lines.append("# 定義名")
    names = []
    for attrs, ref in re.findall(r"<definedName\b([^>]*)>(.*?)</definedName>", workbook, flags=re.S):
        name = html.unescape(re.search(r'name="([^"]*)"', attrs).group(1))
        if name.startswith(("_xlpm.", "_xlfn.")):  # LAMBDAの引数・新しい関数の名前(Excelが自動で作る)
            continue
        scope = re.search(r'localSheetId="(\d+)"', attrs)
        scope_text = f"[{sheet_names[int(scope.group(1))]}] " if scope else ""
        hidden = " (hidden)" if 'hidden="1"' in attrs else ""
        names.append(f"name {scope_text}{name}{hidden} = {_show_formula(html.unescape(ref))}")
    lines.extend(sorted(names))

    for name, _, part in sheets:
        lines.append(f"# {name}")
        if part not in contents:
            continue
        xml = contents[part].decode("utf-8")
        lines.extend(_cells(name, xml, strings))
        lines.extend(_validations(name, xml))
        lines.extend(_conditional_formats(name, xml))
        lines.extend(_charts(name, part, contents))
    return "\n".join(lines) + "\n"
