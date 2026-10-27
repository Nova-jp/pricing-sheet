"""tools/build_vol_level.py(VolLevelシートの構築スクリプト)の中身(layout)のテスト。Excelは使わない。

数式の計算結果はExcelでしか確かめられないので、ここでは書き間違い(括弧・引用符の対応、未定義の名前、
セルの重複、配列UDFの置き場所)だけを見る。
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools import build_vol_level as b  # noqa: E402
from tools.template import array_udf_names  # noqa: E402


def _formulas():
    cells, names = b.layout()
    return [(c, v) for c, v, _ in cells if isinstance(v, str) and v.startswith("=")], names


def _balanced(formula: str) -> bool:
    depth, in_str = 0, False
    for ch in formula:
        if ch == '"':
            in_str = not in_str
        elif not in_str:
            depth += {"(": 1, ")": -1}.get(ch, 0)
            if depth < 0:
                return False
    return depth == 0 and not in_str


class LayoutTest(unittest.TestCase):
    def test_parentheses_and_quotes_match(self):
        formulas, names = _formulas()
        for where, f in formulas + list(names.items()):
            self.assertTrue(_balanced(f), f"{where}: {f}")

    def test_no_duplicate_cells(self):
        cells, _ = b.layout()
        addresses = [c for c, _, _ in cells]
        self.assertEqual(len(addresses), len(set(addresses)))

    def test_vlv_names_are_defined_before_use(self):
        formulas, names = _formulas()
        order = list(names)
        for name, refers_to in names.items():
            for used in re.findall(r"\bVLV_\w+", refers_to):
                self.assertIn(used, order[:order.index(name)], f"{name} が {used} より先に定義されている")
        for cell, f in formulas:
            for used in re.findall(r"\bVLV_\w+", f):
                self.assertIn(used, names, f"{cell}: {used}")

    def test_no_volatile_today(self):
        formulas, names = _formulas()
        for where, f in formulas + list(names.items()):
            self.assertNotRegex(f, r"\b(TODAY|NOW)\(", where)

    def test_array_udfs_are_where_docs_say(self):
        formulas, _ = _formulas()
        udfs = array_udf_names()
        where = {c: re.findall(r"\b(%s)\(" % "|".join(udfs), f) for c, f in formulas}
        self.assertEqual({c: u for c, u in where.items() if u}, {
            "AA3": ["LiveSnapshot", "LiveQuotes"], "AE3": ["LiveSnapshot", "LiveQuotes"],
            "AS3": ["VolHistory"], "BD4": ["VolFwdLive"], "CL4": ["VolFwdLive"], "CN3": ["VolHistory"],
        })

    def test_curve_tenors_fill_the_curve_input_range(self):
        cells = {c: v for c, v, _ in b.layout()[0]}
        self.assertEqual(len(b.CURVE_TENORS), 44)  # AP4:AP47(AJ3 の数式の範囲)
        self.assertEqual(cells["AP47"], "M9")
        self.assertIn("$AP$4:$AP$47", cells["AJ3"])

    def test_grid_axes_fit_the_map(self):
        self.assertLessEqual(len(b.GRID_EXPIRIES), 8)
        self.assertLessEqual(len(b.GRID_TAILS), 8)


if __name__ == "__main__":
    unittest.main()
