# Myself Agent 贡献与 Git 工作流

本说明适用于 `competition/` 应用。应用开发不得顺带修改上游 `libs/`。

## 分支与提交

1. 从最新 `main` 开始：`git switch main`，然后 `git pull --ff-only origin main`。
2. 每项工作创建独立分支：`git switch -c codex/<简短主题>`。
3. 提交标题使用 Conventional Commits，例如 `feat: add workspace switching`、`fix: preserve session selection`、`docs: clarify setup`。
4. 推送主题分支并向 `main` 发起 Pull Request；优先 Squash merge，合并后删除主题分支。
5. 不直接向 `main` 推送。home 工作树已配置本地 pre-push 钩子拦截。

## home 验证

构建、依赖安装、Ruff、pytest、模型请求和执行环境验证都在 home 完成。不要配置公开仓库可触发的 home 自托管 Runner。GitHub 托管的检查仅做差异空白与凭据卫生扫描，不执行应用代码。

在 home 仓库执行一次：

```bash
git config --local core.hooksPath .githooks
```

pre-commit 检查暂存区空白和常见凭据格式；pre-push 运行应用 Ruff、格式检查、完整 pytest 和静态资源 JavaScript 语法检查。pre-push 测试不调用真实模型 API。也可手动执行：

```bash
competition/scripts/check-home.sh
```

Pull Request 描述中记录验证命令、结果和任何告警。界面改动附桌面与移动端浏览器证据。涉及真实模型调用时，单独记录调用预算与实际用量。

## 不提交的内容

不得提交凭据、`.env`、数据库、运行产物、日志、个人维护脚本、home 专用部署目录或真实用户数据。持久运行数据与备份放在 Git 工作树之外。每次提交前检查暂存文件清单；自动扫描只是补充，不替代人工检查。不要把应用凭据或敏感运行记录写进提交、PR 或截图。

## Handoff

See `competition/DEVELOPMENT_STATUS.md` for the current baseline and next task. Local hooks do not replace GitHub branch protection. Merging does not deploy production.
