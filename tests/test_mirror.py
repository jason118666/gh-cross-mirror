import base64
import io
import json
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
        repos = [{"id": index, "owner": {"login": "alice"}, "name": name, "private": False, "fork": fork}
                 for index, (name, fork) in enumerate((("original", False), ("forked", True)))]
        calls = []

        def request(api, path, method="GET", data=None, missing_ok=False):
            calls.append((path, method))
            return {"login": "alice"} if path == "/user" else None

        with patch.dict(os.environ, {"GH_OWNER": "alice", "GC_NAMESPACE": "alice",
                                    "GC_TOKEN": "secret", "DRY_RUN": "true"}, clear=True), patch.object(
            mirror.API, "request", request
        ), patch.object(mirror.API, "pages", lambda api, path: iter(repos if "github" in api.base else [])), patch.object(mirror, "git") as git:
            self.assertEqual(mirror.run(), 0)
            self.assertFalse(any(path.startswith("/repos/alice/") for path, _ in calls))
            self.assertTrue(all(method == "GET" for _, method in calls))
            git.assert_not_called()

    def test_create_limit_and_private_visibility_gate(self):
        for private in (False, True):
            with self.subTest(private=private):
                repos = [{"id": index, "owner": {"login": "alice"}, "name": name,
                          "private": private, "fork": False} for index, name in enumerate(("one", "two"))]
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
                    mirror.API, "pages", lambda api, path: iter(repos if "github" in api.base else [])
                ), patch.object(mirror, "git"), patch.object(mirror.time, "sleep"), patch.object(
                    mirror, "sync_repo", return_value="synced"
                ) as sync:
                    self.assertEqual(mirror.run(), 1 if private else 0)
                    self.assertEqual(list(created), ["one"])
                    self.assertEqual(created["one"]["public"], 0 if private else 1)
                    self.assertEqual(sync.call_count, 0 if private else 1)


