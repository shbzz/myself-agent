# 受约束规划与明确批准（后端）

该增量复用已有编排 DSL、调度器和独立验证机制，不另造执行引擎，不改前端。
输入用户需求、团队及明确的验收命令，选择一个已注册 planner，生成待确认的工作流。
**生成计划不等于执行任务，更不等于完成验收。**

## 状态与边界

1. POST /next/api/orchestration/plans 创建规划运行，返回202及run id。
2. 独立监督进程最多调用模型一次，不提供任何工具、不执行生成的代码、不读工作区文件内容、不修改工作区或外部系统；只接收有界文件路径清单、用户需求、团队能力、工具启用状态及 DSL schema。
3. 模型只能生成已有 DSL；禁止引入未声明能力或新的验收命令。平台在全部顶层产出之后强制追加 acceptance_gate，执行用户给出的全部验收命令。模型的中间verify只能引用这些命令的原文子集。
4. 工作流、请求、团队/模型/策略快照和工作区指纹绑定成摘要。成功生成后仅进入待批准状态，**不会自动提交执行**。
5. 用户通过 approve API 提交所查看的准确摘要和新的幂等请求ID。团队、模型、策略或工作区变更会拒绝批准，必须重新规划。通过后创建一个正常编排运行，复用现有引擎。
6. 执行工作进程在首次模型调用前再次检查工作区指纹；批准后的外部文件变更也会拒绝执行。验收条件不允许模型自行修改。

计划生成与执行分别是两个可审计的运行，各自受冻结策略约束；规划阶段额外收紧到最多1次模型调用，无自动重试。执行预算与普通编排一致，不宣称两个运行共享一个计数器。
计划生成的schema/文本超出上下文预算直接失败；截断、工具调用、非JSON或无效DSL均不产生可批准计划。
仅生成计划时不读取普通对话图片/完整检查点历史，也不会把供应商凭据、私有端点或凭据引用注入模型消息。

## 请求示例

先注册一个planner、至少一个执行角色和一个具备test技能的独立verifier，并创建team/session。

```json
{
  "session_id": "现有会话的32位十六进制ID",
  "request_id": "本次规划的新32位十六进制ID",
  "team_id": "example-team",
  "planner_id": "example-planner",
  "verification_skills": ["test"],
  "text": "在已导入的项目中实现用户明确提出的修改，遵守现有接口",
  "acceptance_checks": ["用户控制的冻结验收命令"],
  "max_steps": 48,
  "reasoning_effort": "off"
}
```

- GET /plans?session_id=...：规划记录。
- GET /plans/{id}：待确认工作流、用户验收、团队/Agent定义、模型配置、策略版本、工作区摘要、实际用量、摘要及关联执行ID；不返回凭据。
- POST /plans/{id}/approve：`{"request_id":"新32位ID","digest":"查看到的64位摘要"}`。此操作会实际启动编排模型调用。相同编号重试返回原执行；同一计划不能用新编号再执行一次。
- POST /plans/{id}/cancel：取消尚未完成的规划；迟到响应不能写入草稿。已批准的执行需要使用其独立运行ID取消。

上面简写路径以 /next/api/orchestration 为前缀，沿用JSON、同源和现有登录边界。
规划生成、批准与执行记录使用同一SQLite事务衔接，失败不能留下孤立运行或已消耗的批准。
生成的工作流不自动污染共享工作流注册表；完整定义保存在执行快照。

## 当前限制与验收解释

- 这是计划确认，不是运行中任意节点的人工暂停/继续，也不代表跨运行自动恢复、知识库或长期记忆已经完成。
- 不能从自然语言自动推导一个绝对可信的验收标准。用户提供的命令质量决定验收质量；优先固定内联断言/外部受控基准。仅运行作者可修改的测试文件仍存在测试被修改的风险。
- 不宣称规划模型能够总是生成正确计划；无效计划安全失败，用户可修改要求后发起新请求。
- 为平台最终验证预留节点和步数；编译后的图仍须满足原有16节点/Flow、48总定义节点、3层嵌套和运行步数上限。
- 文件清单最多100项并显式标记截断，指纹沿用既有1000项/单文件16MiB/总32MiB/10秒边界。
- 前端、知识引用、运行中人工介入、安全恢复和对照评测继续作为后续工作。

## 验证记录

home 完整检查通过：195项pytest（含23项规划专项）、Ruff/格式、JS语法和TypeScript/Vite；前端源码和资源哈希未变，保留既有LangGraph durability警告。

专项离线测试覆盖待确认状态、幂等/事务回滚、审批摘要、团队/策略/工作区变更、
迟到响应、无效/截断/越权规划、重启后确认、凭据不回显和全局运行租约。
真实 Docker 场景分别使用正确和错误产物，验证冻结命令可以否决模型自报成功。

两次独立的有预算真实试验均使用官方 DeepSeek Flash / thinking off，数据与生产隔离，
每次限规划1调用、执行8调用，各阶段120秒，无自动重试。
第一次成功生成并批准计划，但执行者额外自测导致预算用尽，最终验证未完成；
该失败用量19575 tokens（缓存13952）保留，不计成功。
第二次收窄执行者工具范围并采用简短交接配置，在相同调用上限下成功：
规划1次、执行6次，14.641秒，10976 tokens（缓存5504）。
验证了规划不执行、明确摘要批准、独立冻结验收通过。两次记录不是成功率或提效结论。
主机证据及付费运行数据留在Git之外，正式部署不复制夹具和测试团队。


### Knowledge and failed-call accounting

Agents may explicitly reference workspace knowledge and approved experience; their
frozen revisions enter the plan digest. The planner receives only bounded passages
within its own grant. Changed referenced revisions invalidate a not-yet-executed
approval; execution retains the approved snapshot. See KNOWLEDGE.md.
Observed model-call/provider usage is now recorded before proposal validation, also
on malformed, truncated or late-cancelled responses. Provider usage availability is
explicit; missing provider accounting is not zero-cost evidence. Schema-error events
record error types only, not model-generated field names, values or credentials.
