"""
リポジトリ内の各種パスを解決するモジュール。

このリポジトリはGitHub経由で会社PC等にクローンされる想定のため、
クローン先の絶対パスに依存させず、常にこのファイルの場所を起点に解決する。
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
HISTORICAL_DB_PATH = DATA_DIR / "historical.db"
