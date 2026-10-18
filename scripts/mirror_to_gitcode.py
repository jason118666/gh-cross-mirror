"""扫描个人 GitHub 仓库并同步到 GitCode，仅依赖 Python 标准库和 Git。"""

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

MARKER = "Managed by gh-cross-mirror: "
STATE_BRANCH = "mirror-state"


class StopRun(RuntimeError):
    """鉴权、限流等全局问题应结束整次任务。"""


class BudgetReached(RuntimeError):
    """预留时间保存进度，不等 Actions 强制终止。"""


def flag(name, default=False):
    value = os.getenv(name, str(default)).lower()
    if value not in ("true", "false"):
        raise ValueError(f"{name} 只能填写 true 或 false")
    return value == "true"


def valid_name(value):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", value) or value in (".", ".."):
        raise ValueError("账号或仓库路径含有不支持的字符")
    return value


class API:
    def __init__(self, base, token):
        self.base = base
        self.token = token
        self.calls = 0

    def request(self, path, method="GET", data=None, missing_ok=False, accept="application/json"):
        headers = {"Accept": accept, "User-Agent": "gh-cross-mirror"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        body = None if data is None else json.dumps(data).encode("utf-8")
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(self.base + path, data=body, headers=headers, method=method)
        # 写入响应丢失时不重试，留到下次查询，防止重复建仓。
        attempts = 3 if method == "GET" else 1
        for attempt in range(attempts):
            time.sleep(1)
            self.calls += 1
            try:
                with urlopen(request, timeout=60) as response:
                    raw = response.read()
                    return json.loads(raw) if raw else None
            except HTTPError as error:
                if error.code == 404 and missing_ok:
                    return None
                if error.code in (401, 403, 418, 429):
                    raise StopRun(
                        f"API 返回 {error.code}，停止运行。检查权限、配额或限流；"
                        f"Retry-After={error.headers.get('Retry-After', '未提供')}，"
                        f"Reset={error.headers.get('X-RateLimit-Reset', '未提供')}"
                    ) from None
                if error.code >= 500 and attempt + 1 < attempts:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise RuntimeError(f"API {method} 失败，HTTP {error.code}") from None
            except (URLError, TimeoutError):
                if attempt + 1 == attempts:
                    raise RuntimeError("API 网络失败；请在下次运行时重新检查") from None

    def pages(self, path):
        page = 1
        while True:
            separator = "&" if "?" in path else "?"
            rows = self.request(f"{path}{separator}{urlencode({'per_page': 100, 'page': page})}")
            if not isinstance(rows, list):
                raise RuntimeError("仓库列表响应格式错误")
            yield from rows
            if len(rows) < 100:
                return
            page += 1


class SyncState:
    """成功状态和尝试顺序存到独立分支；预览只读取，不创建分支。"""

    def __init__(self, owner, namespace):
        self.context = {"version": 1, "owner": owner.lower(), "namespace": namespace.lower()}
        self.data = {**self.context, "repos": {}}
        self.original = json.dumps(self.data, sort_keys=True)
        self.sha = None
        self.branch_exists = False
        self.api = API("https://api.github.com", os.getenv("GITHUB_TOKEN", ""))
        control = os.getenv("GITHUB_REPOSITORY", "")
        self.path = None
        if control:
            parts = control.split("/")
            if len(parts) != 2:
                raise ValueError("GITHUB_REPOSITORY 格式错误")
            for part in parts:
                valid_name(part)
            self.path = f"/repos/{control}"
            content = self.api.request(
                self.path + f"/contents/.mirror-state.json?ref={STATE_BRANCH}",
                missing_ok=True, accept="application/vnd.github.object+json",
            )
            if content is None:
                if self.api.request(self.path + f"/git/ref/heads/{STATE_BRANCH}", missing_ok=True):
                    raise ValueError("mirror-state 分支已存在但缺少状态文件，请先核对，避免覆盖")
            else:
                self.branch_exists = True
                self.sha = content["sha"]
                # 大于 1 MB 时 Contents API 不返回正文，通过 blob 接口读取。
                if content.get("encoding") == "none":
                    content = self.api.request(self.path + f"/git/blobs/{self.sha}")
                loaded = json.loads(base64.b64decode(content["content"]))
                if not isinstance(loaded, dict) or any(loaded.get(k) != v for k, v in self.context.items()):
                    raise ValueError("状态文件版本或账号不匹配，停止运行以保护原进度")
                if not isinstance(loaded.get("repos"), dict) or any(
                    not isinstance(row, dict) or any(
                        not isinstance(row.get(key, 0), (int, float)) for key in ("checked", "attempt")
                    ) for row in loaded["repos"].values()
                ):
                    raise ValueError("状态文件格式错误，请核对 mirror-state 分支")
                self.data = loaded
                self.original = json.dumps(self.data, sort_keys=True)

    def save(self):
        raw = json.dumps(self.data, sort_keys=True)
        if self.path is None or raw == self.original:
            return
        if not self.branch_exists:
            # 先创建独立提交，最后发布分支引用，避免留下只有空分支的半成品。
            tree = self.api.request(self.path + "/git/trees", "POST", {"tree": [{
                "path": ".mirror-state.json", "mode": "100644", "type": "blob", "content": raw,
            }]})
            commit = self.api.request(self.path + "/git/commits", "POST", {
                "message": "chore: initialize mirror progress", "tree": tree["sha"], "parents": [],
            })
            self.api.request(self.path + "/git/refs", "POST", {
                "ref": f"refs/heads/{STATE_BRANCH}", "sha": commit["sha"],
            })
            self.branch_exists = True
            self.sha = tree["tree"][0]["sha"]
            self.original = raw
            return
        payload = {"message": "chore: save mirror progress", "branch": STATE_BRANCH,
                   "content": base64.b64encode(raw.encode("utf-8")).decode("ascii")}
        if self.sha:
            payload["sha"] = self.sha
        response = self.api.request(self.path + "/contents/.mirror-state.json", "PUT", payload)
        self.sha = response["content"]["sha"]
        self.original = raw


def fingerprint(values):
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode("utf-8")).hexdigest()


