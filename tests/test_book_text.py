"""tools/book_text.py(雛形の構造のテキスト)のテスト。最小限の部品で組み立てたブックで確かめる。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools.book_text import book_text, relative_key, shift_formula  # noqa: E402

WORKBOOK = (
    '<workbook><sheets><sheet name="Calc" sheetId="1" r:id="rId1"/>'
    '<sheet name="Hide" sheetId="2" state="hidden" r:id="rId2"/></sheets>'
    '<definedNames><definedName name="_xlpm.x" hidden="1">#NAME?</definedName>'
    '<definedName name="Broker">Calc!$F$1</definedName>'
    '<definedName name="Local" localSheetId="1">Hide!$A$1</definedName></definedNames></workbook>'
)
WORKBOOK_RELS = (
    '<Relationships><Relationship Id="rId1" Type="ws" Target="worksheets/sheet1.xml"/>'
    '<Relationship Id="rId2" Type="ws" Target="/xl/worksheets/sheet2.xml"/></Relationships>'
)
CALC = (
    '<worksheet><sheetData>'
    '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1"><v>1.5</v></c><c r="C1" t="b"><v>1</v></c>'
    '<c r="F1" t="inlineStr"><is><t>Tradition</t></is></c></row>'
    # 共有数式: A2 が先頭、A3:A4 は空の <f t="shared"/>。B2:B4 は同じ形を1つずつ書いたもの
    '<row r="2"><c r="A2"><f t="shared" ref="A2:A4" si="0">$B$1+B2*2</f><v>1</v></c><c r="B2"><f>C2&amp;"A1"</f></c></row>'
    '<row r="3"><c r="A3"><f t="shared" si="0"/><v>1</v></c><c r="B3"><f>C3&amp;"A1"</f></c></row>'
    '<row r="4"><c r="A4"><f t="shared" si="0"/><v>1</v></c><c r="B4"><f>C4&amp;"A1"</f></c></row>'
    # 動的配列(cm あり)と、揮発性の関数のスピル先の空の数式、旧式の配列数式
    '<row r="6"><c r="A6" cm="1"><f t="array" ref="A6:B7" ca="1">_xlfn.LET(_xlpm.q,LiveQuotes(Broker),_xlpm.q)</f></c>'
    '<c r="B6"><f ca="1"/></c><c r="D6"><f t="array" ref="D6">RiskGrid(A1)</f></c></row>'
    '</sheetData>'
    '<dataValidations count="1"><dataValidation type="list" sqref="F1"><formula1>"Tradition,Totan"</formula1>'
    '</dataValidation></dataValidations>'
    '<conditionalFormatting sqref="B2:B4"><cfRule type="cellIs" operator="lessThan" priority="1">'
    '<formula>0</formula></cfRule></conditionalFormatting>'
    '</worksheet>'
)
HIDE = '<worksheet><sheetData><row r="1"><c r="A1"><v>3</v></c></row></sheetData></worksheet>'
SHEET1_RELS = '<Relationships><Relationship Id="rId1" Type="d" Target="../drawings/drawing1.xml"/></Relationships>'
DRAWING = '<xdr:wsDr><c:chart xmlns:c="c" r:id="rId1"/></xdr:wsDr>'
DRAWING_RELS = '<Relationships><Relationship Id="rId1" Type="c" Target="../charts/chart1.xml"/></Relationships>'
CHART = '<c:chart><c:title><a:t>Spread</a:t></c:title><c:f>Calc!$B$2:$B$4</c:f><c:f>[0]!Dates</c:f></c:chart>'
SHARED_STRINGS = '<sst><si><t>Rates</t><rPh><t>ふりがな</t></rPh></si></sst>'


def book():
    return [(name, xml.encode("utf-8")) for name, xml in (
        ("xl/workbook.xml", WORKBOOK), ("xl/_rels/workbook.xml.rels", WORKBOOK_RELS),
        ("xl/worksheets/sheet1.xml", CALC), ("xl/worksheets/sheet2.xml", HIDE),
        ("xl/worksheets/_rels/sheet1.xml.rels", SHEET1_RELS), ("xl/drawings/drawing1.xml", DRAWING),
        ("xl/drawings/_rels/drawing1.xml.rels", DRAWING_RELS), ("xl/charts/chart1.xml", CHART),
        ("xl/sharedStrings.xml", SHARED_STRINGS),
    )]


class FormulaTest(unittest.TestCase):
    def test_copied_formulas_have_the_same_relative_key(self):
        self.assertEqual(relative_key("$B$1+B2*SUM(C:D)", 2, 1), relative_key("$B$1+C3*SUM(D:E)", 3, 2))
        self.assertNotEqual(relative_key("B2", 2, 1), relative_key("B2", 3, 1))

    def test_shift_moves_only_relative_parts(self):
        self.assertEqual(shift_formula("$B$1+B2+B$2+$B2+Calc!C3+SUM(2:3)", 2, 1), "$B$1+C4+C$2+$B4+Calc!D5+SUM(4:5)")

    def test_strings_names_and_functions_are_not_references(self):
        formula = 'LOG10(A1)&"B2"&\'Sheet 1\'!A1&Tenor1Y&_xlpm.T1&FX1!A1'
        self.assertEqual(shift_formula(formula, 1, 0), 'LOG10(A2)&"B2"&\'Sheet 1\'!A2&Tenor1Y&_xlpm.T1&FX1!A2')

    def test_shift_out_of_the_sheet_is_ref_error(self):
        self.assertEqual(shift_formula("A1", -1, 0), "#REF!")


class BookTextTest(unittest.TestCase):
    def setUp(self):
        self.lines = book_text(book()).splitlines()

    def test_sheets_and_defined_names(self):
        self.assertIn("sheet Hide (hidden)", self.lines)
        self.assertIn("name Broker = Calc!$F$1", self.lines)
        self.assertIn("name [Hide] Local = Hide!$A$1", self.lines)
        self.assertFalse(any("_xlpm" in line for line in self.lines))

    def test_constants(self):
        for line in ('Calc!A1 : "Rates"', "Calc!B1 : 1.5", "Calc!C1 : TRUE", 'Calc!F1 : "Tradition"', "Hide!A1 : 3"):
            self.assertIn(line, self.lines)

    def test_copied_formulas_are_one_line_and_shared_formulas_are_expanded(self):
        self.assertIn("Calc!A2:A4 = $B$1+B2*2", self.lines)
        self.assertIn('Calc!B2:B4 = C2&"A1"', self.lines)

    def test_array_kinds_and_spill_placeholders(self):
        self.assertIn("Calc!A6 = LET(q,LiveQuotes(Broker),q)", self.lines)
        self.assertIn("Calc!D6 {= RiskGrid(A1)}", self.lines)
        self.assertFalse(any(line.startswith("Calc!B6") for line in self.lines))

    def test_validation_format_and_chart(self):
        self.assertIn('Calc!F1 validation list: "Tradition,Totan"', self.lines)
        self.assertIn("Calc!B2:B4 format cellIs lessThan: 0", self.lines)
        self.assertIn('Calc chart "Spread": Calc!$B$2:$B$4 | [0]!Dates', self.lines)

    def test_removed_formula_shows_in_the_text(self):
        old = book()
        new = [(n, d.replace(b"LiveQuotes(Broker)", b"LiveQuotes(&quot;swap&quot;)")) for n, d in old]
        self.assertIn('Calc!A6 = LET(q,LiveQuotes("swap"),q)', book_text(new).splitlines())


if __name__ == "__main__":
    unittest.main()
