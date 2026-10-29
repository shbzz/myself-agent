# myself-agent 对话式开发工作空间

在 home 服务器上运行的网页应用，同时保留命令行入口：DeepSeek 官方 `deepseek-flash` 提供推理，Deep Agents 执行角色，LangGraph 管理依赖调度、并行、验证和修复。FastAPI 提供接口和静态网页。框架 `libs/` 源码保持原样。

支持三个固定合同的软件场景：订单 CSV（两个独立模块，12 项验收）、文本统计（两个串行依赖模块，8 项验收）、订单报告（两个并行模块汇合到报告模块，16 项验收）。平台校验任务依赖，按并发额度派发；验收失败后，修复失败模块与受影响的下游，再全量验收。

网页采用左侧会话列表、中间对话、右侧可收起任务追踪的布局。通过对话提交需求和继续修改，后续消息自动沿用上一轮代码。右侧展示进度、依赖图、真实逐行差异、修复版本和独立验收，支持 ZIP 下载。主界面源文件为 static/chat.html、chat.css 和 chat.js；原静态文件用于旧页面的缓存兼容。

## 网页访问

生产入口：https://agent.foxbh.top:52117，使用专用网页账号登录。模型调用和容器验收仍在 home，阿里云提供 HTTPS 与认证入口；访问无需保持 Windows 的 SSH 转发。服务器认证与部署配置保存在受限运维目录，不随公共应用源码分发。

home 已启动 `myself-agent-web.service`，监听 `127.0.0.1:8787`。也可通过 SSH 转发维护：

```powershell
ssh -N -L 127.0.0.1:8787:127.0.0.1:8787 -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 home
```

浏览器打开 http://127.0.0.1:8787。所有模型调用、构建与测试仍在 home，Windows 无需安装开发依赖。[完整网页指南](WEB_GUIDE.md) 包含场景、配额、状态和运维方式。

## 当前 home 启动方式

```bash
cd /home/dsh/projects/myself-agent/competition
/home/dsh/projects/myself-agent-research/venv/bin/python -m myself_agent
# 或：python -m myself_agent --scenario order-report --concurrency 2 --max-repairs 2
```

每次运行都会调用真实计费 API，并在 `/data/myself-agent/runs/<run_id>/` 保存结果。

`--max-repairs 0` 只运行首次生成和验收；默认最多修复三轮。`--inject-failure` 会明确标记并替换第零轮的统计模块，用于演示故障恢复；不能将这类运行当成自然错误率的评测数据。

API 配置位于服务器仓库外的受限文件 `/home/dsh/.config/myself-agent/deepseek.env`，目录权限 700、文件权限 600，包含 `DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL`、`DEEPSEEK_MODEL`。配置固定官方地址与用户指定模型，密钥不进入图状态、产物或容器。

## 新环境安装

需要 Python 3.11+、Docker、具有项目容器管理权限的操作用户，以及官方 DeepSeek API 配置。在当前 home，外网下载前必须按服务器规则确认现有代理的服务、监听和出口；依赖下载经现有代理进行。示例安装命令从 `competition/` 目录执行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --proxy http://127.0.0.1:7890 \
  ../libs/checkpoint ../libs/sdk-py ../libs/prebuilt \
  ../libs/langgraph ../libs/checkpoint-sqlite -e '.[dev]'
```

固定的测试镜像为 `python@sha256:673719e071ecf27434fd2635bbd8d00f49f4fdd81f243143438a42ced8bedf60`，须在 Docker 中可用。运行时使用 `--pull=never`，不会自动下载或替换镜像。home 已通过现有镜像下载配置获取此镜像；不需要修改 Docker daemon。

## 数据与证据

每次运行目录包含：

- `workflow.mmd`：外层引擎图。
- `plan.json`：校验后的模块任务与依赖图。
- `baseline.json`：后续需求所延续的上一轮代码，仅在存在基础代码时生成。
- `events.jsonl`：角色开始/结束、用量、验证结果、修复选择。
- `planner-round-0.json` 和开发角色响应：模型返回的结构化输出。
- `checkpoints.sqlite`：外层运行状态快照。
- `round-N/workspace/`：该轮实际受测代码。
- `round-N/artifact-hashes.json`：受测文件的内容摘要。
- `round-N/verification.json`：容器退出状态、验收报告与输出。
- `summary.json`：最终状态、修复轮数、模型用量、镜像与运行位置。
- `error.json`：平台异常类型；不会输出凭据、HTTP Header 或任意 API 错误正文。

每次 CLI 对应一个运行，运行内开发并发可选 1/2。网页通过 conversation_id 和 parent_run_id 将多轮需求连接到同一会话，校验修改基础并保存历史。网页限制同时一个作业，提供重复请求去重、600 秒总时限和服务重启后的中断标记。CLI 单独运行不受网页队列约束。能力评分和持久 Agent 注册表尚未实现；检查点已经保存，但自动恢复、人工审批与完整文件回滚仍在后续范围中。

## 执行边界

模型只能返回白名单中的 Python 模块名；写文件和 Docker 操作由可信平台完成。Deep Agents 的默认文件后端使用内存状态，平台通过模型调用中间件移除 `task` 工具，由外层图负责角色派发。

验收容器使用无网络、只读根文件系统、只读代码与测试挂载、UID/GID 65534、删除 capabilities、禁止提权、512 MiB 内存、0.5 CPU 和 64 个进程上限。它不挂载密钥或 Docker socket；宿主进程对运行时间与输出量设置上限，并清理本次创建的容器。

固定验收脚本位于 `src/myself_agent/acceptance/`。模型输出不作为验收结论；平台要求正确退出状态及完整的验收报告。容器共享宿主内核，应用面向受控任务；验收报告检查不构成对主动伪造结果代码的形式化证明。

外层图同步持久化检查点，内层无持久检查点的 Agent 调用显式使用异步 durability，避免继承同步检查点屏障。当前底座可能为这次显式设置发出“不存在检查点”的提示；对应回归测试验证了外层快照和内层结构化输出均正常。

## 检查

```bash
PYTHON=/home/dsh/projects/myself-agent-research/venv/bin/python
$PYTHON -m ruff check .
$PYTHON -m ruff format --check .
$PYTHON -m pytest -q
```

测试包括配置权限、模块与依赖校验、实际容器隔离、错误和缺失报告拒绝、超时与输出量限制、串行与并行场景、下游修复、网页请求去重、配额、来源校验、下载和中断状态，以及真实 Deep Agents 在外层同步检查点中的离线回归。自动化测试不调用模型 API；带 `docker` 标记的测试需要 Docker 和已下载的固定镜像。

## 当前范围

已接通网页、真实模型、多角色、依赖调度、受限容器验收、反馈修复与证据记录，并提供带登录保护的 HTTPS 公网入口。当前依赖图受场景模板合同约束；通用工作流编辑、Agent 注册与版本管理、知识库、能力/负载评分策略、人工审核和多人权限管理仍需后续开发。

应用使用上游 MIT 项目，保留原仓库版权与许可证。自研部分是本目录的任务协议、编排应用、容器执行器、验收与修复路由；第三方运行时和 Agent harness 不计作自研。
