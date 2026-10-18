# GitHub → GitCode 自动备份

每 6 小时扫描个人名下的 GitHub 仓库，包含原创项目和 fork；先安全更新 fork 的上游默认分支，再向 GitCode 同步。GitCode 缺少同名仓库时自动新建，再同步你名下仓库的 Git 分支、标签和 LFS 对象。整个账号只需要这一份工作流。

两边都按每页 100 个读取仓库列表，在程序内比对；保存上次成功同步的状态，日常只处理新增、变化、失败项，另分批轮换核对其他仓库。

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

仅备份公开仓库现有代码时无需 GitHub PAT，但要将 Variables 中 `SYNC_FORKS` 设为 `false`。工作流默认开启自动更新 fork，必须按下一节配置 `GH_PAT`。`GH_OWNER` 默认取控制仓库所属账号；需要时可以在 Variables 显式填写个人 GitHub 用户名。不处理组织仓库。

### 配置 GitHub 细粒度令牌，用于自动更新 fork

令牌不用交给聊天或写入代码。自己保存到本 GitHub 控制仓库的 Secret：

1. 在 GitHub 个人设置 → Developer settings → Personal access tokens → Fine-grained tokens 创建令牌。
2. Resource owner 选择自己的账号；Repository access 覆盖所有准备自动更新的 fork。若选择 Selected repositories，以后新增 fork 时要补充授权；All repositories 会覆盖未来新仓库。
3. Repository permissions 的 **Contents** 选择 **Read and write**；Metadata 读取权限会自动附带。若还要备份私有项目，令牌也需覆盖那些项目。
4. 打开控制仓库 **Settings → Secrets and variables → Actions → Secrets → New repository secret**。
5. Name 填 **`GH_PAT`**，Secret 粘贴令牌原文，点击 **Add secret**。若已有同名只读令牌，更新为具有写入权限的令牌。

不用再添加开关，工作流默认 `SYNC_FORKS=true`。令牌必须属于 `GH_OWNER`。缺少令牌会明确报错，不会假装上游同步成功。遇到 403 时检查该 fork 是否在授权范围、Contents 是否可写、分支规则是否阻止更新；如果平台明确拒绝 `.github/workflows` 文件的更新，再给对应令牌补充 Workflows 写权限。

自动更新只针对 fork 的默认分支，使用 GitHub 官方 `merge-upstream` API，不强推、不重置你的提交。有合并冲突返回 409 时，本仓库保留原代码、标记失败并跳过 GitCode 推送；无冲突的上游更新合并成功后，在同一轮继续同步到 GitCode。已归档 fork 跳过上游写入，仍可备份现有代码。

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

新版首次正式运行会核对已有镜像并建立进度，不会重建已有仓库；之后无变化的项目会显示为“状态未变跳过”。每轮默认最多处理 200 个候选项目，其中最多新建 10 个。更新已有仓库不占新建名额，但占候选处理数量。

进度保存在本控制仓库的独立 **`mirror-state` 分支**，不会写入 `main`。工作流的内置 `GITHUB_TOKEN` 已配置 `contents: write`，用于创建此分支、保存状态；不需要增加 GitHub PAT。首次正式运行会自动初始化，无需手动创建分支。若保存状态时返回 403，请检查仓库/组织是否限制 Actions 写入或创建分支；若对该分支设置保护规则，请允许工作流更新。

预览不新建分支、不推进状态。不要合并状态分支到 main，也不要手动修改其中的 JSON。更换源账号或目标账号时，旧状态不匹配会停止，需要先核对并备份旧状态再重新初始化。

控制仓库自身会因为状态提交更新推送时间，可能每轮都被选中核对；这不代表其他未变化仓库也会重新下载。

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

列表里的 `Mirror to GitCode #7`、`#8` 等是同一个工作流的历史运行记录，不是多个定时工作流；保留即可，不需要删除。每 6 小时只触发这个工作流的新一轮，不会让每条历史记录各执行一遍。写入状态分支也不会额外启动同步。

Summary 会显示新建、失败、状态未变跳过、新建限额待续、批量/时间待续、两边 API 请求尝试次数及进度保存状态。请求尝试次数仅统计本脚本的 API 调用，包含重试，不含 Git/LFS、checkout/setup-python 等环境准备请求，也不是平台实时剩余额度。

### 每次到哪里看成功或失败

1. 打开控制仓库 **Actions → Mirror to GitCode**。
2. 点击列表最上方那一轮运行，查看 **Summary（摘要）→ mirror 摘要**。
3. 绿色勾表示本轮执行成功，红叉表示有失败；黄色转圈表示仍在运行。摘要顶部会说明“本轮完成”“本批完成，仍有待续项目”“预览，未执行同步”或“失败”。
4. 看各项目的两段结果，例如 `上游：已更新（main）；GitCode：已同步分支和标签`，或 `上游：已是最新（main）；GitCode：状态未变，跳过复查`。
5. 出现 `上游同步冲突`、`GitCode：未完成` 或 `进度：保存失败` 时，本轮会失败。点击左侧 **mirror → Scan and sync repositories** 查看具体错误。上游已更新而 GitCode 失败时，下一轮会继续重试，不把它记为全部成功。

