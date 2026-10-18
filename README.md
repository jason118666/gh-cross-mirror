# GitHub → GitCode 自动备份

每 6 小时扫描个人名下的 GitHub 仓库，包含原创项目和 fork；GitCode 缺少同名仓库时自动新建，再同步 Git 分支、标签和 LFS 对象。整个账号只需要这一份工作流。

采用 **GitHub Actions 主动推送**。不会开启 GitCode 网页里的原生 Pull 镜像：目前核查的公开 API 文档只有镜像配置查询接口，未找到可验证的启用接口。不要再给这些目标仓库开启另一套 Pull/Push 镜像。

## 1. 上传本项目

把本目录的改动提交、推送到你自己的 GitHub 控制仓库（当前远端是 `jason118666/gh-cross-mirror`）的默认分支。工作流必须在默认分支上才能定时执行。

无需在每个被备份项目里添加 Actions，也无需修改 fork 的代码。

## 2. 配置 GitCode 令牌

在 GitCode 个人设置里创建个人访问令牌，授予读取个人资料、查询/创建个人仓库和读写仓库所需权限；如果权限按 scope 展示，可使用 `api`、`read_repository`、`write_repository`，以当前界面的说明为准。有效期到期前需要更换。

进入 GitHub 控制仓库 **Settings → Secrets and variables → Actions**：

在 **Secrets** 中新增：

| 名称 | 内容 |
| --- | --- |
| `GC_TOKEN` | GitCode 个人访问令牌 |

在 **Variables** 中新增：

| 名称 | 内容 |
| --- | --- |
| `GC_NAMESPACE` | GitCode 个人用户名，即个人主页 URL 中的路径，不是昵称 |

令牌只放 Secrets，不要放 Variables、代码或聊天中。旧配置中将 `GC_NAMESPACE` 放 Secrets 也仍然兼容。

只备份公开仓库时无需 GitHub PAT：工作流使用内置令牌调用指定用户名的公开仓库列表，不会把内置令牌当成个人身份。`GH_OWNER` 默认取控制仓库所属账号；需要时可以在 Variables 显式填写个人 GitHub 用户名。不处理组织仓库。

## 3. 第一次先预览

建议先在 Variables 添加 `REPO_ALLOWLIST`，填写 1～3 个公开项目名称，英文逗号分隔；至少选一个 fork 测试。

1. 打开 **Actions → Mirror to GitCode → Run workflow**。
2. 保持“仅预览”勾选，点击运行。
3. 查看日志及 Summary，确认项目名单、fork 和 private 标识。

预览会调用只读 API 验证权限和同名仓库，不新建仓库、不克隆、不推送。已有同名但来源不匹配的项目会报错，防止误覆盖。

## 4. 执行首次同步

再次点击 **Run workflow**，取消“仅预览”勾选，再运行。

到 GitCode 检查对应项目的代码、分支、标签、默认分支和可见性。确认成功后，删除 `REPO_ALLOWLIST` 即可包含所有符合条件的项目。

每次最多新建 10 个仓库；超过部分会留到下一轮。可以再次手动执行继续处理，无需等 6 小时。不要对失败任务反复重跑，先查看失败原因。

## 5. 开启自动运行

1. 打开 GitHub 控制仓库（不是 GitCode）的 **Settings**。
2. 左侧点击 **Secrets and variables → Actions**。
3. 右侧切换到 **Variables** 标签，点击 **New repository variable**。
4. Name 填 `ENABLE_SYNC`，Value 填小写 `true`，点击 **Add variable**。若已存在此变量，点击铅笔修改即可。

`GC_TOKEN` 和 `GC_NAMESPACE` 已配置成功后不必重填；这个开关是额外的第三项配置，放在 Variables 中。

之后每天北京时间 **02:17、08:17、14:17、20:17** 附近运行。GitHub 定时任务可能延迟，不能保证精确到分钟。手动执行仍然默认仅预览。

暂停定时同步：删除 `ENABLE_SYNC` 或改为 `false`；也可在 Actions 页面禁用工作流。

### 怎么看下一轮是否在执行

打开 **Actions → Mirror to GitCode**，看最新运行的状态：

