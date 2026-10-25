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
NAME_MAP_FILE = Path(__file__).resolve().parent.parent / "repo-name-map.json"


class StopRun(RuntimeError):
    """鉴权、限流等全局问题应结束整次任务。"""


class BudgetReached(RuntimeError):
    """预留时间保存进度，不等 Actions 强制终止。"""


class APIError(RuntimeError):
    def __init__(self, method, status, path="", detail=""):
        self.status = status
        super().__init__(f"API {method} {path} 失败，HTTP {status}".replace("  ", " ")
                         + (f"；平台原因：{detail}" if detail else ""))


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
                # 只取平台的诊断字段；不回显请求、完整响应或令牌。
                detail = ""
                try:
                    payload = json.loads(error.read(16384))
                    if isinstance(payload, dict):
                        detail = "; ".join(
                            f"{key}={json.dumps(payload[key], ensure_ascii=False)}"
                            for key in ("message", "msg", "error", "errors", "error_description",
                                        "error_message", "error_msg", "code", "error_code")
                            if payload.get(key) is not None
                        )
                except (ValueError, OSError):
                    pass
                for token in (self.token, os.getenv("GC_TOKEN", ""), os.getenv("GH_PAT", ""),
                              os.getenv("GITHUB_TOKEN", "")):
                    if token:
                        detail = detail.replace(token, "***")
                detail = re.sub(r"https?://[^\s\"<>]+", "[URL 已隐藏]", detail)
                detail = re.sub(r"(?i)(bearer\s+|(?:token|password|secret)[\"\s:=]+)[^\s,;\"}]+",
                                r"\1***", detail)
                detail = " ".join(detail.split())[:800] or "平台未提供可用的 JSON 错误信息"
                raise APIError(method, error.code, path.split("?", 1)[0], detail) from None
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


