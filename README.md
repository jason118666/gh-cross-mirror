# GitHub → GitCode 自动备份

通过 GitHub Actions 每 6 小时扫描个人仓库（包含 fork），在 GitCode 自动创建同名仓库并同步分支、标签和 LFS 对象。fork 会先同步上游默认分支，再备份到 GitCode。

使用 Actions 主动推送，不开启 GitCode 原生 Pull 镜像。整个账号只需部署这一份工作流，电脑无需开机。

## 1. 配置

把项目推送到自己的 GitHub 控制仓库默认分支，然后进入 **Settings → Secrets and variables → Actions**。

### Secrets：保存令牌

| 名称 | 内容 |
| --- | --- |
| `GC_TOKEN` | GitCode 个人令牌，需读取个人资料、查询/创建仓库及读写仓库权限 |
| `GH_PAT` | GitHub 细粒度个人令牌，用于更新 fork 上游及读取授权范围内的仓库 |

[创建 GitHub 令牌](https://github.com/settings/personal-access-tokens/new)：

1. **Resource owner** 选自己的账号。
2. **Repository access** 选 **All repositories**，覆盖未来新增仓库；也可只选指定仓库，之后自行补充授权。
3. **Repositories** 权限：`Contents: Read and write`；`Metadata: Read-only` 自动附带。需要同步工作流文件时，添加 `Workflows: Read and write`。
4. 生成后保存为仓库 Secret **`GH_PAT`**。令牌不要写进代码、Variables 或聊天；到期前更换。

### Variables：设置目标和开关

| 名称 | 值 |
| --- | --- |
| `GC_NAMESPACE` | GitCode 个人主页 URL 中的用户名路径，不是昵称 |
| `ENABLE_SYNC` | 首次测试成功后设为小写 `true`，开启定时同步 |

旧配置将 `GC_NAMESPACE` 放在 Secrets 中也兼容。不更新 fork 上游时，可设置 `SYNC_FORKS=false`；仅备份公开仓库现有代码就无需 `GH_PAT`。

## 2. 首次运行

1. 打开 **Actions → Mirror to GitCode → Run workflow**，选择 `main`（或自己的默认分支）。
2. 保持“仅预览”勾选，运行后核对 **Summary → mirror 摘要**。预览只检查，不同步代码，也不保存进度。
3. 再次 **Run workflow**，取消“仅预览”，执行同步。
4. 到 GitCode 检查代码、默认分支及可见性；成功后设置 `ENABLE_SYNC=true`。

默认每轮最多**新建 10 个**、处理 **200 个候选仓库**。更新已有仓库不占新建名额。剩余项目留待下一轮，也可手动再次运行，不必等 6 小时。

## 3. 自动运行与结果

- 当前北京时间约 **02:17、08:17、14:17、20:17** 各运行一轮，可能延迟；不是从开启时开始倒计时。
- 暂停定时：将 Variables 中 `ENABLE_SYNC` 改为 `false`，手动运行仍可用。完全停用可在 Actions 页面选择 **Disable workflow**；已运行任务需另行取消。
- 运行列表是同一工作流的历史记录，无需删除。绿色表示本轮成功，仍需看摘要是否有待续；红色查看 **mirror → Scan and sync repositories** 日志。
- 摘要分别显示“上游”和“GitCode”结果。上游已更新而 GitCode 失败，会标为失败，下轮重试。
- 使用新版代码时，新建 **Run workflow**；旧记录的 **Re-run jobs** 仍使用旧提交。
- 可在 [GitHub 通知设置](https://github.com/settings/notifications) 开启 Actions 失败邮件通知。

频率写在 `.github/workflows/mirror-to-gitcode.yml`：

```yaml
schedule:
  - cron: "17 */6 * * *" # 每 6 小时；改为 "17 * * * *" 即每小时
```

## 可选配置

以下均添加到 **Variables**：

| 名称 | 默认值 | 用途 |
| --- | --- | --- |
| `GH_OWNER` | 控制仓库所属账号 | GitHub 个人用户名，不处理组织仓库 |
| `SYNC_FORKS` | `true` | 更新 fork 上游默认分支，需要 `GH_PAT` 写权限 |
| `INCLUDE_PRIVATE` | `false` | 纳入私有仓库，`GH_PAT` 必须覆盖这些仓库 |
| `REPO_ALLOWLIST` | 空 | 仅处理指定项目名，英文逗号分隔 |
| `REPO_EXCLUDE` | 空 | 排除指定项目，优先于白名单 |
| `MAX_NEW_REPOS` | `10` | 每轮新建上限，范围 1～100 |
| `MAX_SYNC_REPOS` | `200` | 每轮候选处理上限，范围 1～1000 |

## 同步范围与注意事项

- 两边分页读取仓库清单，成功状态保存在控制仓库的独立 `mirror-state` 分支；不要修改或合并该分支。无变化的仓库跳过处理，fork 仍需检查上游；另有分批轮换核对。变化仓库可能需要完整克隆。
- fork 只更新上游默认分支，冲突时保留现有代码并报错。向 GitCode 推送默认不强推；工作流中的 `SOURCE_WINS_REPOS` 已为 `rea` 启用以 GitHub 为准，使用 `--force-with-lease` 校验目标旧提交后覆盖同名分支和标签，目标中途变化则拒绝整批更新。所有仓库都保留目标独有引用。目标仓库应作为备份使用。
- 已有同名仓库不会直接接管。确认来源、内容及可见性后，在 GitCode 手动设置主页为 `https://github.com/用户名/项目名`、描述为 `Managed by gh-cross-mirror: https://github.com/用户名/项目名`，表示允许同步。
- 不迁移 Issues、PR、Release 附件、Wiki、权限或 fork 关系；子模块只保留引用。默认分支后续改名需手动调整 GitCode 设置。
- 源仓库删除、改名或失去访问权限时，不自动删除 GitCode 备份。**源改为私有后，需立即手动将 GitCode 目标设为私有**，脚本不会撤回已公开内容。
- 私有项目应使用私有控制仓库，避免日志、摘要和状态分支公开项目名称；同时留意私有仓库 Actions 分钟额度。
- 请求限流或权限错误时停止本轮；先看日志再重跑。API 额度与 Git/LFS 流量、存储额度分别计算，不承诺零风控风险。fork 请保留许可证和出处。
- GitHub 公开仓库连续 60 天没有活动可能停用定时任务，请定期检查最近运行结果。

参考：[GitHub 限流](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api) · [GitHub 定时任务](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule) · [GitCode API](https://docs.gitcode.com/docs/apis/)

## 本地验证

需要 Python 3.11+、Git 和 Git LFS。离线测试不操作线上仓库：

```sh
python -m unittest discover -s tests -v
```
