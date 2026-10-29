"use strict";

const $ = (id) => document.getElementById(id);
const state = {scenarios: [], runs: [], selected: null, detail: null, submitting: false,
  timer: null, pending: null, graphKey: "", evidenceKey: "", testsKey: "", codeKey: ""};
const active = (status) => ["queued", "running"].includes(status);
const labels = {queued: "等待启动", running: "运行中", passed: "验收通过", failed: "验收未通过",
  error: "运行异常", interrupted: "已中断", idle: "待运行"};
const taskNames = {"parser.py": "订单解析 Agent", "stats.py": "统计开发 Agent",
  "tokenize_text.py": "分词开发 Agent", "metrics.py": "指标开发 Agent", "report.py": "报告开发 Agent"};

function el(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}
function titleFor(id) { return state.scenarios.find((s) => s.id === id)?.title || id; }
function currentScenario() { return state.scenarios.find((s) => s.id === $("scenario-select").value); }
function formatTime(value, date = false) {
  return new Intl.DateTimeFormat("zh-CN", {timeZone: "Asia/Shanghai", hour12: false,
    ...(date ? {month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit"}
      : {hour: "2-digit", minute: "2-digit", second: "2-digit"})}).format(new Date(value));
}
function badge(status) { return el("span", `status-badge ${status}`, labels[status] || status); }
function empty(container, title, message) {
  const box = el("div", "empty-state");
  box.append(el("span", "", "⌁"), el("strong", "", title), el("p", "", message));
  container.replaceChildren(box);
}
async function api(path, options = {}) {
  const response = await fetch(path, {cache: "no-store", ...options});
  const body = await response.json();
  if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : "请求参数无效，请检查后重试");
  return body;
}
function connection(ok, message = "无法连接 home 服务，请检查 SSH 转发是否仍在运行。") {
  $("connection-error").hidden = ok;
  $("connection-message").textContent = message;
  $("server-state").textContent = ok ? "服务器已连接" : "服务器连接中断";
  $("server-dot").className = `connection-dot ${ok ? "connected" : "disconnected"}`;
}
function updateSubmit() {
  const busy = state.runs.some((r) => active(r.status));
  $("submit-run").disabled = state.submitting || busy || !state.scenarios.length;
  $("submit-label").textContent = state.submitting ? "正在创建任务…" : busy ? "已有任务正在运行" : "开始真实运行";
}
function scenarioChanged() {
  const scenario = currentScenario();
  if (!scenario) return;
  $("scenario-description").textContent = scenario.description;
  $("contract-text").textContent = scenario.requirement;
  const dependencies = scenario.tasks.filter((task) => task.depends_on.length);
  $("scenario-topology").textContent = `${scenario.tasks.length} 个开发任务 · ${scenario.tests} 项独立验收 · `
    + (dependencies.length ? "包含前置依赖" : "独立任务可并行");
  if (!state.detail) render();
}
function view(name) {
  const history = name === "history";
  $("workbench-view").hidden = history;
  $("history-view").hidden = !history;
  $("nav-workbench").classList.toggle("active", !history);
  $("nav-history").classList.toggle("active", history);
  $("page-name").textContent = history ? "运行记录" : "开发工作台";
  $("page-title").textContent = history ? "每一次协作，都有迹可循。" : "把需求，交给协作的 Agent。";
  $("page-description").textContent = history ? "回看运行结果、生成代码与独立验收，保留可复核的开发证据。"
    : "从任务拆解到独立验收，每一步都有依据，每一次修复都有记录。";
  if (!history) requestAnimationFrame(drawEdges);
}
function renderHistory() {
  $("history-count").textContent = String(state.runs.length);
  const recent = $("recent-runs");
  recent.replaceChildren();
  state.runs.slice(0, 5).forEach((run) => {
    const button = el("button", `recent-run ${run.run_id === state.selected ? "selected" : ""}`);
    button.type = "button";
    const text = el("div");
    text.append(el("strong", "", titleFor(run.scenario)),
      el("small", "", `${run.run_id.slice(0, 8)} · ${labels[run.status]}`));
    button.append(el("span", `recent-dot ${run.status}`), text);
    button.addEventListener("click", () => selectRun(run.run_id));
    recent.append(button);
  });
  if (!state.runs.length) recent.append(el("p", "sidebar-empty", "暂无运行记录"));
  const history = $("history-list");
  history.replaceChildren();
  state.runs.forEach((run) => {
    const row = el("div", "history-row");
    row.tabIndex = 0;
    row.setAttribute("role", "button");
    row.setAttribute("aria-label", `查看${titleFor(run.scenario)}，${labels[run.status]}`);
    const text = el("div");
    text.append(el("strong", "", titleFor(run.scenario)),
      el("p", "", `${run.run_id.slice(0, 12)} · 并发 ${run.concurrency} · 最多修复 ${run.max_repairs} 轮`));
    row.append(text, badge(run.status), el("small", "", formatTime(run.created, true)), el("span", "history-arrow", "↗"));
    row.addEventListener("click", () => selectRun(run.run_id));
    row.addEventListener("keydown", (event) => { if (event.key === "Enter") selectRun(run.run_id); });
    history.append(row);
  });
  if (!state.runs.length) empty(history, "还没有运行记录", "创建第一个任务，开始一次可验证的 Agent 协作。");
  updateSubmit();
}
async function selectRun(id) {
  state.selected = id;
  localStorage.setItem("myself-agent-run", id);
  state.graphKey = state.evidenceKey = state.testsKey = state.codeKey = "";
  view("workbench");
  try {
    const detail = await api(`/api/runs/${id}`);
    if (state.selected !== id) return;
    state.detail = detail;
    $("scenario-select").value = detail.job.scenario;
    $("brief").value = detail.job.brief;
    $("brief-count").textContent = `${detail.job.brief.length} / 1500`;
    $("concurrency").value = String(detail.job.concurrency);
    $("max-repairs").value = String(detail.job.max_repairs);
    scenarioChanged(); render(); renderHistory(); connection(true);
  } catch (error) { connection(false, error.message); }
}
function newRun() {
  state.selected = null;
  state.detail = null;
  state.pending = null;
  localStorage.removeItem("myself-agent-run");
  state.graphKey = state.evidenceKey = state.testsKey = state.codeKey = "";
  $("brief").value = "";
  $("brief-count").textContent = "0 / 1500";
  $("concurrency").value = "2";
  $("max-repairs").value = "2";
  $("form-message").textContent = "";
  $("tab-events").click();
  view("workbench"); scenarioChanged(); render(); renderHistory();
  $("scenario-select").focus();
}
function taskStates(tasks, events) {
  const statuses = Object.fromEntries(tasks.map((task) => [task.filename, "waiting"]));
  for (const event of events) {
    if (event.kind === "repair_scheduled") event.targets.forEach((name) => { statuses[name] = "waiting"; });
    const name = event.role?.replace(/_developer$/, ".py");
    if (name in statuses && ["agent_started", "agent_completed", "agent_failed"].includes(event.kind)) {
      statuses[name] = {agent_started: "running", agent_completed: "done", agent_failed: "failed"}[event.kind];
    }
  }
  if (state.detail && !active(state.detail.job.status)) {
    Object.keys(statuses).forEach((name) => { if (statuses[name] === "running") statuses[name] = "failed"; });
  }
  return statuses;
}
function renderGraph() {
  const detail = state.detail;
  const scenario = detail?.scenario || currentScenario();
  if (!scenario) return;
  const tasks = detail?.plan?.tasks || scenario.tasks;
  const events = detail?.events || [];
  const statuses = taskStates(tasks, events);
  const status = detail?.job.status || "idle";
  const key = JSON.stringify([tasks, statuses, status]);
  $("run-status").className = `status-badge ${status}`;
  $("run-status").textContent = labels[status];
  $("graph-mode").textContent = detail ? "真实运行记录" : "模板预览";
  $("run-id-label").textContent = detail ? detail.job.run_id.slice(0, 12) : "尚未创建运行";
  const plannerEvents = events.filter((e) => e.role === "planner");
  const planner = plannerEvents.at(-1);
  const plannerStatus = planner?.kind === "agent_completed" ? "done" : planner?.kind === "agent_started"
    ? active(status) ? "running" : "failed" : planner?.kind === "agent_failed" ? "failed" : "waiting";
  $("planner-status").textContent = {waiting: "待开始", running: "规划中", done: "已完成", failed: "异常"}[plannerStatus];
  $("planner-status").className = `node-status ${plannerStatus}`;
  const verification = events.filter((e) => e.kind.startsWith("verification_")).at(-1);
  const vStatus = verification?.kind === "verification_started" ? active(status) ? "running" : "failed"
    : verification?.passed ? "done" : verification ? "failed" : "waiting";
  $("verifier-status").textContent = {waiting: "待开始", running: "验证中", done: "已通过", failed: "未通过"}[vStatus];
  $("verifier-status").className = `node-status ${vStatus}`;
  $("verifier-description").textContent = `${scenario.tests} 项独立验收 · 受限容器执行`;
  const repair = events.filter((e) => e.kind === "repair_scheduled").at(-1);
  $("repair-description").textContent = repair ? `第 ${repair.repair_round} 轮修复：${repair.targets.join("、")}；修复后全量验收`
    : "验收失败 → 定位模块 → 更新受影响的依赖 → 再次验收";
  if (key === state.graphKey) return;
  state.graphKey = key;
  const columns = $("dag-columns");
  columns.replaceChildren();
  const depths = {};
  function depth(task) {
    if (depths[task.filename] !== undefined) return depths[task.filename];
    const value = task.depends_on.length ? 1 + Math.max(...task.depends_on.map((name) => depth(tasks.find((t) => t.filename === name)))) : 0;
    depths[task.filename] = value;
    return value;
  }
  const max = Math.max(...tasks.map(depth));
  for (let level = 0; level <= max; level++) {
    const column = el("div", "dag-column");
    tasks.filter((t) => depths[t.filename] === level).forEach((task) => {
      const node = el("div", `task-node ${statuses[task.filename]}`);
      node.dataset.filename = task.filename;
      const title = el("div", "task-title");
      title.append(el("span", "task-symbol", "⌘"), el("strong", "", task.filename));
      node.append(el("i", "port in"), title, el("div", "task-subtitle", taskNames[task.filename]),
        el("span", "task-state", {waiting: "等待", running: "执行中", done: "已完成", failed: "异常"}[statuses[task.filename]]),
        el("i", "port out"));
      column.append(node);
    });
    columns.append(column);
  }
  requestAnimationFrame(drawEdges);
}
function drawEdges() {
  const svg = $("dag-edges");
  svg.replaceChildren();
  if ($("workbench-view").hidden) return;
  const scenario = state.detail?.scenario || currentScenario();
  const tasks = state.detail?.plan?.tasks || scenario?.tasks || [];
  const base = $("dag").getBoundingClientRect();
  const nodes = new Map([...$("dag-columns").querySelectorAll(".task-node")].map((node) => [node.dataset.filename, node]));
  tasks.forEach((task) => task.depends_on.forEach((dependency) => {
    const from = nodes.get(dependency)?.getBoundingClientRect();
    const to = nodes.get(task.filename)?.getBoundingClientRect();
    if (!from || !to) return;
    const x1 = from.right - base.left, y1 = from.top + from.height / 2 - base.top;
    const x2 = to.left - base.left, y2 = to.top + to.height / 2 - base.top;
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", `M ${x1} ${y1} C ${x1 + 20} ${y1}, ${x2 - 20} ${y2}, ${x2} ${y2}`);
    svg.append(path);
  }));
}
function eventCopy(event) {
  const file = event.role?.replace(/_developer$/, ".py");
  const role = event.role === "planner" ? "规划 Agent" : file;
  const count = event.report?.tests_run || 0;
  switch (event.kind) {
    case "run_started": return ["运行开始", `${titleFor(event.scenario)} · 开发并发上限 ${event.concurrency}`, ""];
    case "agent_started": return [`${role} 开始执行`, event.repair_round ? `第 ${event.repair_round} 轮修复` : "首次开发", ""];
    case "agent_completed": return [`${role} 已完成`, "结构化输出已保存", event.elapsed_seconds ? `${event.elapsed_seconds}s` : ""];
    case "agent_failed": return [`${role} 执行异常`, event.error_type, ""];
    case "plan_created": return ["任务计划已校验", `${event.tasks.length} 个模块，依赖关系有效`, ""];
    case "batch_scheduled": return [`派发任务：${event.tasks.join("、")}`, event.reason, `并发 ${event.concurrency}`];
    case "verification_started": return ["独立验收开始", `第 ${event.repair_round} 轮 · 受限 Docker 容器`, ""];
    case "verification_completed": return [event.passed ? `${count} 项验收全部通过` : "独立验收未通过",
      event.passed ? "测试结果由平台验收脚本生成" : `${event.report?.failed_cases?.length || 0} 条失败记录 · ${event.failure_kind || "acceptance"}`,
      event.elapsed_seconds ? `${event.elapsed_seconds}s` : ""];
    case "repair_scheduled": return [`安排第 ${event.repair_round} 轮修复：${event.targets.join("、")}`, event.reason || "按失败用例定位模块", ""];
    case "controlled_fault_injected": return ["已注入受控故障", `${event.module} · 此运行用于修复验证`, ""];
    case "run_completed": return ["运行结束", labels[event.outcome], ""];
    default: return [event.kind, "", ""];
  }
}
function renderEvidence() {
  const detail = state.detail;
  const events = detail?.events || [];
  $("event-count").textContent = String(events.length);
  const key = `${state.selected}:${events.length}:${detail?.job.status}`;
  if (key !== state.evidenceKey) {
    state.evidenceKey = key;
    const list = $("events-list");
    const wasBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 70;
    list.replaceChildren();
    events.forEach((event) => {
      const [title, copy, extra] = eventCopy(event);
      const good = event.kind === "agent_completed" || event.passed;
      const bad = event.kind === "agent_failed" || event.kind === "verification_completed" && !event.passed;
      const row = el("div", "event-row"), text = el("div", "event-copy");
      text.append(el("strong", "", title));
      if (copy) text.append(el("p", "", copy));
      row.append(el("span", "event-time", formatTime(event.time)),
        el("span", `event-mark ${good ? "good" : bad ? "bad" : ""}`, good ? "✓" : bad ? "!" : "·"),
        text, el("span", "event-extra", extra));
      list.append(row);
    });
    if (!events.length) empty(list, "执行过程会在这里展开", "创建任务后，可实时查看 Agent 协作、调度与测试记录。");
    if (detail?.job.error_type) {
      const row = el("div", "event-row");
      row.append(el("span", "event-time", formatTime(detail.job.updated)), el("span", "event-mark bad", "!"),
        el("div", "event-copy", `任务${labels[detail.job.status]}：${detail.job.error_type}。历史证据已保留。`));
      list.append(row);
    }
    if (wasBottom) list.scrollTop = list.scrollHeight;
  }
  const rounds = detail?.rounds || [];
  const testsKey = JSON.stringify([state.selected, rounds]);
  if (testsKey !== state.testsKey) {
    state.testsKey = testsKey;
    const list = $("tests-list");
    list.replaceChildren();
    rounds.forEach((round) => {
      const card = el("article", "test-round"), heading = el("div", "test-round-header");
      heading.append(el("strong", "", round.round ? `修复后验收 · 第 ${round.round} 轮` : "首次独立验收"),
        badge(round.passed ? "passed" : "failed"), el("small", "", `${round.elapsed_seconds}s`));
      const report = round.report;
      card.append(heading, el("p", "", report
        ? `已执行 ${report.tests_run} 项用例 · ${report.failures} 条断言失败 · ${report.errors} 条错误记录`
        : `未获得完整验收报告 · ${round.failure_kind}`));
      if (report?.failed_cases?.length || !report) {
        const details = el("details"), summary = el("summary", "", "展开失败原因与测试反馈");
        details.append(summary, el("pre", "", report?.failed_cases?.map((c) => `${c.test}\n${c.traceback}`).join("\n\n") || round.output));
        card.append(details);
      }
      const repair = events.find((event) => event.kind === "repair_scheduled" && event.repair_round === round.round + 1);
      if (repair) card.append(el("p", "", `↻ 下一轮修复：${repair.targets.join("、")}`));
      list.append(card);
    });
    if (!rounds.length) empty(list, "等待独立验收", "模块生成完毕后，将执行场景对应的固定测试用例。");
  }
  const artifacts = detail?.artifacts || {};
  const codeKey = JSON.stringify([state.selected, artifacts]);
  if (codeKey !== state.codeKey) {
    state.codeKey = codeKey;
    const selected = $("source-select").value;
    $("source-select").replaceChildren(...Object.keys(artifacts).map((name) => {
      const option = el("option", "", name); option.value = name; return option;
    }));
    if (selected in artifacts) $("source-select").value = selected;
    renderCode();
  }
  const downloadable = detail && !active(detail.job.status);
  $("download-result").hidden = !downloadable;
  if (downloadable) $("download-result").href = `/api/runs/${detail.job.run_id}/download`;
}
function renderCode() {
  const artifacts = state.detail?.artifacts || {};
  $("source-code").textContent = artifacts[$("source-select").value] || "任务完成后，可在这里查看生成的 Python 模块。";
  $("code-note").textContent = Object.keys(artifacts).length ? "最近一轮验收的源文件" : "尚无生成代码";
}
function renderMetrics() {
  const detail = state.detail, events = detail?.events || [], summary = detail?.summary;
  let seconds = summary?.elapsed_seconds;
  if (seconds === undefined && events.length) {
    const end = active(detail.job.status) ? Date.now() : new Date(events.at(-1).time).getTime();
    seconds = Math.max(0, (end - new Date(events[0].time).getTime()) / 1000);
  }
  $("metric-time").textContent = seconds === undefined ? "—" : Number(seconds).toFixed(1);
  const report = detail?.rounds.at(-1)?.report;
  const passed = detail?.rounds.at(-1)?.passed;
  $("metric-tests").textContent = report ? passed ? `${report.tests_run}/${report.tests_run}` : "未通过" : "—";
  $("metric-tests-unit").textContent = report && !passed ? `已执行 ${report.tests_run} 项` : "项用例";
  const rounds = events.filter((e) => e.kind === "repair_scheduled").map((e) => e.repair_round);
  $("metric-repairs").textContent = detail ? String(summary?.repair_rounds ?? Math.max(0, ...rounds)) : "—";
  let tokens = summary?.usage?.total_tokens;
  if (tokens === undefined && detail) {
    tokens = events.reduce((sum, e) => sum + Object.values(e.usage || {}).reduce((n, u) => n + (u.total_tokens || 0), 0), 0);
  }
  $("metric-tokens").textContent = tokens === undefined ? "—" : tokens.toLocaleString("en-US");
}
function render() { renderGraph(); renderMetrics(); renderEvidence(); updateSubmit(); }