def repo_name_map(repositories):
    try:
        aliases = json.loads(NAME_MAP_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError('无法读取 repo-name-map.json，请检查文件存在且为 JSON 对象，例如 {"source":"target"}') from None
    if not isinstance(aliases, dict):
        raise ValueError("repo-name-map.json 必须是 JSON 对象")
    names = {}
    for source, target in aliases.items():
        if not isinstance(target, str):
            raise ValueError("repo-name-map.json 的目标路径必须是字符串")
        key = valid_name(source).lower()
        if key in names:
            raise ValueError("repo-name-map.json 含大小写重复的源仓库名")
        names[key] = valid_name(target)
    destinations = set()
    for repo in repositories:
        target = names.get(repo["name"].lower(), repo["name"])
        # 不允许两个源仓库映射到同一个目标，包含未改名的源仓库。
        if target.lower() in destinations:
            raise ValueError(f"GitCode 目标路径冲突：{target}；请调整 repo-name-map.json")
        destinations.add(target.lower())
    return names


def candidates(repositories, targets, records, now, sync_forks=False, name_map=None):
    selected = []
    skipped = 0
    for repo in repositories:
        key = str(repo["id"])
        previous = records.get(key, {})
        target_name = (name_map or {}).get(repo["name"].lower(), repo["name"])
        target = targets.get(target_name.lower())
        changed = ((sync_forks and repo.get("fork", False) and not repo.get("archived", False))
                   or target is None or previous.get("source") != source_fingerprint(repo)
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


def check_target(target, source, private, identity=None):
    # 同名不代表归属；只允许推送带有本脚本标记的镜像。
    if not isinstance(target, dict):
        raise RuntimeError("无法读取目标仓库信息，停止同步")
    # 改名镜像可用 GitHub 数字 ID 地址标记归属，避免原名称再次触发元数据审核。
    markers = {source, identity} if identity else {source}
    if not any(target.get("homepage") == value and target.get("description") == MARKER + value
               for value in markers):
        raise RuntimeError("同名仓库没有匹配的镜像标记，已保护；请按 README 核对")
    visibility = target.get("private")
    if visibility is None or type(visibility) not in (bool, int) or visibility not in (0, 1):
        raise RuntimeError("无法确认目标可见性，停止同步")
    if bool(visibility) != private:
        raise RuntimeError("源与目标可见性不同，停止同步；请先修正目标可见性")


def verify_listed_target(gc, namespace, name, listed, source, private, identity=None):
    try:
        check_target(listed, source, private, identity)
        return listed
    except RuntimeError:
        # 列表可能返回空值或摘要值；出现差异先查详情，不能直接认定来源错误。
        print(f"{name}: 列表核验存在差异，补查仓库详情确认", flush=True)
        detailed = gc.request(f"/repos/{namespace}/{name}")
        check_target(detailed, source, private, identity)
        return detailed


def sync_fork(gh, owner, repo):
    """让 GitHub 安全合并上游默认分支，不使用强推或重置。"""
    branch = repo.get("default_branch")
    if not isinstance(branch, str) or not branch:
        raise RuntimeError("上游同步失败：无法确认 fork 默认分支")
    path = f"/repos/{owner}/{repo['name']}"
    try:
        response = gh.request(path + "/merge-upstream", "POST", {"branch": branch})
    except APIError as error:
        if error.status == 409:
            raise RuntimeError("上游同步冲突：保留你的提交，本仓库不推送 GitCode，请先手动解决冲突") from None
        raise RuntimeError(f"上游同步失败（HTTP {error.status}），检查分支、上游状态及 GH_PAT 授权范围") from None
    except StopRun as error:
        raise StopRun(f"上游同步停止：{error}；GH_PAT 需覆盖该 fork 并具有 Contents 写入权限") from None
    if not isinstance(response, dict) or response.get("merge_type") not in ("none", "merge", "fast-forward"):
        raise RuntimeError("无法确认上游同步结果，本仓库不推送 GitCode")
    if response["merge_type"] == "none":
        return repo, False
    # 上游写入发生后刷新元数据，防止用旧时间戳把刚更新的代码跳过。
    refreshed = gh.request(path)
    if (not isinstance(refreshed, dict) or refreshed.get("id") != repo["id"]
            or refreshed.get("name") != repo["name"]
            or refreshed.get("owner", {}).get("login", "").lower() != owner.lower()
            or type(refreshed.get("private")) is not bool):
        raise RuntimeError("上游已更新，但仓库身份或可见性无法确认，暂不推送 GitCode")
    return refreshed, True


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
        command = args[2:] if args[0] == "-C" else args
        operation = " ".join(command[:2] if command[0] == "lfs" else command[:1])
        output = (error.stderr or "") + "\n" + (error.stdout or "")
        # 只输出固定诊断，不回显可能含令牌的命令、URL 或远端原始错误。
        reasons = [message for pattern, message in (
            (r"non-fast-forward|fetch first", "分支不是快进更新，源和目标历史存在分叉或改写"),
            (r"stale info", "目标引用在核对后发生变化，本次停止覆盖，下轮重新核对"),
            (r"already exists|would clobber existing tag", "同名标签已存在且指向不同提交"),
            (r"protected branch|protected ref|pre-receive hook declined|hook declined",
             "远端保护规则或接收钩子拒绝推送"),
            (r"does not support.*atomic|atomic.*not supported", "目标服务不支持原子推送"),
            (r"authentication failed|access denied|permission denied|\b40[13]\b",
             "认证失败或没有访问权限"),
            (r"repository.*not found", "远端仓库不存在或当前令牌不可见"),
            (r"missing.*object|object does not exist|\b404\b", "远端对象或资源不存在，检查 LFS 对象是否缺失"),
            (r"quota|exceed.*limit|file.*too large", "远端配额、文件大小或传输限制"),
            (r"could not resolve|failed to connect|timed out|connection.*reset|\b50[0234]\b",
             "网络连接或远端服务异常"),
        ) if re.search(pattern, output, re.IGNORECASE)]
        reason = "；".join(reasons) or "未识别的 Git 错误，需进一步检查远端拒绝原因"
        raise RuntimeError(
            f"Git {operation} 失败（退出码 {error.returncode}）；{reason}。"
        ) from None
    except subprocess.TimeoutExpired:
        if env.get("MIRROR_DEADLINE") and time.time() >= float(env["MIRROR_DEADLINE"]):
            raise BudgetReached("本轮时间预算已用完，下轮继续") from None
        raise RuntimeError("Git 操作超过 15 分钟") from None


def refs(url, env):
    output = git(["ls-remote", "--heads", "--tags", url], env)
    return {ref: sha for sha, ref in (line.split() for line in output.splitlines())
            if not ref.endswith("^{}")}


def sync_repo(source_url, target_url, env, source_wins=False):
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
        leases = []
        if source_wins:
            # 仅覆盖已核对的引用；目标中途变化就整批拒绝，不删除目标独有引用。
            refspecs = []
            for ref, sha in source_refs.items():
                if target_refs.get(ref) != sha:
                    leases.append(f"--force-with-lease={ref}:{target_refs.get(ref, '')}")
                    refspecs.append(f"{sha}:{ref}")
        git(["-C", bare, "push", "--atomic", *leases, target_url, *refspecs], env)
    return "已同步分支和标签" + ("（以 GitHub 为准，已校验目标旧提交）" if source_wins else "")


def run():
    started = time.time()
    owner = valid_name(os.environ["GH_OWNER"].strip())
    namespace = valid_name(os.environ["GC_NAMESPACE"].strip())
    gc_token = os.environ["GC_TOKEN"].strip()
    if not gc_token:
        raise ValueError("请配置 GC_TOKEN")
    include_private = flag("INCLUDE_PRIVATE")
    sync_forks = flag("SYNC_FORKS")
    dry_run = flag("DRY_RUN", True)
    gh_pat = os.getenv("GH_PAT", "").strip()
    if include_private and not gh_pat:
        raise ValueError("同步私有仓库必须配置 GH_PAT")
    if sync_forks and not gh_pat:
        raise ValueError("自动更新 fork 需要 GH_PAT Secret（Contents 读写）；只备份现有代码可将 SYNC_FORKS 设为 false")
    gh = API("https://api.github.com", gh_pat or os.getenv("GITHUB_TOKEN", ""))
    gc = API("https://api.gitcode.com/api/v5", gc_token)
    if gc.request("/user").get("login", "").lower() != namespace.lower():
        raise ValueError("GC_NAMESPACE 必须是 GC_TOKEN 所属的个人用户名")
    if include_private or sync_forks:
        if gh.request("/user").get("login", "").lower() != owner.lower():
            raise ValueError("GH_PAT 必须属于 GH_OWNER 对应的个人账号")
    if include_private:
        source_path = "/user/repos?affiliation=owner&visibility=all&sort=full_name"
    else:
        source_path = f"/users/{owner}/repos?type=owner&sort=full_name"
    repositories = list(gh.pages(source_path))
    state = SyncState(owner, namespace)
    # 先完整读取两边清单；任意一页失败就停止，不能把半份清单当作仓库不存在。
    targets = target_index(gc.pages("/user/repos?affiliation=owner&visibility=all&sort=full_name"), namespace)
    allow = {name.strip().lower() for name in os.getenv("REPO_ALLOWLIST", "").split(",") if name.strip()}
    exclude = {name.strip().lower() for name in os.getenv("REPO_EXCLUDE", "").split(",") if name.strip()}
    source_wins_repos = {name.strip().lower() for name in os.getenv("SOURCE_WINS_REPOS", "").split(",") if name.strip()}
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
    name_map = repo_name_map(filtered)
    queue, skipped = candidates(filtered, targets, state.data["repos"], started, sync_forks, name_map)
    current_ids = {str(repo["id"]) for repo in filtered}
    missing = sum(key not in current_ids for key in state.data["repos"])
    if not dry_run:
        git(["lfs", "version"], os.environ.copy())
    created, failed, processed, planned_new, deferred_new, deferred_batch = 0, 0, 0, 0, 0, 0
    upstream_updated, upstream_current, upstream_failed, mirror_done = 0, 0, 0, 0
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
                target_name = name_map.get(name.lower(), name)
                label = name if name == target_name else f"{name} → {target_name}"
                private = repo["private"]
                key = str(repo["id"])
                target = targets.get(target_name.lower())
                listed_fingerprint = target_fingerprint(target) if target is not None else None
                if target is None and (planned_new if dry_run else created) >= limit:
                    deferred_new += 1
                    continue
                source = f"https://github.com/{owner}/{name}"
                identity = f"https://api.github.com/repositories/{repo['id']}" if target_name != name else source
                previous = dict(state.data["repos"].get(key, {}))
                needs_upstream = sync_forks and repo.get("fork", False) and not repo.get("archived", False)
                audit = audit or (needs_upstream and "source" in previous
                                  and started - previous.get("checked", 0) >= 7 * 86400)
                upstream_pending = needs_upstream and not dry_run
                updated = False
                upstream_message = "未启用" if repo.get("fork", False) else "非 fork，无需更新上游"
                if sync_forks and repo.get("fork", False) and repo.get("archived", False):
                    upstream_message = "已归档，跳过上游更新"
                processed += 1
                result_count = len(results)
                if not dry_run:
                    state.data["repos"][key] = {
                        **state.data["repos"].get(key, {}), "name": name,
                        "attempt": time.time(), "pending": True,
                    }
                try:
                    if upstream_pending:
                        repo, updated = sync_fork(gh, owner, repo)
                        upstream_pending = False
                        upstream_updated += int(updated)
                        upstream_current += int(not updated)
                        upstream_message = ("已更新" if updated else "已是最新") + f"（{repo['default_branch']}）"
                        private = repo["private"]
                        if private and not include_private:
                            raise RuntimeError("源仓库已改为私有，停止备份；请立即检查 GitCode 可见性")
                    if needs_upstream and dry_run:
                        upstream_message = f"预览，将安全同步默认分支 {repo.get('default_branch', '未知')}"
                    # 上游检查正常且本地状态未变时，不反复补查 GitCode 摘要字段或下载代码。
                    if (needs_upstream and not dry_run and not updated and not audit and target is not None
                            and not previous.get("pending", False)
                            and previous.get("source") == source_fingerprint(repo)
                            and previous.get("target") == listed_fingerprint):
                        state.data["repos"][key]["pending"] = False
                        skipped += 1
                        mirror_done += 1
                        results.append(f"{label}: 上游：{upstream_message}；GitCode：状态未变，跳过复查")
                        continue
                    if target is not None:
                        target = verify_listed_target(gc, namespace, target_name, target, source, private, identity)
                    if dry_run:
                        if target is None:
                            planned_new += 1
                        results.append(f"{label}: 上游：{upstream_message}；GitCode：预览，{'核对已有镜像' if target else '将新建镜像'}（private={private}）")
                        continue
                    if target is None:
                        gc.request("/user/repos", "POST", {
                            "name": target_name, "path": target_name, "public": 0 if private else 1,
                            "private": private, "auto_init": False,
                            "homepage": identity, "description": MARKER + identity,
                            "default_branch": repo.get("default_branch", "main"),
                        })
                        created += 1
                        # 建仓响应成功后仍重新核对可见性，确认后才传输代码。
                        target = gc.request(f"/repos/{namespace}/{target_name}")
                        check_target(target, source, private, identity)
                        time.sleep(3)
                    message = sync_repo(source + ".git", f"https://gitcode.com/{namespace}/{target_name}.git", env,
                                        source_wins=name.lower() in source_wins_repos)
                    state.data["repos"][key].update({
                        "source": source_fingerprint(repo),
                        # 下轮仍以同一个列表接口比较，避免列表/详情格式差异导致反复补查。
                        "target": listed_fingerprint or target_fingerprint(target),
                        "checked": time.time(), "pending": False,
                    })
                    mirror_done += 1
                    results.append(f"{label}: 上游：{upstream_message}；GitCode：{'轮换核对，' if audit else ''}{message}")
                except BudgetReached:
                    deferred_batch = len(queue) - position
                    results.append(f"{label}: 上游：{upstream_message}；GitCode：时间预算已用完，保存进度，下轮继续")
                    break
                except StopRun as error:
                    upstream_failed += int(upstream_pending)
                    failed += 1
                    deferred_batch = len(queue) - position
                    results.append(f"{label}: 上游：{'失败' if upstream_pending else upstream_message}；本轮停止，{error}")
                    raise
                except (RuntimeError, ValueError) as error:
                    upstream_failed += int(upstream_pending)
                    failed += 1
                    results.append(f"{label}: 上游：{'失败' if upstream_pending else upstream_message}；GitCode：未完成；{error}")
                    # 仅在 Actions 实际同步时生成注解，避免离线测试中的预期失败污染界面。
                    if os.getenv("GITHUB_ACTIONS") == "true":
                        annotation = results[-1].replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
                        print(f"::error::{annotation}", flush=True)
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
            stages = (f"上游更新 {upstream_updated}；上游已最新 {upstream_current}；上游失败 {upstream_failed}；"
                      f"GitCode 完成/状态未变 {mirror_done}")
            print(totals, flush=True)
            print(stages, flush=True)
            if os.getenv("GITHUB_STEP_SUMMARY"):
                summary = "\n".join(f"- {row}" for row in results) or "本轮没有需要处理的仓库"
                outcome = "失败，请检查下方记录" if failed or not saved else (
                    "预览，未执行同步" if dry_run else (
                        "本批完成，仍有待续项目" if deferred_new or deferred_batch else "本轮完成"
                    )
                )
                Path(os.environ["GITHUB_STEP_SUMMARY"]).write_text(
                    f"**结果：{outcome}**\n\n{totals}\n\n{stages}\n\n"
                    f"GitHub 来源查询/上游更新：{gh.calls} 次（{'GH_PAT' if gh_pat else '内置令牌'}）；"
                    f"状态读写：{state.api.calls} 次（内置令牌）。\n\n{summary}\n\n"
                    "完成本轮后结束；下一轮由定时或手动触发。\n",
                    encoding="utf-8",
                )
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(run())
    except (KeyError, ValueError, RuntimeError) as error:
        print(f"任务停止：{error}", file=sys.stderr)
        sys.exit(1)