| 状态 | 含义 |
| --- | --- |
| In progress / 黄色转圈 | 本轮正在执行 |
| Queued / Waiting / Pending | 本轮已经触发，在排队或等待 |
| Success / 绿色勾 | 这一轮已经结束；不表示下一轮已经开始 |
| Failure / 红色叉 | 这一轮失败，需要查看具体日志 |
| Skipped / 跳过 | 若 mirror 作业被跳过，先检查 `ENABLE_SYNC=true` 是否已设置 |

“超出本次新建上限，下次继续”不会立即自动触发下一批。只有下次手动运行或定时时间到达，才会继续；等待下一次定时时间时，页面通常没有倒计时，也不会预先显示一条排队记录。

想立即完成剩余项目：等上一轮结束，点击 **Run workflow**，分支选择 `main`，取消“仅预览”后运行。已有镜像会先检查，一致就跳过完整克隆，再处理剩下的项目。

提交工作流更新本身不会启动同步，已经运行或排队的旧任务也不会自动换成新版本。要立即使用新版，请从工作流页面新建一次 **Run workflow**；不要在旧运行页面点 **Re-run jobs**，后者仍使用旧提交。

## 可选配置

以下均为 GitHub Actions 的 **Variables**，除 `GH_PAT` 外：

| 名称 | 默认值 | 用途 |
| --- | --- | --- |
| `GH_OWNER` | 控制仓库所属账号 | GitHub 个人用户名 |
| `REPO_ALLOWLIST` | 空 | 只处理这些项目名称，英文逗号分隔 |
| `REPO_EXCLUDE` | 空 | 排除这些项目名称，优先于白名单 |
| `MAX_NEW_REPOS` | `10` | 每次新建上限，允许 1～100；建议维持默认 |
| `INCLUDE_PRIVATE` | `false` | 设为 `true` 纳入私有仓库，必须同时配置下面的 Secret |
| `GH_PAT`（Secret） | 空 | 私有仓库读取令牌，必须属于 `GH_OWNER` |

### 私有仓库

先创建细粒度 GitHub PAT：选择自己的账号为资源所有者、需要备份的仓库（要覆盖未来新仓库则选择所有仓库），授予 **Contents: Read-only** 及自动提供的 Metadata 读取权限。保存为 `GH_PAT` Secret，再将 `INCLUDE_PRIVATE` 设为 `true`。实际可扫描范围受令牌授权限制。

源私有仓库只会新建为私有。推送前重新读取 GitCode 可见性，无法确认或不一致就停止该仓库，绝不自动改为公开。源仓库改为私有后，目标如已公开，脚本会报错，**需要你立即在 GitCode 手动设为私有**，脚本不会自动撤回已经公开的内容。

控制仓库公开时，运行日志和 Summary 可能公开私有项目名称；有私有项目时建议使用私有控制仓库，并留意 Actions 分钟额度。

### 旧的同名仓库

不会因同名就接管。脚本创建的项目使用以下标记：

- 主页：`https://github.com/你的用户名/项目名`
- 描述：`Managed by gh-cross-mirror: https://github.com/你的用户名/项目名`

若要接管以前建的仓库，先确认内容、可见性与源一致，且目标没有需要保留的独立开发；再在 GitCode 项目设置里手动填写上述两个字段，字符和大小写应与日志来源一致。这个操作表示允许脚本后续推送。不要给无关仓库添加标记。

## 同步方式和边界

- 默认包含 fork、原创、已归档及空仓库；只处理个人拥有的项目。星标、关注或参与的别人的项目不会被搬运。
- fork 的源是 `你的账号/项目`，不会自动更新 GitHub fork 到上游最新版本。
- 先用 `git ls-remote` 比较源与目标分支、标签。源引用都一致就跳过完整克隆；目标独有引用保留。
- 有变化时在临时目录完整裸克隆，随后传输 LFS，再原子推送分支和标签；结束后删除临时目录。不跨轮缓存完整仓库，因此发生变化的仓库仍有完整下载成本。
- 不强推、不删除目标独有分支或标签；GitHub 上的 rebase、强推、标签移动可能导致非快进失败，需要人工处理。目标作为备份使用，不在两边同时开发。
- 如果 GitCode 不支持原子推送或分支保护阻止更新，任务会失败，不降级为强制或部分引用推送。需人工检查设置。
- 不迁移 Issues、PR、Release 附件、Wiki、Packages、权限、fork 关系等平台数据；子模块只保留引用，不递归备份子模块仓库。
- LFS 会单独传输；若 LFS 失败则不发布新引用。引用完全一致时不会重新校验历史 LFS 对象；接管旧镜像前应自行确认 LFS 完整。
- 新建仓库会传入源默认分支；仍应在首次验收时检查 GitCode 默认分支。源默认分支后续改名时，需要手动调整 GitCode 默认分支设置。
- 这是保留历史的备份同步，不是会强制删除所有差异的逐字节镜像。

