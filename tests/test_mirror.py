import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from scripts import mirror_to_gitcode as mirror


class APITests(unittest.TestCase):
    def test_only_404_means_missing(self):
        api = mirror.API("https://example.test", "secret")
        for status in (401, 403, 418, 429):
            with self.subTest(status=status), patch.object(mirror.time, "sleep"), patch.object(
                mirror, "urlopen", side_effect=HTTPError("", status, "", {}, io.BytesIO())
            ) as request:
                with self.assertRaises(mirror.StopRun):
                    api.request("/repo", missing_ok=True)
                self.assertEqual(request.call_count, 1)
        with patch.object(mirror.time, "sleep"), patch.object(
            mirror, "urlopen", side_effect=HTTPError("", 404, "", {}, io.BytesIO())
        ):
            self.assertIsNone(api.request("/repo", missing_ok=True))

    def test_creation_is_not_retried(self):
        with patch.object(mirror.time, "sleep"), patch.object(
            mirror, "urlopen", side_effect=HTTPError("", 503, "", {}, io.BytesIO())
        ) as request:
            with self.assertRaises(RuntimeError):
                mirror.API("https://example.test", "secret").request("/repo", "POST", {})
            self.assertEqual(request.call_count, 1)

    def test_pagination(self):
        api = mirror.API("https://example.test", "secret")
        with patch.object(api, "request", side_effect=[[{}] * 100, [{"name": "last"}]]) as request:
            self.assertEqual(len(list(api.pages("/repos?type=owner"))), 101)
            self.assertIn("&per_page=100&page=2", request.call_args.args[0])

    def test_target_ownership_and_visibility(self):
        source = "https://github.com/alice/demo"
        target = {"homepage": source, "description": mirror.MARKER + source, "private": False}
        mirror.check_target(target, source, False)
        with self.assertRaises(RuntimeError):
            mirror.check_target(target, source, True)
        with self.assertRaises(RuntimeError):
            mirror.check_target({**target, "description": "unrelated"}, source, False)
        with self.assertRaises(RuntimeError):
            mirror.check_target({**target, "private": "false"}, source, False)

    def test_dry_run_includes_forks_without_writing(self):
        repos = [{"owner": {"login": "alice"}, "name": name, "private": False, "fork": fork}
                 for name, fork in (("original", False), ("forked", True))]
        calls = []

        def request(api, path, method="GET", data=None, missing_ok=False):
            calls.append((path, method))
            return {"login": "alice"} if path == "/user" else None

        with patch.dict(os.environ, {"GH_OWNER": "alice", "GC_NAMESPACE": "alice",
                                    "GC_TOKEN": "secret", "DRY_RUN": "true"}, clear=True), patch.object(
            mirror.API, "request", request
        ), patch.object(mirror.API, "pages", return_value=iter(repos)), patch.object(mirror, "git") as git:
            self.assertEqual(mirror.run(), 0)
            self.assertIn(("/repos/alice/forked", "GET"), calls)
            self.assertTrue(all(method == "GET" for _, method in calls))
            git.assert_not_called()

    def test_create_limit_and_private_visibility_gate(self):
        for private in (False, True):
            with self.subTest(private=private):
                repos = [{"owner": {"login": "alice"}, "name": name,
                          "private": private, "fork": False} for name in ("one", "two")]
                created = {}

                def request(api, path, method="GET", data=None, missing_ok=False):
                    if path == "/user":
                        return {"login": "alice"}
                    if method == "POST":
                        created[data["name"]] = data
                        return data
                    name = path.rsplit("/", 1)[-1]
                    if name not in created:
                        return None
                    # 模拟平台错误地把私有仓库创建为公开，必须阻止代码上传。
                    return {**created[name], "private": False}

                variables = {"GH_OWNER": "alice", "GC_NAMESPACE": "alice", "GC_TOKEN": "secret",
                             "GH_PAT": "secret", "DRY_RUN": "false", "MAX_NEW_REPOS": "1",
                             "INCLUDE_PRIVATE": str(private).lower()}
                with patch.dict(os.environ, variables, clear=True), patch.object(mirror.API, "request", request), patch.object(
                    mirror.API, "pages", return_value=iter(repos)
                ), patch.object(mirror, "git"), patch.object(mirror.time, "sleep"), patch.object(
                    mirror, "sync_repo", return_value="synced"
                ) as sync:
                    self.assertEqual(mirror.run(), 1 if private else 0)
                    self.assertEqual(list(created), ["one"])
                    self.assertEqual(created["one"]["public"], 0 if private else 1)
                    self.assertEqual(sync.call_count, 0 if private else 1)


class GitIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.target = self.root / "target.git"
        self.env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"}
        self.cmd("init", "-b", "main", str(self.source))
        self.cmd("init", "--bare", str(self.target))
        self.cmd("-C", str(self.source), "config", "user.email", "test@example.test")
        self.cmd("-C", str(self.source), "config", "user.name", "Test")
        (self.source / "README").write_text("first", encoding="utf-8")
        self.cmd("-C", str(self.source), "add", ".")
        self.cmd("-C", str(self.source), "commit", "-m", "first")
        self.cmd("-C", str(self.source), "branch", "feature")
        self.cmd("-C", str(self.source), "tag", "v1")

    def cmd(self, *args):
        return subprocess.run(["git", *args], check=True, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", env=self.env).stdout

    def sync(self):
        return mirror.sync_repo(self.source.as_uri(), self.target.as_uri(), self.env)

    def test_branches_tags_idempotency_and_no_deletion(self):
        self.sync()
        self.assertEqual(mirror.refs(str(self.source), self.env), mirror.refs(str(self.target), self.env))
        with patch.object(mirror, "git", wraps=mirror.git) as git:
            self.assertIn("跳过", self.sync())
            self.assertFalse(any(call.args[0][0] == "clone" for call in git.call_args_list))
        self.cmd("-C", str(self.source), "branch", "-D", "feature")
        self.sync()
        self.assertIn("refs/heads/feature", mirror.refs(str(self.target), self.env))

    def test_rewritten_history_is_rejected_atomically(self):
        self.sync()
        before = mirror.refs(str(self.target), self.env)
        self.cmd("-C", str(self.source), "commit", "--amend", "-m", "rewritten")
        self.cmd("-C", str(self.source), "tag", "v2")
        with self.assertRaises(RuntimeError):
            self.sync()
        self.assertEqual(mirror.refs(str(self.target), self.env), before)

    def test_lfs_objects_are_transferred(self):
        self.cmd("-C", str(self.source), "lfs", "install", "--local")
        self.cmd("-C", str(self.source), "lfs", "track", "*.bin")
        payload = b"test-lfs-payload\x00" * 100
        (self.source / "asset.bin").write_bytes(payload)
        self.cmd("-C", str(self.source), "add", ".")
        self.cmd("-C", str(self.source), "commit", "-m", "LFS asset")
        self.sync()
        checkout = self.root / "checkout"
        self.cmd("clone", "-b", "main", self.target.as_uri(), str(checkout))
        self.cmd("-C", str(checkout), "lfs", "pull")
        self.assertEqual((checkout / "asset.bin").read_bytes(), payload)


if __name__ == "__main__":
    unittest.main()