如需邮件提醒，确保已关注本控制仓库，再打开 GitHub 个人设置 **Notifications → System → Actions**，选择 **Email**，可勾选 **Only notify for failed workflows**，点击 Save。使用 GitHub 自带通知即可，本项目不需要额外邮件令牌，不会向第三方发送消息。成功结果仍可在 Actions 摘要查看。[官方通知说明](https://docs.github.com/en/subscriptions-and-notifications/how-tos/managing-github-actions-notifications)

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
| `MAX_SYNC_REPOS` | `200` | 每轮候选处理上限，允许 1～1000；包含核对、更新、新建和失败尝试，超出的下轮继续 |
| `SYNC_FORKS` | `true`（工作流） | 先更新 fork 默认分支，需要 `GH_PAT` 写权限；设为 `false` 只备份当前 fork，不跟随上游 |
| `INCLUDE_PRIVATE` | `false` | 设为 `true` 纳入私有仓库，必须同时配置下面的 Secret |
| `GH_PAT`（Secret） | 空 | 自动更新 fork 需 Contents 读写；仅备份私有仓库可只读，必须属于 `GH_OWNER` |

### 私有仓库

先创建细粒度 GitHub PAT：选择自己的账号为资源所有者、需要备份的仓库（要覆盖未来新仓库则选择所有仓库），授予 **Contents: Read-only** 及自动提供的 Metadata 读取权限。保存为 `GH_PAT` Secret，再将 `INCLUDE_PRIVATE` 设为 `true`。实际可扫描范围受令牌授权限制。

源私有仓库只会新建为私有。依据 GitCode 列表检查来源和可见性；待处理项的列表字段缺失、为空或核验不一致时，先补查详情，以详情确认后才决定是否拦截。新建后还会重新读取核验，无法确认或不一致就停止该仓库，绝不自动改为公开。启用私有扫描时，源仓库改为私有后、目标如已公开，脚本会报错；仅扫描公开仓库时，改为私有的源只会显示为“不在本次范围/不可见”。**两种情况都需要你立即在 GitCode 手动设为私有**，脚本不会自动撤回已经公开的内容。

控制仓库公开时，运行日志、Summary 和状态分支可能公开私有项目名称与部分元数据，状态分支也可能随控制仓库一起备份到 GitCode；有私有项目时请使用私有控制仓库，并留意 Actions 分钟额度。状态文件不保存令牌或代码正文。

### 旧的同名仓库

不会因同名就接管。脚本创建的项目使用以下标记：

- 主页：`https://github.com/你的用户名/项目名`
- 描述：`Managed by gh-cross-mirror: https://github.com/你的用户名/项目名`

若要接管以前建的仓库，先确认内容、可见性与源一致，且目标没有需要保留的独立开发；再在 GitCode 项目设置里手动填写上述两个字段，字符和大小写应与日志来源一致。这个操作表示允许脚本后续推送。不要给无关仓库添加标记。

## 同步方式和边界

- 默认包含 fork、原创、已归档及空仓库；只处理个人拥有的项目。星标、关注或参与的别人的项目不会被搬运。
- fork 的镜像源仍是 `你的账号/项目`。开启 `SYNC_FORKS` 后先同步上游默认分支，再备份你自己的 fork；不会直接拿上游覆盖 GitCode，也不会把上游其他分支或标签全部复制进你的 fork。
- 先分页读取两边清单，用 GitHub 仓库 ID 关联上次状态，对比名称、推送/更新时间、默认分支、可见性及目标来源信息。未变化且未到轮换时间的项目，跳过 Git 操作。
- 上次失败、还没处理或目标缺失的项目会再次进入候选列表；只有成功完成才更新成功指纹。失败尝试也记录尝试时间，使后面的待处理仓库下轮有机会先运行。
- 成功核对满 7 天的项目重新进入轮换列表，每 4 个变化项穿插 1 个轮换项，补查时间字段可能遗漏的分支/标签变化和目标代码差异。7 天是开始进入队列的间隔，大账号完成整批所需时间取决于队列、每轮上限和传输速度，不保证每个仓库都在 7 天内核对完。
- 对选中的项目用 `git ls-remote` 比较源与目标分支、标签。源引用都一致就跳过完整克隆；目标独有引用保留。
- 启用自动上游时，未归档 fork 即使自身时间戳没变，也会进入候选队列调用上游同步接口，否则发现不了作者的新提交。同样受每轮 200 个候选上限和轮换顺序约束。上游返回“已是最新”且本地成功状态未变时，跳过 GitCode/Git 复查；到期的轮换核对仍执行。上游确实更新时刷新元数据并强制进入本轮镜像处理，避免时间戳延迟导致漏同步。
- 有变化时在临时目录完整裸克隆，随后传输 LFS，再原子推送分支和标签；结束后删除临时目录。不跨轮缓存完整仓库，因此发生变化的仓库仍有完整下载成本。
- 不强推、不删除目标独有分支或标签；GitHub 上的 rebase、强推、标签移动可能导致非快进失败，需要人工处理。目标作为备份使用，不在两边同时开发。
- 如果 GitCode 不支持原子推送或分支保护阻止更新，任务会失败，不降级为强制或部分引用推送。需人工检查设置。
- 不迁移 Issues、PR、Release 附件、Wiki、Packages、权限、fork 关系等平台数据；子模块只保留引用，不递归备份子模块仓库。
- LFS 会单独传输；若 LFS 失败则不发布新引用。引用完全一致时不会重新校验历史 LFS 对象；接管旧镜像前应自行确认 LFS 完整。
- 新建仓库会传入源默认分支；仍应在首次验收时检查 GitCode 默认分支。源默认分支后续改名时，需要手动调整 GitCode 默认分支设置。
- 这是保留历史的备份同步，不是会强制删除所有差异的逐字节镜像。
- GitHub 仓库从当前列表消失时，可能是删除、改为私有、移出权限范围或被筛选，不直接判定为删除；保留旧状态和 GitCode 仓库，Summary 统计提示。源改名后不会自动删除旧名备份。
- 每轮尽量在 90 分钟内结束 Git 操作并保存进度，超出每轮处理数量的项目也留到下轮。如果 Actions 被手动取消或异常强杀，可能来不及保存，下一轮会重新核对未记下的项目，不把它们直接标记成功。
- 不依赖用户活动事件 API 作为唯一变更来源，也没有配置 webhook 接收服务。列表时间字段用于筛选，轮换引用检查负责兜底。

## 限流、配额与故障处理

普通 GitHub PAT 通常为 5000 API 请求/小时；内置 `GITHUB_TOKEN` 通常为每仓库 1000 次/小时。有 `GH_PAT` 时，来源清单、身份核验、fork 上游更新使用 PAT；控制仓库的状态读写始终使用内置令牌。没有 PAT 且关闭上游更新时，公开来源清单也使用内置令牌。列表每页 100 项；例如 200 个仓库，列表通常约 3 次请求（含结束分页），不是每个仓库调用一次 GitHub API。

每个选中的 fork 上游检查额外调用 1 次 GitHub POST；如果发生合并/快进，再额外 GET 1 次刷新本仓库元数据。POST 属于写操作，除了普通额度还受 GitHub 二级/内容生成限流影响；代码串行调用、每次间隔至少 1 秒，不自动重试写入，403/429 会停止本轮。控制台和摘要分别显示来源/上游与状态读写请求数，避免把不同令牌额度混淆。

GitCode 默认 API 限额为 400 次/分钟、4000 次/小时。脚本每个 API 请求至少间隔 1 秒。新版每轮用分页列表代替逐仓存在性查询；新建另加创建、核验请求，列表安全字段缺失、为空或存在核验差异的候选仓库额外查详情。确认成功后保存列表指纹供下次比较，避免仅因列表与详情格式不同而每轮重复补查。新建串行处理且每轮默认最多 10 个。API 请求额度与 Git 克隆、LFS 流量是不同限制，低于 API 限额不等于没有其他风控。

例如两边各 1 万个仓库、没有新建且列表字段完整时，清单通常各需约 101 次请求；GitCode 另有 1 次账号核验。GitHub 额外读取/保存状态通常约 2 次，初次初始化或状态文件较大时略多。上游 fork 的请求另外按上一段计算；轮换、变化、上游候选默认每轮最多 200 个。此估算不含失败重试和环境准备。

- 收到 `401/403/418/429`：立即停止本轮，日志保留 Retry-After/Reset 提示，不循环重试。先检查权限或配额；若是限流，至少等平台指定时间后再重跑。若指定等待时间超过 6 小时，先关闭 `ENABLE_SYNC`，到期再开启。
- GET 网络/服务器异常：最多 3 次尝试；建仓 POST 不自动重试，下一轮重新检查仓库是否已创建。
- 累计 3 个项目失败：保存进度并停止，避免对大量仓库重复执行错误操作。每次运行最多 120 分钟，每个 Git 命令最多 15 分钟，接近 90 分钟预算时缩短剩余 Git 命令等待时间并结束本轮。
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

测试使用模拟 API 和本地临时 Git 仓库，不调用真实平台、不创建线上项目。覆盖分页、fork、预览、限流停止、同名/可见性保护、分支与标签、非快进原子拒绝、LFS 内容传输、状态读写、增量筛选、失败重试、分批续跑、轮换核对和消失仓库保留。真实 GitCode 列表字段、GitHub 状态分支写入权限及存储额度仍需按首次正式运行验收。
