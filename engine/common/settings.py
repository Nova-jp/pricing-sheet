"""
全体の設定ファイル(リポジトリ直下の settings.toml)を読む。

めったに変えない、どのPCでも同じ値だけを置く(ユーザー指示)。環境ごとの値・秘密は OPERATIONS.md。

■ [history]: ヒストリカルを取り始める日
  start が全データ共通の開始日。データごと(HISTORY_DATASETS)に別の日にしたいときだけ、その名前で書く。
  知らない名前・日付でない値はエラーにする(書き間違いを黙って既定値にしない)。
"""

import tomllib
from datetime import date
from pathlib import Path
from typing import Dict, NamedTuple

from engine.common.paths import REPO_ROOT

DEFAULT_SETTINGS = REPO_ROOT / "settings.toml"

# 開始日を個別に書けるデータ(名前 → 説明)
HISTORY_DATASETS: Dict[str, str] = {
    "jgb": "JGBの引け値",
    "futures": "長期国債先物の清算値",
    "swaption": "スワップションのプレミアム",
}


class HistorySettings(NamedTuple):
    start: date  # 全データ共通の開始日
    overrides: Dict[str, date]  # データごとの開始日(書いたものだけ)

    def start_of(self, dataset: str) -> date:
        if dataset not in HISTORY_DATASETS:
            raise KeyError(f"開始日の設定に無いデータです: {dataset!r}")
        return self.overrides.get(dataset, self.start)


class Settings(NamedTuple):
    history: HistorySettings


def _as_date(path: Path, key: str, value) -> date:
    # tomllib は日付を date で返す(日時 2022-01-12T00:00 は datetime になるので受け付けない)
    if type(value) is not date:
        raise ValueError(f"{path}: [history] {key} は日付で書いてください(例 2022-01-12): {value!r}")
    return value


def load_settings(path: Path = DEFAULT_SETTINGS) -> Settings:
    path = Path(path)
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    history = raw.get("history")
    if not isinstance(history, dict) or "start" not in history:
        raise ValueError(f"{path}: [history] の start がありません")
    unknown = sorted(set(history) - {"start"} - set(HISTORY_DATASETS))
    if unknown:
        raise ValueError(f"{path}: [history] に知らない項目があります: {', '.join(unknown)}")
    start = _as_date(path, "start", history["start"])
    overrides = {k: _as_date(path, k, v) for k, v in history.items() if k != "start"}
    return Settings(HistorySettings(start, overrides))
