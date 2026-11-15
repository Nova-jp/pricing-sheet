"""全体の設定ファイル(settings.toml、engine/common/settings.py)の読み込み。"""

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.common.settings import DEFAULT_SETTINGS, HISTORY_DATASETS, load_settings  # noqa: E402


class SettingsTest(unittest.TestCase):
    def _load(self, text: str):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.toml"
            path.write_text(text, encoding="utf-8")
            return load_settings(path)

    def test_repository_settings_can_be_read(self):
        history = load_settings(DEFAULT_SETTINGS).history
        for name in HISTORY_DATASETS:
            self.assertIsInstance(history.start_of(name), date)

    def test_common_start_and_overrides(self):
        history = self._load("[history]\nstart = 2022-01-12\nfutures = 2023-04-03\n").history
        self.assertEqual(history.start_of("jgb"), date(2022, 1, 12))
        self.assertEqual(history.start_of("swaption"), date(2022, 1, 12))
        self.assertEqual(history.start_of("futures"), date(2023, 4, 3))
        with self.assertRaises(KeyError):
            history.start_of("swap")

    def test_mistakes_are_errors(self):
        for text, message in [
            ("[history]\n", "start がありません"),
            ("", "start がありません"),
            ("[history]\nstart = 2022-01-12\njgbs = 2022-01-12\n", "知らない項目があります: jgbs"),
            ('[history]\nstart = "2022-01-12"\n', "日付で書いてください"),
            ("[history]\nstart = 2022-01-12T00:00:00\n", "日付で書いてください"),
        ]:
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, message):
                self._load(text)


if __name__ == "__main__":
    unittest.main()
