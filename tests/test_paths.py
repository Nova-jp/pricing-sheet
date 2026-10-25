"""
パスの解決(engine/common/paths.py)のテスト。git worktreeの中からメインの作業ツリーを見つけること。

実行方法:
    python -m unittest tests.test_paths -v
"""

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from engine.common.paths import main_worktree_root  # noqa: E402


class MainWorktreeRootTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name).resolve()
        self.main = tmp / "pricing-sheet"
        self.wt = tmp / "pricing-sheet-feature"
        gitdir = self.main / ".git" / "worktrees" / "pricing-sheet-feature"
        gitdir.mkdir(parents=True)
        (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
        self.wt.mkdir()
        self.gitdir = gitdir

    def test_main_tree_is_itself(self):
        self.assertEqual(main_worktree_root(self.main), self.main)

    def test_worktree_points_to_main_tree(self):
        (self.wt / ".git").write_text(f"gitdir: {self.gitdir.as_posix()}\n", encoding="utf-8")
        self.assertEqual(main_worktree_root(self.wt), self.main)

    def test_relative_gitdir(self):
        (self.wt / ".git").write_text("gitdir: ../pricing-sheet/.git/worktrees/pricing-sheet-feature\n", encoding="utf-8")
        self.assertEqual(main_worktree_root(self.wt), self.main)

    def test_unreadable_git_file_falls_back_to_itself(self):
        (self.wt / ".git").write_text("something else\n", encoding="utf-8")
        self.assertEqual(main_worktree_root(self.wt), self.wt)
        (self.wt / ".git").write_text("gitdir: ../missing/.git/worktrees/x\n", encoding="utf-8")
        self.assertEqual(main_worktree_root(self.wt), self.wt)

    def test_no_git_is_itself(self):
        self.assertEqual(main_worktree_root(self.wt), self.wt)


if __name__ == "__main__":
    unittest.main()
