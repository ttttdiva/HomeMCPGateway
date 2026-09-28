import subprocess
import tempfile
import unittest
from pathlib import Path

from home_mcp_gateway import git_qa as git


class GitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "repo with spaces"
        self.repo.mkdir()
        self.run_git("init", "-b", "main")
        for key, value in (("user.name", "QA"), ("user.email", "qa@example.test"),
                           ("core.autocrlf", "false"), ("commit.gpgsign", "false")):
            self.run_git("config", key, value)
        (self.repo / "hello.txt").write_bytes(b"hello\n")
        self.run_git("add", ".")
        self.run_git("commit", "-m", "initial")
        self.sha = self.run_git("rev-parse", "HEAD").strip()
        self.workspace = self.root / "isolated" / "run-1"

    def run_git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], stderr=subprocess.STDOUT).decode()

    def test_dirty_checkout_isolation_patch_diff_and_cleanup(self):
        (self.repo / "hello.txt").write_bytes(b"original dirty\n")
        (self.repo / "untracked.txt").write_text("keep")
        before = self.run_git("status", "--porcelain")
        created = git.git_worktree_create(str(self.repo), self.sha, str(self.workspace))
        self.assertEqual(created["repository"]["head"], self.sha)
        self.assertIsNone(created["repository"]["branch"])
        patch = "diff --git a/hello.txt b/hello.txt\n--- a/hello.txt\n+++ b/hello.txt\n@@ -1 +1 @@\n-hello\n+patched\n"
        git.git_apply_patch(str(self.workspace), patch, check_only=True)
        self.assertEqual((self.workspace / "hello.txt").read_text(), "hello\n")
        git.git_apply_patch(str(self.workspace), patch)
        self.assertIn("+patched", git.git_worktree_diff(str(self.workspace))["diff"])
        (self.workspace / "new file.txt").write_text("new")
        changes = git.git_changed_files(str(self.workspace))
        self.assertIn({"status": "M", "path": "hello.txt"}, changes["files"])
        self.assertIn({"status": "?", "path": "new file.txt"}, changes["files"])
        self.assertEqual(len(git.git_changed_files(str(self.workspace), ["hello.txt"])["files"]), 1)
        self.assertEqual(len(git.git_worktree_list(str(self.repo))["worktrees"]), 2)
        self.assertTrue(git.git_worktree_status(str(self.workspace))["dirty"])
        git.git_worktree_cleanup(str(self.repo), str(self.workspace), force=True)
        self.assertFalse(self.workspace.exists())
        self.assertEqual(self.run_git("status", "--porcelain"), before)
        self.assertEqual((self.repo / "hello.txt").read_bytes(), b"original dirty\n")

    def test_find_resolve_fetch_remote_head(self):
        subdir = self.repo / "subdir"
        subdir.mkdir()
        self.assertEqual(Path(git.git_resolve(str(subdir))["repository_root"]), self.repo)
        self.assertEqual(len(git.git_find(str(self.root))["repositories"]), 1)
        remote = self.root / "remote.git"
        subprocess.check_output(["git", "clone", "--bare", str(self.repo), str(remote)], stderr=subprocess.STDOUT)
        self.run_git("remote", "add", "origin", str(remote))
        self.assertEqual(git.git_remote_head(str(self.repo))["head"], self.sha)
        git.git_fetch(str(self.repo))
        self.run_git("branch", "--set-upstream-to", "origin/main")
        self.assertEqual(git.git_status(str(self.repo))["upstream"], "origin/main")
        self.assertTrue(git.git_resolve(str(remote))["bare"])

    def test_invalid_sha_and_patch_leave_files_untouched(self):
        with self.assertRaises(ValueError):
            git.git_worktree_create(str(self.repo), "HEAD", str(self.workspace))
        with self.assertRaises(ValueError):
            git.git_apply_patch(str(self.repo), "not a patch")
        self.assertFalse(git.git_status(str(self.repo))["dirty"])
