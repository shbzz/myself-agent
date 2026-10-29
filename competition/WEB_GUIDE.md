# Myself Agent 网页工作台

网页服务和全部模型调用、容器验收在 home 上运行。Windows 只负责浏览器访问和 SSH 转发。

## 使用

直接打开 https://agent.foxbh.top:52117 并使用专用账号登录。服务端账号文件位于 /home/dsh/.config/myself-agent/web-login.json，权限 600；当前 Windows 的登录说明由本机私有访问文件提供。

若需通过 SSH 维护，在 Windows PowerShell 保持此命令运行，再打开 http://127.0.0.1:8787：

```powershell
ssh -N -L 127.0.0.1:8787:127.0.0.1:8787 -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 home
```

服务由 home 的 myself-agent-web.service 管理，监听 127.0.0.1:8787。不会修改原有 SSH、反向隧道、路由、防火墙或其他应用。无需在 Windows 安装项目依赖。

1. 点击“新会话”，在输入框上方选择开发场景，也可点击中间的场景提示。
2. 输入开发需求，按 Enter 发送；Shift+Enter 换行。输入框上方的设置按钮可调整并发数和修复轮数。
3. 中间对话显示执行摘要；右上角“任务追踪”可展开或收起右侧栏。收起不会暂停任务。
4. 右侧“进度”查看阶段、依赖和执行记录；“修改”查看逐行差异、源码及不同修复版本；“验证”查看各轮独立测试和失败反馈。
5. 任务结束后，继续在同一对话输入修改要求。下一条需求会沿用上一轮已有代码，并重新验收。切换场景时新建会话。
6. 右下角可下载本轮代码与证据。左侧会话列表支持搜索和回看；会话保存在服务器，刷新不会重复触发模型。

每条发送的需求都会触发一次真实开发与验收，并产生模型用量。右侧百分比按规划、开发模块、独立验收这几个实际阶段的完成数量计算。

场景为有固定公共接口和独立验收的任务模板。补充偏好不会改变接口、模块边界或测试标准。当前不是任意软件项目生成器，也不是可拖拽编辑的通用工作流平台。

## 三个场景

| 场景 | 开发依赖 | 验收 |
| --- | --- | --- |
| 订单 CSV 工具 | parser.py 与 stats.py 独立，可并行 | 12 项 |
| 文本统计工具 | tokenize_text.py → metrics.py | 8 项 |
| 订单报告生成器 | parser.py、stats.py → report.py | 16 项 |

模型为每个模板输出模块任务描述和依赖，平台校验模块集合、依赖闭合及无环性，再与模板合同校验。调度器按依赖就绪状态派发，优先剩余依赖路径较长的任务，并遵守 1/2 的并发额度。当前是批次调度；没有抢占、自动负载预测或跨服务器调度。

失败用例映射到模块，并计算所有受影响的下游任务。修复模块按依赖顺序重新生成，不涉及的独立模块保留；每轮仍执行完整验收。失败与修复事件、每轮代码和哈希均保留。

## 配额与状态

- 同时最多一个网页作业，开发角色并发最多两个。
- 每个角色最多两次模型调用，修复最多三轮；网页默认两轮。
- 每个作业最长 600 秒，容器单次验收最长 20 秒。
- 重复请求编号复用已有作业；同一编号不同内容被拒绝。
- 作业记录在 /data/myself-agent/web/jobs.sqlite，产物在 /data/myself-agent/runs。
- 服务重启后，未完成任务标为“已中断”，不会自动重启计费。
- 不将模型密钥发给浏览器、容器或下载包。模型密钥仍在仓库外受限配置文件。

公网入口由阿里云 Apache 提供 HTTPS 和登录保护，通过 home 主动建立的专用隧道访问后端。当前是单个共享工作空间账号，作业仍遵守全局单任务额度；用户隔离与分组权限尚未实现。认证、隧道和证书维护细节保存在受限运维目录，不随公共应用源码分发。

## 运维与验证

home：

```bash
sudo -n systemctl status myself-agent-web --no-pager
sudo -n journalctl -u myself-agent-web -n 30 --no-pager
curl -fsS http://127.0.0.1:8787/health
cd /home/dsh/projects/myself-agent/competition
/home/dsh/projects/myself-agent-research/venv/bin/python -m ruff check .
/home/dsh/projects/myself-agent-research/venv/bin/python -m pytest -q
```

命令行仍可用，例如 `python -m myself_agent --scenario order-report --concurrency 2 --max-repairs 2`。不会因为网页上线而迁移旧运行或覆盖上一次产物。
