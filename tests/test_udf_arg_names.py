"""UDFの引数名がVBAの予約語と衝突しないことのテスト。

xlwingsのImport Functionsは、引数名をそのまま使ってVBAのラッパー(xlwings_udfs)を書く。予約語(End等)が
あるとモジュールがコンパイルできず、「MacroOptions メソッドが失敗しました 1004」になって全UDFが登録されない
(2026-09のswap_pricingの end、2026-10-19のJapanHolidaysの end で2回起きた)。
"""

import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools.template import UDF_MODULES  # noqa: E402

# VBAの予約語(引数名に使えないもの)。大文字小文字は区別しない
VBA_RESERVED = {
    "and", "as", "boolean", "byref", "byte", "byval", "call", "case", "const", "currency", "date", "declare",
    "dim", "do", "double", "each", "else", "elseif", "empty", "end", "enum", "eqv", "erase", "error", "event",
    "exit", "false", "for", "friend", "function", "get", "global", "gosub", "goto", "if", "imp", "implements",
    "in", "integer", "is", "let", "lib", "like", "long", "loop", "lset", "me", "mod", "new", "next", "not",
    "nothing", "null", "object", "on", "option", "optional", "or", "paramarray", "preserve", "private",
    "property", "public", "raiseevent", "redim", "rem", "resume", "return", "rset", "select", "set", "single",
    "static", "step", "stop", "string", "sub", "then", "to", "true", "type", "typeof", "until", "variant",
    "wend", "while", "with", "xor",
}


def _udf_params():
    for path in UDF_MODULES:
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.FunctionDef) and any("func" in ast.unparse(d) for d in node.decorator_list):
                for arg in node.args.args:
                    yield path.name, node.name, arg.arg


class UdfArgNamesTest(unittest.TestCase):
    def test_no_vba_reserved_words(self):
        bad = [(f, n, a) for f, n, a in _udf_params() if a.lower() in VBA_RESERVED]
        self.assertEqual(bad, [])

    def test_reads_udfs(self):
        self.assertIn(("udf.py", "JapanHolidays", "start_date"), set(_udf_params()))


if __name__ == "__main__":
    unittest.main()