def source_fingerprint(repo):
    # 时间字段用来筛选；标签/分支漏报由轮换核对兜底，不把它当完整事件日志。
    return fingerprint({key: repo.get(key) for key in (
        "name", "pushed_at", "updated_at", "default_branch", "private",
    )})


def target_fingerprint(repo):
    return fingerprint({key: repo.get(key) for key in (
        "id", "homepage", "description", "private",
    )})


def target_index(rows, namespace):
    result = {}
    for repo in rows:
        # 列表可能混入组织项目，只接收目标个人命名空间。
        full_name = repo.get("full_name", "")
        if not isinstance(full_name, str) or "/" not in full_name:
            raise RuntimeError("GitCode 仓库列表缺少有效 full_name，停止处理")
        prefix, separator, name = full_name.partition("/")
        if separator and prefix.lower() == namespace.lower() and "/" not in name:
            result[valid_name(name).lower()] = repo
    return result


def candidates(repositories, targets, records, now):
    selected = []
    skipped = 0
    for repo in repositories:
        key = str(repo["id"])
        previous = records.get(key, {})
        target = targets.get(repo["name"].lower())
        changed = (target is None or previous.get("source") != source_fingerprint(repo)
                   or previous.get("target") != target_fingerprint(target)
                   or previous.get("pending", False))
        # 成功检查超过 7 天后再次核对引用；大批量时按最久未尝试的先处理。
        if changed or now - previous.get("checked", 0) >= 7 * 86400:
            selected.append((repo, not changed))
        else:
            skipped += 1
    selected.sort(key=lambda item: (
        item[1], records.get(str(item[0]["id"]), {}).get("attempt", 0), item[0]["name"].lower(),
    ))
    primary = [row for row in selected if not row[1]]
    audits = [row for row in selected if row[1]]
    ordered = []
    # 每 4 个变更项穿插 1 个轮换项，避免持续有更新时旧仓库始终得不到核对。
    for start in range(0, max(len(primary), len(audits) * 4), 4):
        ordered.extend(primary[start:start + 4])
        ordered.extend(audits[start // 4:start // 4 + 1])
    return ordered, skipped


def check_target(target, source, private):
    # 同名不代表归属；只允许推送带有本脚本标记的镜像。
    if not isinstance(target, dict):
        raise RuntimeError("无法读取目标仓库信息，停止同步")
    if target.get("homepage") != source or target.get("description") != MARKER + source:
        raise RuntimeError("同名仓库没有匹配的镜像标记，已保护；请按 README 核对")
    visibility = target.get("private")
    if visibility is None or type(visibility) not in (bool, int) or visibility not in (0, 1):
        raise RuntimeError("无法确认目标可见性，停止同步")
    if bool(visibility) != private:
        raise RuntimeError("源与目标可见性不同，停止同步；请先修正目标可见性")


def verify_listed_target(gc, namespace, name, listed, source, private):
    try:
        check_target(listed, source, private)
        return listed
    except RuntimeError:
        # 列表可能返回空值或摘要值；出现差异先查详情，不能直接认定来源错误。
        print(f"{name}: 列表核验存在差异，补查仓库详情确认", flush=True)
        detailed = gc.request(f"/repos/{namespace}/{name}")
        check_target(detailed, source, private)
        return detailed


def git(args, env):
    timeout = 900
    if env.get("MIRROR_DEADLINE"):
        remaining = float(env["MIRROR_DEADLINE"]) - time.time()
        if remaining <= 0:
            raise BudgetReached("本轮时间预算已用完，下轮继续")
        timeout = min(timeout, remaining)
    try:
        return subprocess.run(
            ["git", "-c", "credential.helper=", *args], env=env,
            check=True, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        ).stdout
    except subprocess.CalledProcessError as error:
        # 不输出远端原始错误，防止认证信息出现在日志中。
        raise RuntimeError(
            f"Git 失败（退出码 {error.returncode}）；"
            "检查令牌、分支保护、LFS 或非快进更新。未启用强制覆盖。"
        ) from None
    except subprocess.TimeoutExpired:
        if env.get("MIRROR_DEADLINE") and time.time() >= float(env["MIRROR_DEADLINE"]):
            raise BudgetReached("本轮时间预算已用完，下轮继续") from None
        raise RuntimeError("Git 操作超过 15 分钟") from None


def refs(url, env):
    output = git(["ls-remote", "--heads", "--tags", url], env)
    return {ref: sha for sha, ref in (line.split() for line in output.splitlines())
            if not ref.endswith("^{}")}


def sync_repo(source_url, target_url, env):
    source_refs = refs(source_url, env)
    target_refs = refs(target_url, env)
    # 保留目标独有引用；其余引用一致就不下载完整历史。
    if all(target_refs.get(ref) == sha for ref, sha in source_refs.items()):
        return "一致，跳过完整克隆" if source_refs else "源仓库为空"
    with tempfile.TemporaryDirectory() as temporary:
        bare = str(Path(temporary) / "repo.git")
        git(["clone", "--bare", source_url, bare], env)
        # LFS 对象不在普通 Git 包里，必须在发布引用之前单独传输。
        git(["-C", bare, "lfs", "fetch", "--all", "origin"], env)
        git(["-C", bare, "lfs", "push", "--all", target_url], env)
        refspecs = []
        if any(ref.startswith("refs/heads/") for ref in source_refs):
            refspecs.append("refs/heads/*:refs/heads/*")
        if any(ref.startswith("refs/tags/") for ref in source_refs):
            refspecs.append("refs/tags/*:refs/tags/*")
        # 不强制覆盖、不删除目标分支；一项失败则整组引用都不更新。
        git(["-C", bare, "push", "--atomic", target_url, *refspecs], env)
    return "已同步分支和标签"


def run():
    started = time.time()
    owner = valid_name(os.environ["GH_OWNER"].strip())
    namespace = valid_name(os.environ["GC_NAMESPACE"].strip())
    gc_token = os.environ["GC_TOKEN"].strip()
    if not gc_token:
        raise ValueError("请配置 GC_TOKEN")
    include_private = flag("INCLUDE_PRIVATE")
    dry_run = flag("DRY_RUN", True)
    gh_pat = os.getenv("GH_PAT", "").strip()
    if include_private and not gh_pat:
        raise ValueError("同步私有仓库必须配置 GH_PAT")
    gh = API("https://api.github.com", gh_pat or os.getenv("GITHUB_TOKEN", ""))
    gc = API("https://api.gitcode.com/api/v5", gc_token)
    if gc.request("/user").get("login", "").lower() != namespace.lower():
        raise ValueError("GC_NAMESPACE 必须是 GC_TOKEN 所属的个人用户名")
    if include_private:
        if gh.request("/user").get("login", "").lower() != owner.lower():
            raise ValueError("GH_PAT 必须属于 GH_OWNER 对应的个人账号")
        source_path = "/user/repos?affiliation=owner&visibility=all&sort=full_name"
    else:
        source_path = f"/users/{owner}/repos?type=owner&sort=full_name"
    repositories = list(gh.pages(source_path))
    state = SyncState(owner, namespace)
    # 先完整读取两边清单；任意一页失败就停止，不能把半份清单当作仓库不存在。
    targets = target_index(gc.pages("/user/repos?affiliation=owner&visibility=all&sort=full_name"), namespace)
    allow = {name.strip().lower() for name in os.getenv("REPO_ALLOWLIST", "").split(",") if name.strip()}
    exclude = {name.strip().lower() for name in os.getenv("REPO_EXCLUDE", "").split(",") if name.strip()}
    limit = int(os.getenv("MAX_NEW_REPOS", "10"))
    if not 1 <= limit <= 100:
        raise ValueError("MAX_NEW_REPOS 必须为 1 至 100")
    batch_limit = int(os.getenv("MAX_SYNC_REPOS", "200"))
    if not 1 <= batch_limit <= 1000:
        raise ValueError("MAX_SYNC_REPOS 必须为 1 至 1000")
    filtered = []
    for repo in repositories:
        if repo.get("owner", {}).get("login", "").lower() != owner.lower():
            continue
        name = valid_name(repo["name"])
        if name.lower() in exclude or (allow and name.lower() not in allow):
            continue
        if repo["private"] and not include_private:
            continue
        filtered.append(repo)
    queue, skipped = candidates(filtered, targets, state.data["repos"], started)
    current_ids = {str(repo["id"]) for repo in filtered}
    missing = sum(key not in current_ids for key in state.data["repos"])
    if not dry_run:
        git(["lfs", "version"], os.environ.copy())
    created, failed, processed, planned_new, deferred_new, deferred_batch = 0, 0, 0, 0, 0, 0
    results = []
    print(f"清单：GitHub {len(filtered)} 个，GitCode {len(targets)} 个；候选 {len(queue)}，状态未变跳过 {skipped}")
    if missing:
        print(f"{missing} 个已记录仓库不在本次范围或已不可见；保留 GitCode 备份，不自动删除")
    try:
        with tempfile.TemporaryDirectory() as temporary:
            askpass = Path(temporary) / "askpass.py"
            askpass.write_text(
                '#!/usr/bin/env python3\nimport os, sys\n'
                'prompt = sys.argv[1].lower()\n'
                'host = "GC" if "gitcode.com" in prompt else "GH"\n'
                'print(os.environ[host + ("_GIT_USER" if "username" in prompt else "_GIT_TOKEN")])\n',
                encoding="utf-8",
            )
            askpass.chmod(0o700)
            env = {**os.environ, "GIT_ASKPASS": str(askpass), "GIT_TERMINAL_PROMPT": "0",
                   "GC_GIT_USER": namespace, "GC_GIT_TOKEN": gc_token,
                   "GH_GIT_USER": "x-access-token", "GH_GIT_TOKEN": gh_pat or os.getenv("GITHUB_TOKEN", ""),
                   "GIT_LFS_SKIP_SMUDGE": "1", "MIRROR_DEADLINE": str(started + 90 * 60)}
            for position, (repo, audit) in enumerate(queue):
                if processed >= batch_limit or time.time() >= started + 90 * 60:
                    deferred_batch = len(queue) - position
                    break
                name = repo["name"]
                private = repo["private"]
                key = str(repo["id"])
                target = targets.get(name.lower())
                listed_fingerprint = target_fingerprint(target) if target is not None else None
                if target is None and (planned_new if dry_run else created) >= limit:
                    deferred_new += 1
                    continue
                source = f"https://github.com/{owner}/{name}"
                processed += 1
                result_count = len(results)
                if not dry_run:
                    state.data["repos"][key] = {
                        **state.data["repos"].get(key, {}), "name": name,
                        "attempt": time.time(), "pending": True,
                    }
                try:
                    if target is not None:
                        target = verify_listed_target(gc, namespace, name, target, source, private)
                    if dry_run:
                        if target is None:
                            planned_new += 1
                        results.append(f"{name}: 预览，{'核对已有镜像' if target else '将新建镜像'}（fork={repo['fork']}，private={private}）")
                        continue
                    if target is None:
                        gc.request("/user/repos", "POST", {
                            "name": name, "path": name, "public": 0 if private else 1,
                            "private": private, "auto_init": False,
                            "homepage": source, "description": MARKER + source,
                            "default_branch": repo.get("default_branch", "main"),
                        })
                        created += 1
                        # 建仓响应成功后仍重新核对可见性，确认后才传输代码。
                        target = gc.request(f"/repos/{namespace}/{name}")
                        check_target(target, source, private)
                        time.sleep(3)
                    message = sync_repo(source + ".git", f"https://gitcode.com/{namespace}/{name}.git", env)
                    state.data["repos"][key].update({
                        "source": source_fingerprint(repo),
                        # 下轮仍以同一个列表接口比较，避免列表/详情格式差异导致反复补查。
                        "target": listed_fingerprint or target_fingerprint(target),
                        "checked": time.time(), "pending": False,
                    })
                    results.append(f"{name}: {'轮换核对，' if audit else ''}{message}")
                except BudgetReached:
                    deferred_batch = len(queue) - position
                    results.append(f"{name}: 时间预算已用完，保存进度，下轮继续")
                    break
                except StopRun as error:
                    failed += 1
                    deferred_batch = len(queue) - position
                    results.append(f"{name}: 本轮停止，{error}")
                    raise
                except (RuntimeError, ValueError) as error:
                    failed += 1
                    results.append(f"{name}: 失败，{error}")
                    if failed >= 3:
                        deferred_batch = len(queue) - position - 1
                        print("累计 3 个仓库失败，保存已完成进度，停止本轮，请检查配置")
                        break
                finally:
                    if len(results) > result_count:
                        print(results[-1], flush=True)
    finally:
        saved = False
        try:
            if not dry_run:
                state.save()
            saved = True
        finally:
            totals = (f"模式：{'预览' if dry_run else '执行'}；新建 {created}；失败 {failed}；"
                      f"检查 {processed}；状态未变跳过 {skipped}；新建限额待续 {deferred_new}；"
                      f"批量/时间待续 {deferred_batch}；源不在范围/不可见 {missing}；"
                      f"API 请求尝试：GitHub {gh.calls + state.api.calls}，GitCode {gc.calls}；"
                      f"进度：{'预览不写入' if dry_run else ('已保存' if saved and state.path else ('本地临时状态' if saved else '保存失败，请查看错误'))}")
            print(totals, flush=True)
            if os.getenv("GITHUB_STEP_SUMMARY"):
                summary = "\n".join(f"- {row}" for row in results) or "本轮没有需要处理的仓库"
                Path(os.environ["GITHUB_STEP_SUMMARY"]).write_text(
                    f"{totals}\n\n{summary}\n\n完成本轮后结束；下一轮由定时或手动触发。\n",
                    encoding="utf-8",
                )
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(run())
    except (KeyError, ValueError, RuntimeError) as error:
        print(f"任务停止：{error}", file=sys.stderr)
        sys.exit(1)
