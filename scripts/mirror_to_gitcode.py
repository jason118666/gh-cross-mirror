"""扫描个人 GitHub 仓库并同步到 GitCode，仅依赖 Python 标准库和 Git。"""

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


class StopRun(RuntimeError):
    """鉴权、限流等全局问题应结束整次任务。"""


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

    def request(self, path, method="GET", data=None, missing_ok=False):
        headers = {"Accept": "application/json", "User-Agent": "gh-cross-mirror"}
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


def check_target(target, source, private):
    # 同名不代表归属；只允许推送带有本脚本标记的镜像。
    if target.get("homepage") != source or target.get("description") != MARKER + source:
        raise RuntimeError("同名仓库没有匹配的镜像标记，已保护；请按 README 核对")
    visibility = target.get("private")
    if visibility is None or type(visibility) not in (bool, int) or visibility not in (0, 1):
        raise RuntimeError("无法确认目标可见性，停止同步")
    if bool(visibility) != private:
        raise RuntimeError("源与目标可见性不同，停止同步；请先修正目标可见性")


def git(args, env):
    try:
        return subprocess.run(
            ["git", "-c", "credential.helper=", *args], env=env,
            check=True, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900,
        ).stdout
    except subprocess.CalledProcessError as error:
        # 不输出远端原始错误，防止认证信息出现在日志中。
        raise RuntimeError(
            f"Git 失败（退出码 {error.returncode}）；"
            "检查令牌、分支保护、LFS 或非快进更新。未启用强制覆盖。"
        ) from None
    except subprocess.TimeoutExpired:
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
    allow = {name.strip().lower() for name in os.getenv("REPO_ALLOWLIST", "").split(",") if name.strip()}
    exclude = {name.strip().lower() for name in os.getenv("REPO_EXCLUDE", "").split(",") if name.strip()}
    limit = int(os.getenv("MAX_NEW_REPOS", "10"))
    if not 1 <= limit <= 100:
        raise ValueError("MAX_NEW_REPOS 必须为 1 至 100")
    if not dry_run:
        git(["lfs", "version"], os.environ.copy())
    created, failed = 0, 0
    results = []
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
                   "GIT_LFS_SKIP_SMUDGE": "1"}
            for repo in repositories:
                if repo.get("owner", {}).get("login", "").lower() != owner.lower():
                    continue
                name = valid_name(repo["name"])
                if name.lower() in exclude or (allow and name.lower() not in allow):
                    continue
                private = repo["private"]
                if private and not include_private:
                    continue
                source = f"https://github.com/{owner}/{name}"
                try:
                    target = gc.request(f"/repos/{namespace}/{name}", missing_ok=True)
                    if target is not None:
                        check_target(target, source, private)
                    elif created >= limit:
                        results.append(f"{name}: 超出本次新建上限，下次继续")
                        continue
                    if dry_run:
                        results.append(f"{name}: 预览，{'同步已有镜像' if target else '将新建镜像'}（fork={repo['fork']}，private={private}）")
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
                    results.append(f"{name}: {message}")
                except StopRun:
                    raise
                except (RuntimeError, ValueError) as error:
                    failed += 1
                    results.append(f"{name}: 失败，{error}")
                    if failed >= 3:
                        raise StopRun("累计 3 个仓库失败，停止本次任务，请先检查配置") from None
                finally:
                    if results:
                        print(results[-1], flush=True)
    finally:
        if os.getenv("GITHUB_STEP_SUMMARY"):
            summary = "\n".join(f"- {row}" for row in results) or "没有处理结果；请检查日志"
            Path(os.environ["GITHUB_STEP_SUMMARY"]).write_text(
                f"模式：{'预览' if dry_run else '执行'}；新建 {created}；失败 {failed}\n\n{summary}\n",
                encoding="utf-8",
            )
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(run())
    except (KeyError, ValueError, RuntimeError) as error:
        print(f"任务停止：{error}", file=sys.stderr)
        sys.exit(1)
