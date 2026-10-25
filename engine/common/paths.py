"""
リポジトリ内の各種パスを解決するモジュール。

このリポジトリはGitHub経由で会社PC等にクローンされる想定のため、
クローン先の絶対パスに依存させず、常にこのファイルの場所を起点に解決する。

■ リアルタイムの受け渡し(live.db)はメインの作業ツリーのものを使う
  フィード(python -m engine.common.live_feed)はメインの作業ツリー(最初にクローンしたフォルダ)で
  1本だけ動かす。git worktreeで作った別のフォルダのコード・ブックも、メインの作業ツリーの
  data/live.db を読む(worktreeごとにフィードを立てるとLSEGの購読が増えるため。docs/decisions.md)。
  ヒストリカルDB(historical.db)は今までどおり自分のフォルダのもの(worktreeではコピーを使い、
  本物を書き換えない)。
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def main_worktree_root(repo_root: Path = REPO_ROOT) -> Path:
    """git worktreeの中ならメインの作業ツリーのフォルダ、それ以外は repo_root。

    worktreeでは .git がファイルで、中身は「gitdir: <メインの.git>/worktrees/<名前>」。その下の
    commondir(メインの.gitへの相対パス)をたどり、メインの.gitの親フォルダを返す。
    読めない形のときは repo_root のまま(自分のフォルダの live.db を使う)。
    """
    git = repo_root / ".git"
    if not git.is_file():
        return repo_root
    try:
        text = git.read_text(encoding="utf-8").strip()
        if not text.startswith("gitdir:"):
            return repo_root
        gitdir = Path(text[len("gitdir:"):].strip())
        if not gitdir.is_absolute():
            gitdir = repo_root / gitdir
        commondir = gitdir / "commondir"
        if commondir.is_file():
            common = gitdir / commondir.read_text(encoding="utf-8").strip()
        else:
            common = gitdir.parent.parent
        main_root = common.resolve().parent
    except OSError:
        return repo_root
    return main_root if (main_root / ".git").is_dir() else repo_root


MAIN_ROOT = main_worktree_root()
DATA_DIR = REPO_ROOT / "data"
HISTORICAL_DB_PATH = DATA_DIR / "historical.db"
# フィード(engine/common/live_feed.py)→手動計算のブックへのリアルタイムの受け渡し(メインの作業ツリーのもの)
LIVE_DATA_DIR = MAIN_ROOT / "data"
LIVE_DB_PATH = LIVE_DATA_DIR / "live.db"