## 限流、配额与故障处理

普通 GitHub PAT 通常为 5000 API 请求/小时；内置 `GITHUB_TOKEN` 通常为每仓库 1000 次/小时。本项目公开仓库扫描使用后者，列表每页 100 项；例如 200 个仓库，列表通常约 3 次请求（含结束分页），不是每个仓库调用一次 GitHub API。

GitCode 默认 API 限额为 400 次/分钟、4000 次/小时。脚本每个 API 请求至少间隔 1 秒，每个项目一次存在性查询，新建另加创建和核验请求；新建串行处理且每轮默认最多 10 个。API 请求额度与 Git 克隆、LFS 流量是不同限制，低于 API 限额不等于没有其他风控。

- 收到 `401/403/418/429`：立即停止本轮，日志保留 Retry-After/Reset 提示，不循环重试。先检查权限或配额；若是限流，至少等平台指定时间后再重跑。若指定等待时间超过 6 小时，先关闭 `ENABLE_SYNC`，到期再开启。
- GET 网络/服务器异常：最多 3 次尝试；建仓 POST 不自动重试，下一轮重新检查仓库是否已创建。
- 累计 3 个项目失败：停止，避免对大量仓库重复执行错误操作。每次运行最多 120 分钟，每个 Git 命令最多 15 分钟。
- 私有仓库发现为公开：立即到 GitCode 修正目标可见性。
- 新建后暂时不可读取：本轮失败，下一轮会重新核验已有仓库，不会直接盲目推送。
- GitCode 项目数量、代码/LFS 存储额度：在个人设置 → 用量与存储查看，以实际账号额度为准。
- 公开控制仓库连续 60 天没有仓库活动，GitHub 可能停用定时任务；Actions 的成功运行本身不能保证保活。请检查是否停用，需要时手动重新启用。未添加刷提交保活逻辑。
- 请在 GitHub 通知设置中开启 Actions 失败邮件通知，并定期检查最近成功运行时间。

批量镜像不是零封号风险；不要绕过限流、批量刷仓或搬运无权再发布的内容。fork 应保留原许可证及出处；没有许可证或授权不清楚的项目，应先排除并核实许可。

官方依据（核查日期：2026-10-18）：[GitHub API 限流](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)、[GitCode API](https://docs.gitcode.com/docs/apis/)、[GitCode 个人仓库 API](https://docs.gitcode.com/en/docs/users/)、[GitCode 镜像功能](https://docs.gitcode.com/docs/help/home/org_project/project_manage/project_settings/repository_mirroring/)、[GitCode 镜像查询接口](https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-repo-remote-mirror/)、[GitHub 定时任务](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)、[GitCode 用量](https://docs.gitcode.com/docs/help/home/user_center/data_management/usage_and_storage/)、[GitHub 使用政策](https://docs.github.com/en/site-policy/acceptable-use-policies/github-acceptable-use-policies)、[GitCode 服务条款](https://docs.gitcode.com/docs/help/home/protocol/terms-of-service/)。

## 开发验证

Python 3.11+、Git 和 Git LFS：

```sh
python -m unittest discover -s tests -v
```

测试使用模拟 API 和本地临时 Git 仓库，不调用真实平台、不创建线上项目。覆盖分页、fork、预览、限流停止、同名/可见性保护、分支与标签、重复运行、非快进原子拒绝及 LFS 内容传输。真实 GitCode 令牌权限、API 返回格式、分支策略及存储额度仍需按首次同步步骤验收。