class IncrementalTests(unittest.TestCase):
    def setUp(self):
        self.variables = {"GH_OWNER": "alice", "GC_NAMESPACE": "alice", "GC_TOKEN": "secret",
                          "DRY_RUN": "false", "MAX_SYNC_REPOS": "2"}
        self.repos = [{"id": i, "owner": {"login": "alice"}, "name": f"repo{i}",
                       "private": False, "fork": True, "pushed_at": "first", "updated_at": "first",
                       "default_branch": "main"} for i in range(3)]
        self.targets = [{"id": i + 10, "full_name": f"alice/repo{i}", "private": False,
                         "homepage": f"https://github.com/alice/repo{i}",
                         "description": mirror.MARKER + f"https://github.com/alice/repo{i}"}
                        for i in range(3)]
        with patch.dict(os.environ, {}, clear=True):
            self.state = mirror.SyncState("alice", "alice")
        self.requests = []

    def execute(self, effect=None):
        def request(api, path, method="GET", data=None, missing_ok=False):
            self.requests.append((api.base, path, method))
            if path == "/user":
                return {"login": "alice"}
            raise AssertionError(f"不应逐仓查询：{path}")

        with patch.dict(os.environ, self.variables, clear=True), patch.object(
            mirror, "SyncState", return_value=self.state
        ), patch.object(mirror.API, "request", request), patch.object(
            mirror.API, "pages", lambda api, path: iter(self.repos if "github" in api.base else self.targets)
        ), patch.object(mirror, "git"), patch.object(
            mirror, "sync_repo", side_effect=effect, return_value="synced"
        ) as sync:
            code = mirror.run()
            return code, [call.args[0] for call in sync.call_args_list]

    def test_batch_resumes_and_unchanged_repos_skip_git(self):
        self.assertEqual(len(self.execute()[1]), 2)
        self.assertEqual(self.execute()[1], ["https://github.com/alice/repo2.git"])
        self.assertEqual(self.execute()[1], [])
        self.assertEqual(len(self.state.data["repos"]), 3)
        self.assertTrue(all(path == "/user" for _, path, _ in self.requests))

    def test_only_changed_repo_is_selected(self):
        self.variables["MAX_SYNC_REPOS"] = "10"
        self.execute()
        self.repos[1]["pushed_at"] = "second"
        self.assertEqual(self.execute()[1], ["https://github.com/alice/repo1.git"])

    def test_failed_work_remains_pending_and_does_not_starve_other_work(self):
        code, urls = self.execute([RuntimeError("test failure"), "synced"])
        self.assertEqual(code, 1)
        self.assertTrue(self.state.data["repos"]["0"]["pending"])
        self.assertNotIn("source", self.state.data["repos"]["0"])
        code, urls = self.execute()
        self.assertEqual(urls[0], "https://github.com/alice/repo2.git")
        self.assertFalse(self.state.data["repos"]["0"]["pending"])

    def test_periodic_audit_catches_changes_without_timestamp_update(self):
        self.variables["MAX_SYNC_REPOS"] = "10"
        self.execute()
        self.state.data["repos"]["1"]["checked"] = 1
        self.assertEqual(self.execute()[1], ["https://github.com/alice/repo1.git"])

    def test_missing_source_preserves_target_and_state(self):
        self.execute()
        self.repos = []
        self.assertEqual(self.execute()[1], [])
        self.assertEqual(len(self.targets), 3)
        self.assertEqual(len(self.state.data["repos"]), 2)

    def test_dry_run_does_not_advance_checkpoint(self):
        self.variables["DRY_RUN"] = "true"
        self.assertEqual(self.execute()[1], [])
        self.assertEqual(self.state.data["repos"], {})

    def test_target_recreation_forces_validation(self):
        self.execute()
        self.repos = self.repos[:1]
        self.targets[0]["id"] = 999
        self.targets[0]["description"] = "unrelated"
        code, urls = self.execute()
        self.assertEqual(code, 1)
        self.assertEqual(urls, [])

    def test_time_budget_defers_and_saves(self):
        with patch.object(self.state, "save") as save:
            code, urls = self.execute(mirror.BudgetReached("time budget"))
            self.assertEqual(code, 0)
            self.assertEqual(len(urls), 1)
            save.assert_called_once()
        self.assertTrue(self.state.data["repos"]["0"]["pending"])

    def test_limit_failure_still_checkpoints_completed_work(self):
        with patch.object(self.state, "save") as save:
            with self.assertRaises(mirror.StopRun):
                self.execute(["synced", mirror.StopRun("rate limited")])
            save.assert_called_once()
        self.assertFalse(self.state.data["repos"]["0"]["pending"])
        self.assertTrue(self.state.data["repos"]["1"]["pending"])

    def test_ten_thousand_repos_use_pages_instead_of_detail_requests(self):
        count = 10000
        sources = [{**self.repos[0], "id": i, "name": f"repo{i:05d}"} for i in range(count)]
        targets = [{**self.targets[0], "id": i, "full_name": f"alice/repo{i:05d}",
                    "homepage": f"https://github.com/alice/repo{i:05d}",
                    "description": mirror.MARKER + f"https://github.com/alice/repo{i:05d}"}
                   for i in range(count)]
        calls = []

        def request(api, path, method="GET", data=None, missing_ok=False):
            calls.append((api.base, path))
            if path == "/user":
                return {"login": "alice"}
            self.assertIn("per_page=100", path)
            page = int(path.rsplit("page=", 1)[-1])
            rows = sources if "github" in api.base else targets
            return rows[(page - 1) * 100:page * 100]

        with patch.dict(os.environ, self.variables, clear=True), patch.object(
            mirror, "SyncState", return_value=self.state
        ), patch.object(mirror.API, "request", request), patch.object(mirror, "git"), patch.object(
            mirror, "sync_repo", return_value="synced"
        ) as sync, patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(mirror.run(), 0)
        self.assertEqual(sum("github" in base for base, _ in calls), 101)
        self.assertEqual(sum("gitcode" in base for base, _ in calls), 102)
        self.assertEqual(sync.call_count, 2)

    def test_audits_get_a_slot_among_continuous_changes(self):
        repos = [{**self.repos[0], "id": i, "name": f"repo{i}"} for i in range(6)]
        targets = {r["name"]: {**self.targets[0], "id": r["id"]} for r in repos}
        last = repos[-1]
        records = {str(last["id"]): {"source": mirror.source_fingerprint(last),
                   "target": mirror.target_fingerprint(targets[last["name"]]), "checked": 1}}
        queue, _ = mirror.candidates(repos, targets, records, 10 * 86400)
        self.assertEqual(queue[4][0]["id"], last["id"])
        self.assertTrue(queue[4][1])


