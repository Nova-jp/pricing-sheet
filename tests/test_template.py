"""tools/template.py(LSEGのデータを含まない雛形の作成・検査)のテスト。

実物のブックは使わず、最小限の部品で組み立てたブックで、規則ごとに確かめる。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools.template import array_udf_names, check_template, make_template  # noqa: E402

WORKBOOK = (
    '<workbook xmlns:mc="m" xmlns:x15ac="a"><fileVersion/>'
    '<mc:AlternateContent xmlns:mc="m"><mc:Choice Requires="x15">'
    '<x15ac:absPath url="C:\\Users\\someone\\book\\" xmlns:x15ac="a"/></mc:Choice></mc:AlternateContent>'
    '<sheets><sheet name="Calc" sheetId="1" r:id="rId1"/><sheet name="Raw" sheetId="2" r:id="rId2"/></sheets>'
    '<calcPr calcId="191029" calcMode="manual"/></workbook>'
)
WORKBOOK_RELS = (
    '<Relationships>'
    '<Relationship Id="rId1" Type="ws" Target="worksheets/sheet1.xml"/>'
    '<Relationship Id="rId2" Type="ws" Target="worksheets/sheet2.xml"/>'
    '<Relationship Id="rId3" Type="vd" Target="volatileDependencies.xml"/>'
    '</Relationships>'
)
CONTENT_TYPES = (
    '<Types><Override PartName="/xl/workbook.xml" ContentType="wb"/>'
    '<Override PartName="/xl/volatileDependencies.xml" ContentType="vd"/></Types>'
)
CALC_SHEET = (
    '<worksheet><sheetData>'
    '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1"><v>42</v></c></row>'
    '<row r="2"><c r="A2" t="str"><f>RtGet("IDN","X","BID")</f><v>1.234</v></c>'
    '<c r="B2" t="str" cm="1"><f t="array" ref="B2:C3">HistoricalRates(A1)</f><v>Date</v></c>'
    '<c r="C2" s="5"><v>46290</v></c></row>'
    '<row r="3"><c r="B3"><v>46291</v></c><c r="C3" t="s"><v>1</v></c>'
    '<c r="D3"><f t="shared" ref="D3:D4" si="0">A1+1</f><v>2</v></c></row>'
    '<row r="4"><c r="D4"><f t="shared" si="0"/><v>3</v></c><c r="E4" t="e"><f>FairRate(A1)</f><v>#VALUE!</v></c></row>'
    '</sheetData></worksheet>'
)
RAW_SHEET = (
    '<worksheet><sheetData>'
    '<row r="1"><c r="A1" t="str"><f>RDP.HistoricalPricing(A1)</f><v>更新</v></c></row>'
    '<row r="2"><c r="A2" t="s"><v>2</v></c></row>'
    '<row r="3"><c r="A3" t="s"><v>3</v></c><c r="B3" s="1"><v>1.5</v></c></row>'
    '<row r="4"><c r="A4"><v>46290</v></c></row>'
    '</sheetData></worksheet>'
)
SHARED_STRINGS = '<sst count="4" uniqueCount="4"><si><t>入力</t></si><si><t>SPILL</t></si><si><t>説明</t></si><si><t>JP1WONI=TRDT</t></si></sst>'
CHART = '<c:chart><c:val><c:numRef><c:f>Calc!$B$3:$B$9</c:f><c:numCache><c:pt idx="0"><c:v>0.5</c:v></c:pt></c:numCache></c:numRef></c:val></c:chart>'
EXTERNAL_LINK = (
    '<externalLink><externalBook r:id="rId1"><xxl21:alternateUrls><xxl21:absoluteUrl r:id="rId2"/></xxl21:alternateUrls>'
    '<sheetDataSet><sheetData sheetId="0"/><sheetData sheetId="1"><row r="5"><cell r="A5"><v>46290</v></cell></row></sheetData>'
    '</sheetDataSet></externalBook></externalLink>'
)
EXTERNAL_LINK_RELS = (
    '<Relationships><Relationship Id="rId2" Type="p" Target="file:///C:\\Users\\someone\\h.xlsx" TargetMode="External"/>'
    '<Relationship Id="rId1" Type="p" Target="h.xlsx" TargetMode="External"/></Relationships>'
)
CORE = '<cp:coreProperties><dc:creator>山田 太郎</dc:creator><cp:lastModifiedBy>山田 太郎</cp:lastModifiedBy></cp:coreProperties>'

AREAS = {"Raw": 3}


def _book(**override):
    parts = {
        "[Content_Types].xml": CONTENT_TYPES,
        "xl/workbook.xml": WORKBOOK,
        "xl/_rels/workbook.xml.rels": WORKBOOK_RELS,
        "xl/worksheets/sheet1.xml": CALC_SHEET,
        "xl/worksheets/sheet2.xml": RAW_SHEET,
        "xl/sharedStrings.xml": SHARED_STRINGS,
        "xl/charts/chart1.xml": CHART,
        "xl/externalLinks/externalLink1.xml": EXTERNAL_LINK,
        "xl/externalLinks/_rels/externalLink1.xml.rels": EXTERNAL_LINK_RELS,
        "xl/volatileDependencies.xml": '<volTypes><tp><v>1.75</v></tp></volTypes>',
        "docProps/core.xml": CORE,
        "xl/vbaProject.bin": "c:\\users\\someone\\xlwings.xlam",
    }
    parts.update(override)
    return [(k, v.encode("utf-8")) for k, v in parts.items()]


def _text(parts, name):
    return dict(parts)[name].decode("utf-8")


class MakeTemplateTest(unittest.TestCase):
    def setUp(self):
        self.out = make_template(_book(), AREAS)

    def test_formula_results_are_removed_and_formulas_kept(self):
        calc = _text(self.out, "xl/worksheets/sheet1.xml")
        self.assertIn('<c r="A2"><f>RtGet("IDN","X","BID")</f></c>', calc)
        self.assertIn('<c r="E4"><f>FairRate(A1)</f></c>', calc)  # エラー表示も消える
        self.assertIn('<c r="D4"><f t="shared" si="0"/></c>', calc)
        self.assertNotIn("1.234", calc)
        self.assertNotIn("#VALUE!", calc)

    def test_dynamic_array_anchor_keeps_cm_and_spill_cells_are_cleared(self):
        calc = _text(self.out, "xl/worksheets/sheet1.xml")
        self.assertIn('<c r="B2" cm="1"><f t="array" ref="B2:C3">HistoricalRates(A1)</f></c>', calc)
        self.assertIn('<c r="C2" s="5"/>', calc)  # 書式は残す
        self.assertNotIn('r="B3"', calc)
        self.assertNotIn('r="C3"', calc)
        self.assertNotIn("46290", calc)

    def test_shared_formula_range_is_not_treated_as_spill(self):
        calc = _text(self.out, "xl/worksheets/sheet1.xml")
        self.assertIn('<c r="D3"><f t="shared" ref="D3:D4" si="0">A1+1</f></c>', calc)

    def test_constant_inputs_are_kept(self):
        calc = _text(self.out, "xl/worksheets/sheet1.xml")
        self.assertIn('<c r="A1" t="s"><v>0</v></c>', calc)
        self.assertIn('<c r="B1"><v>42</v></c>', calc)

    def test_receive_area_is_cleared_from_declared_row(self):
        raw = _text(self.out, "xl/worksheets/sheet2.xml")
        self.assertIn('<c r="A2" t="s"><v>2</v></c>', raw)  # 受信領域より上の説明は残す
        self.assertIn('<c r="B3" s="1"/>', raw)
        self.assertNotIn('r="A3"', raw)
        self.assertNotIn('r="A4"', raw)

    def test_unreferenced_shared_strings_are_blanked(self):
        sst = _text(self.out, "xl/sharedStrings.xml")
        self.assertEqual(sst.count("<si>"), 4)  # 番号がずれないよう要素の数は保つ
        self.assertIn("入力", sst)
        self.assertIn("説明", sst)
        self.assertNotIn("SPILL", sst)
        self.assertNotIn("JP1WONI", sst)

    def test_caches_paths_and_names_are_removed(self):
        self.assertNotIn("numCache", _text(self.out, "xl/charts/chart1.xml"))
        link = _text(self.out, "xl/externalLinks/externalLink1.xml")
        self.assertIn('<sheetData sheetId="1"/>', link)
        self.assertNotIn("alternateUrls", link)
        self.assertNotIn("file:///", _text(self.out, "xl/externalLinks/_rels/externalLink1.xml.rels"))
        workbook = _text(self.out, "xl/workbook.xml")
        self.assertNotIn("absPath", workbook)
        self.assertIn('<calcPr fullCalcOnLoad="1" calcId="191029" calcMode="manual"/>', workbook)
        self.assertNotIn("山田", _text(self.out, "docProps/core.xml"))

    def test_rtd_cache_part_and_its_references_are_removed(self):
        names = [n for n, _ in self.out]
        self.assertNotIn("xl/volatileDependencies.xml", names)
        self.assertNotIn("volatileDependencies", _text(self.out, "[Content_Types].xml"))
        self.assertNotIn("volatileDependencies", _text(self.out, "xl/_rels/workbook.xml.rels"))
        self.assertEqual(names[0], "[Content_Types].xml")  # 部品の順序を保つ

    def test_making_twice_changes_nothing(self):
        self.assertEqual(make_template(self.out, AREAS), self.out)

    def test_unknown_receive_sheet_is_an_error(self):
        with self.assertRaises(ValueError):
            make_template(_book(), {"NoSuchSheet": 3})


class CheckTemplateTest(unittest.TestCase):
    UDFS = {"HistoricalRates"}

    def test_template_passes(self):
        self.assertEqual(check_template(make_template(_book(), AREAS), AREAS, self.UDFS), [])

    def test_working_book_with_data_fails(self):
        problems = check_template(_book(), AREAS, self.UDFS)
        self.assertTrue(any("データが残っています" in p for p in problems))
        self.assertTrue(any("ローカルのパス" in p for p in problems))

    def test_leftover_part_is_reported_by_name(self):
        parts = make_template(_book(), AREAS) + [("xl/volatileDependencies.xml", b"<volTypes/>")]
        problems = check_template(parts, AREAS, self.UDFS)
        self.assertEqual(problems, ["削除すべき部品が残っています(python -m tools.template で作り直す): "
                                    "xl/volatileDependencies.xml"])

    def test_unknown_part_fails(self):
        parts = make_template(_book(), AREAS) + [("xl/pivotCache/pivotCacheRecords1.xml", b"<x/>")]
        self.assertTrue(any("知らない部品" in p for p in check_template(parts, AREAS, self.UDFS)))

    def test_legacy_array_udf_cell_fails(self):
        legacy = CALC_SHEET.replace(
            '<c r="B2" t="str" cm="1"><f t="array" ref="B2:C3">', '<c r="B2" t="str"><f>').replace(
            '<c r="C2" s="5"><v>46290</v></c>', "").replace('<c r="B3"><v>46291</v></c><c r="C3" t="s"><v>1</v></c>', "")
        parts = make_template(_book(**{"xl/worksheets/sheet1.xml": legacy}), AREAS)
        problems = check_template(parts, AREAS, self.UDFS)
        self.assertTrue(any("動的配列ではありません" in p and "B2" in p for p in problems))


class ArrayUdfNamesTest(unittest.TestCase):
    def test_reads_array_returning_udfs_from_udf_modules(self):
        names = array_udf_names()
        self.assertTrue({"HistoricalRates", "RiskGrid", "BojImpliedRates", "PcaModel"} <= names)
        self.assertNotIn("FairRate", names)
        self.assertNotIn("MarketInfo", names)


if __name__ == "__main__":
    unittest.main()
