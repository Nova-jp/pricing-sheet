"""生シートの見本(tools/raw_sample.py)のCSVとセルの値の変換、置いてある見本の読み込み(Excelは不要)。"""

import datetime as dt
import unittest

from tools import raw_sample as rs


class CellTextTest(unittest.TestCase):
    def test_cell_to_text(self):
        self.assertEqual(rs.cell_to_text(None), "")
        self.assertEqual(rs.cell_to_text(500.0), "500")
        self.assertEqual(rs.cell_to_text(-0.00095), "-0.00095")
        self.assertEqual(rs.cell_to_text(1.22875), "1.22875")
        self.assertEqual(rs.cell_to_text("CURVE.JPY.OIS"), "CURVE.JPY.OIS")
        self.assertEqual(rs.cell_to_text(dt.datetime(2026, 11, 27, tzinfo=dt.timezone.utc)), "2026-11-27")
        self.assertEqual(rs.cell_to_text(dt.datetime(2026, 11, 27, 15, 0)), "2026-11-27T15:00:00")
        with self.assertRaises(rs.SampleError):
            rs.cell_to_text("=SUM(A1)")
        with self.assertRaises(rs.SampleError):
            rs.cell_to_text(True)

    def test_text_to_cell(self):
        self.assertEqual(rs.text_to_cell(""), (None, False))
        self.assertEqual(rs.text_to_cell("500"), (500, False))
        self.assertEqual(rs.text_to_cell("-0.00095"), (-0.00095, False))
        self.assertEqual(rs.text_to_cell("1e12"), (1e12, False))
        self.assertEqual(rs.text_to_cell("10Y"), ("10Y", False))
        self.assertEqual(rs.text_to_cell("0-02-0023-00068"), ("0-02-0023-00068", False))
        self.assertEqual(rs.text_to_cell("2026-11-27"), (46353.0, True))  # Excelの日付のシリアル値
        self.assertEqual(rs.text_to_cell("2026-11-27T12:00:00"), (46353.5, True))
        with self.assertRaises(rs.SampleError):
            rs.text_to_cell("=1+1")

    def test_round_trip(self):
        grid = (
            ("ID", "ExerciseDate", "Strike", None),
            ("TEST-001", dt.datetime(2026, 11, 27), 102.5, None),
            (None, None, None, None),  # 末尾の空行は持たない
        )
        text = rs.grid_to_csv(grid)
        self.assertEqual(text, "ID,ExerciseDate,Strike\nTEST-001,2026-11-27,102.5\n")
        values, dates = rs.csv_to_grid(text)
        self.assertEqual(values, [["ID", "ExerciseDate", "Strike"], ["TEST-001", 46353.0, 102.5]])
        self.assertEqual(dates, [(1, 1)])


class SampleFolderCheckTest(unittest.TestCase):
    def test_only_csv_and_readme(self):
        import tempfile
        from pathlib import Path

        from tools.template import raw_sample_odd_files

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "PositionSheet").mkdir()
            (root / "README.md").write_text("x")
            (root / "PositionSheet" / "Raw_IR.csv").write_text("a\n")
            self.assertEqual(raw_sample_odd_files(root), [])
            (root / "PositionSheet" / "Raw_IR.xlsx").write_bytes(b"")
            self.assertEqual(raw_sample_odd_files(root), ["PositionSheet/Raw_IR.xlsx"])
        self.assertEqual(raw_sample_odd_files(Path(d) / "missing"), [])


class SampleFilesTest(unittest.TestCase):
    def test_samples_parse(self):
        """置いてある見本が全シート揃っていて読めること(1行目は見出し)。"""
        for book, sheets in rs.RAW_SHEETS.items():
            for sheet in sheets:
                path = rs.sample_path(book, sheet)
                with self.subTest(path=path.name):
                    self.assertTrue(path.exists(), path)
                    values, _ = rs.csv_to_grid(path.read_text(encoding="utf-8"))
                    self.assertGreaterEqual(len(values), 2)
                    self.assertTrue(all(isinstance(v, str) for v in values[0] if v is not None))


if __name__ == "__main__":
    unittest.main()