class StateTests(unittest.TestCase):
    def encoded(self, data):
        return {"sha": "old-sha", "encoding": "base64",
                "content": base64.b64encode(json.dumps(data).encode()).decode()}

    def test_initialization_publishes_complete_branch_last(self):
        replies = [None, None, {"sha": "tree", "tree": [{"sha": "blob"}]},
                   {"sha": "commit"}, {"ref": "refs/heads/mirror-state"}]
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "alice/control"}, clear=True), patch.object(
            mirror.API, "request", side_effect=replies
        ) as request:
            state = mirror.SyncState("alice", "alice")
            self.assertEqual(request.call_count, 2)  # 预览只读取，不初始化。
            state.data["repos"]["1"] = {"name": "demo"}
            state.save()
            self.assertEqual(request.call_args.args[0], "/repos/alice/control/git/refs")
            self.assertEqual(request.call_args.args[2]["sha"], "commit")
            self.assertEqual(state.sha, "blob")
            state.save()
            self.assertEqual(request.call_count, 5)

    def test_load_and_save_use_sha_for_conflict_protection(self):
        data = {"version": 1, "owner": "alice", "namespace": "alice", "repos": {}}
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "alice/control"}, clear=True), patch.object(
            mirror.API, "request", side_effect=[self.encoded(data), {"content": {"sha": "new-sha"}}]
        ) as request:
            state = mirror.SyncState("alice", "alice")
            state.data["repos"]["1"] = {"pending": True}
            state.save()
            payload = request.call_args.args[2]
            self.assertEqual(payload["sha"], "old-sha")
            self.assertEqual(payload["branch"], "mirror-state")
            self.assertEqual(state.sha, "new-sha")

    def test_wrong_context_or_existing_unmanaged_branch_is_rejected(self):
        wrong = {"version": 1, "owner": "other", "namespace": "alice", "repos": {}}
        for replies in ([self.encoded(wrong)], [None, {"ref": "exists"}]):
            with patch.dict(os.environ, {"GITHUB_REPOSITORY": "alice/control"}, clear=True), patch.object(
                mirror.API, "request", side_effect=replies
            ), self.assertRaises(ValueError):
                mirror.SyncState("alice", "alice")

    def test_large_state_uses_blob_fallback(self):
        data = {"version": 1, "owner": "alice", "namespace": "alice", "repos": {}}
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "alice/control"}, clear=True), patch.object(
            mirror.API, "request", side_effect=[{"sha": "large", "encoding": "none"}, self.encoded(data)]
        ) as request:
            mirror.SyncState("alice", "alice")
            self.assertEqual(request.call_args.args[0], "/repos/alice/control/git/blobs/large")

    def test_failed_save_does_not_claim_checkpoint_was_persisted(self):
        data = {"version": 1, "owner": "alice", "namespace": "alice", "repos": {}}
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "alice/control"}, clear=True), patch.object(
            mirror.API, "request", side_effect=[self.encoded(data), RuntimeError("conflict")]
        ):
            state = mirror.SyncState("alice", "alice")
            before = state.original
            state.data["repos"]["1"] = {"pending": True}
            with self.assertRaises(RuntimeError):
                state.save()
            self.assertEqual(state.original, before)

    def test_target_list_filters_other_namespaces(self):
        rows = [{"full_name": "alice/demo"}, {"full_name": "org/demo"}]
        self.assertEqual(list(mirror.target_index(rows, "alice")), ["demo"])

    def test_malformed_target_list_is_not_treated_as_missing_repositories(self):
        with self.assertRaises(RuntimeError):
            mirror.target_index([{"name": "demo"}], "alice")


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