async function refresh() {
  clearTimeout(state.timer);
  try {
    const {runs} = await api("/api/runs");
    state.runs = runs;
    renderHistory();
    if (state.selected) {
      const id = state.selected;
      const detail = await api(`/api/runs/${id}`);
      if (state.selected === id) { state.detail = detail; render(); }
    }
    connection(true);
  } catch (error) { connection(false, error.message); }
  finally { state.timer = setTimeout(refresh, 1500); }
}
async function initialize() {
  try {
    const meta = await api("/api/meta");
    state.scenarios = meta.scenarios;
    $("scenario-select").replaceChildren(...state.scenarios.map((scenario) => {
      const option = el("option", "", scenario.title); option.value = scenario.id; return option;
    }));
    const {runs} = await api("/api/runs"); state.runs = runs;
    const saved = localStorage.getItem("myself-agent-run");
    const selected = runs.find((run) => active(run.status)) || runs.find((run) => run.run_id === saved) || runs[0];
    if (selected) await selectRun(selected.run_id);
    else { scenarioChanged(); render(); renderHistory(); }
    connection(true); refresh();
  } catch (error) { connection(false, error.message); state.timer = setTimeout(initialize, 5000); }
}

$("run-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.submitting || state.runs.some((run) => active(run.status))) return;
  state.submitting = true; updateSubmit(); $("form-message").textContent = "";
  const data = {scenario: $("scenario-select").value, brief: $("brief").value.trim(),
    concurrency: Number($("concurrency").value), max_repairs: Number($("max-repairs").value)};
  const payload = JSON.stringify(data);
  if (!state.pending || state.pending.payload !== payload) {
    state.pending = {payload, id: crypto.randomUUID().replaceAll("-", "")};
  }
  try {
    const run = await api("/api/runs", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({...data, request_id: state.pending.id})});
    state.pending = null;
    state.runs = [run, ...state.runs.filter((item) => item.run_id !== run.run_id)];
    await selectRun(run.run_id);
  } catch (error) { $("form-message").textContent = error.message; }
  finally { state.submitting = false; updateSubmit(); }
});
$("scenario-select").addEventListener("change", () => {
  state.selected = null; state.detail = null; localStorage.removeItem("myself-agent-run");
  state.graphKey = ""; scenarioChanged(); render(); renderHistory();
});
$("brief").addEventListener("input", () => { $("brief-count").textContent = `${$("brief").value.length} / 1500`; });
$("source-select").addEventListener("change", renderCode);
$("new-run").addEventListener("click", newRun);
$("nav-workbench").addEventListener("click", () => view("workbench"));
$("nav-history").addEventListener("click", () => view("history"));
$("retry-connection").addEventListener("click", () => { clearTimeout(state.timer); state.scenarios.length ? refresh() : initialize(); });
document.querySelectorAll("[data-tab]").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll("[data-tab]").forEach((tab) => {
      const selected = tab === button;
      tab.classList.toggle("active", selected); tab.setAttribute("aria-selected", String(selected));
      $(`pane-${tab.dataset.tab}`).hidden = !selected;
    });
  });
});
new ResizeObserver(drawEdges).observe($("dag"));
initialize();
