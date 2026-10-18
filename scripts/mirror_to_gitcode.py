"""
定时扫描 GitHub 账号下的仓库（默认只备份 fork），
在 GitCode 不存在同名仓库时，调用官方 API 自动建仓并通过 import_url 拉取全量代码；
已存在的仓库用 git mirror push 增量同步最新内容。

所需环境变量（GitHub Secrets）：
  GITHUB_TOKEN   GitHub 内置，无需手动添加
  GC_TOKEN       GitCode 个人访问令牌(PAT)，需有“新建/管理项目”权限
  GC_NAMESPACE   GitCode 用户名（gitcode.com/后面那串，不是昵称）

可选环境变量：
  ONLY_FORK      默认 true，只同步 fork；设为 false 则同步名下全部公开仓库
  MAKE_PRIVATE   默认 false，新建仓为公开；设为 true 则私有
"""
import os
import subprocess
import tempfile

import requests
from github import Github

GC_API = "https://api.gitcode.com/api/v5"
GH_TOKEN = os.getenv("GITHUB_TOKEN")
GC_TOKEN = os.getenv("GC_TOKEN")
GC_NS = os.getenv("GC_NAMESPACE", "").strip()
ONLY_FORK = os.getenv("ONLY_FORK", "true").lower() != "false"
MAKE_PRIVATE = os.getenv("MAKE_PRIVATE", "false").lower() == "true"


def ensure_git_url(repo) -> str:
    url = repo.clone_url
    if not url.endswith(".git"):
        url = url + ".git"
    return url


def repo_exists_on_gitcode(name: str) -> bool:
    r = requests.get(
        f"{GC_API}/repos/{GC_NS}/{name}",
        params={"access_token": GC_TOKEN},
        timeout=30,
    )
    return r.status_code == 200


def create_on_gitcode(name: str, import_url: str) -> bool:
    payload = {
        "name": name,
        "private": MAKE_PRIVATE,
        "auto_init": False,
        "import_url": import_url,
    }
    r = requests.post(
        f"{GC_API}/user/repos",
        params={"access_token": GC_TOKEN},
        json=payload,
        timeout=60,
    )
    if r.status_code in (200, 201, 202):
        print(f"[OK] 已自动建仓并提交导入任务: {GC_NS}/{name}")
        return True
    print(f"[FAIL] 建仓失败 {name} ({r.status_code}): {r.text[:300]}")
    return False


def mirror_push_to_gitcode(name: str, gh_url: str) -> None:
    """已存在的仓库：GitHub 裸克隆后 mirror push 到 GitCode，做增量最新同步。"""
    target = f"https://{GC_NS}:{GC_TOKEN}@gitcode.com/{GC_NS}/{name}.git"
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(
            ["git", "clone", "--mirror", gh_url, tmp],
            check=True, capture_output=True, timeout=600,
        )
        subprocess.run(
            ["git", "-C", tmp, "push", "--mirror", target],
            check=True, capture_output=True, timeout=600,
        )
    print(f"[OK] 已增量同步最新: {GC_NS}/{name}")


def main() -> None:
    if not GC_TOKEN or not GC_NS:
        raise SystemExit("缺少 GC_TOKEN 或 GC_NAMESPACE，请在仓库 Secrets 里配置")

    gh = Github(GH_TOKEN)
    user = gh.get_user()
    count = 0

    for repo in user.get_repos(sort="updated"):
        if ONLY_FORK and not repo.fork:
            continue
        if repo.private:
            print(f"[SKIP] 私有仓库不同步: {repo.name}")
            continue

        name = repo.name
        gh_url = ensure_git_url(repo)
        count += 1
        print(f"\n=== 处理 {name} (fork={repo.fork}) ===")

        if repo_exists_on_gitcode(name):
            print(f"[--] GitCode 已存在 {GC_NS}/{name}，执行增量同步")
            try:
                mirror_push_to_gitcode(name, gh_url)
            except subprocess.CalledProcessError as e:
                print(f"[WARN] 增量推送失败（不影响其它仓库）: {e.stderr.decode()[:300]}")
        else:
            create_on_gitcode(name, gh_url)

    print(f"\n完成，本次共扫描 {count} 个符合条件的仓库。")


if __name__ == "__main__":
    main()
