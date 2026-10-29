"use strict";

// Conversation workspace and its collapsible live tracker.

const $ = (id) => document.getElementById(id);
const active = (status) => ["running", "queued"].includes(status);
const labels = {queued:"等待启动",running:"执行中",passed:"验收通过",failed:"验收未通过",error:"运行异常",interrupted:"已中断",idle:"未开始"};
const roleNames = {"parser.py":"订单解析", "stats.py":"订单汇总", "tokenize_text.py":"文本分词", "metrics.py":"统计指标", "report.py":"报告生成"};
const state = {scenarios:[], conversations:[], conversation:null, turns:[], tracked:null, detail:null,
  ready:false, sending:false, refreshing:false, pending:null, timer:null, epoch:0,
  chatKey:"", progressKey:"", fileKey:"", testKey:"", listKey:"", selectedFile:null,
  selectedVersion:null, followLatest:true, codeMode:"diff", tab:"progress",
  trackerOpen:localStorage.getItem("myself-tracker-open") === null ? innerWidth > 1100 : localStorage.getItem("myself-tracker-open") === "true",
  navigationOpen:innerWidth > 760};

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function icon(name) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const use = document.createElementNS(node.namespaceURI, "use");
  use.setAttribute("href", `#i-${name}`); node.append(use); node.setAttribute("aria-hidden", "true");
  return node;
}
function scenario() { return state.scenarios.find((row) => row.id === $("scenario-select").value); }
function titleFor(id) { return state.scenarios.find((row) => row.id === id)?.title || id; }
function time(value, date=false) {
  return new Intl.DateTimeFormat("zh-CN", {timeZone:"Asia/Shanghai", hour12:false,
    ...(date ? {month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"} : {hour:"2-digit",minute:"2-digit"})}).format(new Date(value));
}
async function api(path, options={}) {
  const response = await fetch(path, {cache:"no-store", ...options});
  if (response.status === 401) throw new Error("登录已失效，请刷新页面重新登录。");
  const body = await response.json();
  if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : "请求参数无效，请检查后重试。");
  return body;
}
function connection(ok, message="连接暂时中断，任务会继续在服务器上运行。") {
  $("connection-error").hidden = ok;
  $("connection-message").textContent = message;
  $("connection-dot").className = `connection-dot ${ok ? "connected" : "disconnected"}`;
  $("connection-label").textContent = ok ? "home 已连接" : "连接中断";
}
function setTracker(open, persist=true) {
  state.trackerOpen = open;
  $("app").classList.toggle("tracker-open", open);
  $("app").classList.toggle("tracker-closed", !open);
  $("toggle-tracker").setAttribute("aria-expanded", String(open));
  $("task-tracker").inert = !open;
  $("tracker-backdrop").hidden = !open || innerWidth > 1100;
  if (persist) localStorage.setItem("myself-tracker-open", String(open));
  if (open && innerWidth <= 760) setNavigation(false);
  requestAnimationFrame(drawGraph);
}
function setNavigation(open) {
  state.navigationOpen = open;
  $("app").classList.toggle("navigation-closed", !open);
  $("conversation-sidebar").inert = !open;
  $("navigation-backdrop").hidden = !open || innerWidth > 760;
}
function setTab(name) {
  state.tab = name;
  document.querySelectorAll("[data-tab]").forEach((button) => {
    const selected = button.dataset.tab === name;
    button.classList.toggle("active", selected); button.setAttribute("aria-selected", String(selected));
    $(`pane-${button.dataset.tab}`).hidden = !selected;
  });
  if (name === "progress") requestAnimationFrame(drawGraph);
}
function empty(node, title, copy) {
  node.replaceChildren(el("p", "sidebar-empty", title + (copy ? " · " + copy : "")));
}
function renderList() {
  const search = $("conversation-search").value.trim().toLowerCase();
  const rows = state.conversations.filter((row) => (row.title + titleFor(row.scenario)).toLowerCase().includes(search));
  const key = JSON.stringify([rows, state.conversation, search]);
  if (key === state.listKey) return;
  state.listKey = key;
  $("conversation-count").textContent = String(state.conversations.length);
  const list = $("conversation-list"); list.replaceChildren();
  rows.forEach((row) => {
    const button = el("button", `conversation-item ${row.conversation_id === state.conversation ? "selected" : ""}`);
    button.dataset.conversationId = row.conversation_id;
    button.title = row.title;
    button.setAttribute("aria-label",`${row.title}，${labels[row.status]}`);
    const copy = el("div", "conversation-copy");
    copy.append(el("strong", "", row.title), el("small", "", `${titleFor(row.scenario)} · ${row.turns} 条需求`));
    button.append(el("span", `conversation-indicator ${row.status}`), copy);
    button.addEventListener("click", () => selectConversation(row.conversation_id));
    list.append(button);
  });
  if (!rows.length) empty(list, search ? "没有匹配的会话" : "新建会话，开始协作");
}
function updateComposer() {
  const busy = state.conversations.some((row) => active(row.status)) || state.turns.some((row) => active(row.job.status));
  $("send-message").disabled = !state.ready || busy || state.sending || !$("brief").value.trim();
  $("send-message").title = state.sending ? "正在提交" : busy ? "任务完成后可继续发送" : "发送消息";
  $("composer-state").classList.toggle("working", busy || state.sending);
  const indicator = el("i");
  $("composer-state").replaceChildren(indicator, document.createTextNode(state.sending ? "正在提交" : busy ? "任务执行中" : "就绪"));
  $("tracker-live-dot").hidden = !busy;
  $("settings-summary").textContent = `并行 ${$("concurrency").value} · 修复 ${$("max-repairs").value}`;
  $("scenario-select").disabled = state.turns.length > 0;
  const current = scenario();
  $("scope-note").textContent = current ? `${current.title} · ${current.tests} 项固定验收` : "正在连接工作空间";
}
function resetRenderKeys() { state.chatKey = state.progressKey = state.fileKey = state.testKey = ""; }
function newConversation() {
  state.epoch++; state.conversation = null; state.turns = []; state.tracked = null; state.detail = null;
  state.pending = null; state.selectedFile = null; state.followLatest = true; resetRenderKeys();
  localStorage.removeItem("myself-conversation");
  $("brief").value = ""; $("brief").style.height = ""; $("brief-count").textContent = "0 / 1500";
  $("concurrency").value = "2"; $("max-repairs").value = "2"; $("form-message").textContent = "";
  $("run-settings").hidden = true; $("open-settings").setAttribute("aria-expanded", "false");
  if (innerWidth <= 760) setNavigation(false);
  setTab("progress"); render(); $("brief").focus();
}
async function selectConversation(id) {
  const epoch = ++state.epoch;
  state.conversation = id; state.turns = []; state.detail = null; state.tracked = null;
  resetRenderKeys(); localStorage.setItem("myself-conversation", id);
  if (innerWidth <= 760) setNavigation(false);
  render();
  try {
    const data = await api(`/api/conversations/${id}`);
    if (epoch !== state.epoch) return;
    state.turns = data.turns; state.tracked = data.turns.at(-1)?.job.run_id;
    $("scenario-select").value = data.turns.at(-1).job.scenario;
    render(); await loadTracked(epoch); connection(true);
    $("chat-scroll").scrollTop = $("chat-scroll").scrollHeight;
  } catch (error) { if (epoch === state.epoch) connection(false, error.message); }
}
async function loadTracked(epoch=state.epoch) {
  const id = state.tracked;
  if (!id) { state.detail = null; renderTracker(); return; }
  const detail = await api(`/api/runs/${id}`);
  if (id !== state.tracked || epoch !== state.epoch) return;
  state.detail = detail; renderTracker();
}
async function trackRun(id, tab=state.tab) {
  state.tracked = id; state.detail = null; state.selectedFile = null; state.followLatest = true;
  state.progressKey = state.fileKey = state.testKey = "";
  setTracker(true); setTab(tab); renderTracker(); renderChat();
  try { await loadTracked(); } catch (error) { connection(false, error.message); }
}
function moduleStates(tasks, events, status) {
  const result = Object.fromEntries(tasks.map((task) => [task.filename,"waiting"]));
  for (const event of events) {
    if (event.kind === "repair_scheduled") event.targets.forEach((name) => { result[name] = "waiting"; });
    const name = event.role?.replace(/_developer$/, ".py");
    if (name in result && event.kind === "agent_started") result[name] = "running";
    if (name in result && event.kind === "agent_completed") result[name] = "done";
    if (name in result && event.kind === "agent_failed") result[name] = "failed";
  }
  if (!active(status)) Object.keys(result).forEach((name) => { if (result[name] === "running") result[name] = "failed"; });
  return result;
}
function stageInfo(turn, detail=null) {
  const events = turn?.events || [];
  const status = turn?.job.status || "idle";
  const sc = detail?.scenario || state.scenarios.find((row) => row.id === turn?.job.scenario) || scenario();
  const tasks = detail?.plan?.tasks || sc?.tasks || [];
  const modules = moduleStates(tasks, events, status);
  const planDone = events.some((e) => e.kind === "plan_created") || Boolean(detail?.plan)
    || events.some((e) => e.role === "planner" && e.kind === "agent_completed") && Boolean(turn?.summary);
  const plannerEvent = events.filter((e) => e.role === "planner").at(-1);
  const planState = planDone ? "done" : plannerEvent?.kind === "agent_started" && active(status) ? "running"
    : plannerEvent && !active(status) ? "failed" : "waiting";
  const verification = events.filter((e) => e.kind.startsWith("verification_")).at(-1);
  const verificationState = verification?.passed ? "done" : verification?.kind === "verification_started" && active(status)
    ? "running" : verification && !active(status) ? "failed" : "waiting";
  const repairs = events.filter((e) => e.kind === "repair_scheduled").at(-1)?.repair_round || 0;
  const complete = Object.values(modules).filter((value) => value === "done").length + Number(planDone) + Number(verification?.passed === true);
  const total = tasks.length + 2;
  let stage = status === "idle" ? "等待开始" : status === "queued" ? "等待启动" : planDone ? "正在开发" : "正在规划";
  if (active(status) && repairs) stage = `第 ${repairs} 轮修复`;
  if (verificationState === "running") stage = "独立验收中";
  if (active(status) && verificationState === "done") stage = "正在保存结果";
  if (status === "passed") stage = "本轮任务已完成";
  if (status === "failed") stage = "验收未通过";
  if (status === "error" || status === "interrupted") stage = labels[status];
  return {events,status,tasks,modules,planState,verificationState,verification,repairs,
    complete,total,percent:Math.round(complete / total * 100),stage};
}
function assistantCopy(turn) {
  const info = stageInfo(turn), report = turn.summary?.verification?.report;
  if (turn.job.status === "passed") return `本轮${turn.job.parent_run_id ? "修改" : "开发"}已完成，${report?.tests_run || "全部"} 项独立验收通过。\n你可以在右侧查看实际代码变化和测试记录，也可以继续告诉我需要调整的地方。`;
  if (turn.job.status === "failed") return "本轮独立验收未通过，已保留生成代码、失败反馈和修复记录。\n可以查看右侧的验证详情，再继续提出修改要求。";
  if (turn.job.status === "error" || turn.job.status === "interrupted") return `本轮${labels[turn.job.status]}，已保留现有记录。${turn.job.error_type ? `错误类型：${turn.job.error_type}。` : ""}\n你可以查看已完成的修改，再发起下一轮。`;
  if (info.verificationState === "running") return "模块已生成，正在执行独立验收。我会根据测试反馈定位需要修复的模块，并更新右侧的记录。";
  if (info.repairs) return `正在进行第 ${info.repairs} 轮修复，相关模块和受影响的下游会按依赖顺序更新，随后重新验收。`;
  if (info.planState === "done") return "任务已拆分，正在按依赖关系安排开发。文件生成后，右侧会同步显示真实的代码变化。";
  return turn.job.parent_run_id ? "已收到这轮修改要求，将继续处理，并重新执行独立验收。已有代码会作为本轮的修改基础。" : "已收到需求，正在规划模块任务和依赖关系，随后开始开发与验证。";
}
function renderChat() {
  const hasTurns = state.turns.length > 0;
  $("app").classList.toggle("is-empty", !hasTurns);
  $("welcome").hidden = hasTurns; $("messages").hidden = !hasTurns;
  $("conversation-title").textContent = state.turns[0]?.job.brief?.slice(0,44) || (hasTurns ? titleFor(state.turns[0].job.scenario) : "新会话");
  $("conversation-subtitle").textContent = hasTurns ? `${titleFor(state.turns.at(-1).job.scenario)} · ${state.turns.length} 条需求` : "home 工作空间";
  const key = JSON.stringify([state.turns.map((turn) => [turn.job.run_id,turn.job.status,turn.events.length]),state.tracked]);
  if (key === state.chatKey) return;
  state.chatKey = key;
  const scroll = $("chat-scroll"), nearBottom = scroll.scrollHeight-scroll.scrollTop-scroll.clientHeight < 110;
  const list = $("messages"); list.replaceChildren();
  state.turns.forEach((turn, index) => {
    const info = stageInfo(turn), article = el("article", `turn ${turn.job.run_id === state.tracked ? "turn-selected" : ""}`);
    article.dataset.runId = turn.job.run_id;
    const user = el("div", "message-user"), assistant = el("div", "message-assistant"), content = el("div", "assistant-content");
    user.append(el("div", "user-bubble", turn.job.brief || `历史任务：${titleFor(turn.job.scenario)}`));
    const author = el("div", "message-author"); author.append(el("strong", "", "Myself Agent"), el("time", "", time(turn.job.created)));
    content.append(author,el("p", "assistant-text", assistantCopy(turn)));
    const steps = el("div", "assistant-steps");
    function step(status, text) {
      const row = el("div", `assistant-step ${status}`); row.append(el("span", "", status === "done" ? "✓" : status === "failed" ? "!" : status === "running" ? "" : "·"), el("span", "", text)); steps.append(row);
    }
    step(info.planState, info.planState === "done" ? `规划完成 · ${info.tasks.length} 个开发任务` : "任务规划");
    info.tasks.forEach((task) => step(info.modules[task.filename], `${task.filename} · ${{waiting:"等待依赖或派发",running:"正在生成",done:"已生成",failed:"执行异常"}[info.modules[task.filename]]}`));
    step(info.verificationState, info.verificationState === "done" ? `${info.verification.report.tests_run} 项独立验收通过` : info.verificationState === "running" ? "正在执行独立验收" : info.verificationState === "failed" ? "验收未通过" : "独立验收");
    content.append(steps);
    const actions = el("div", "assistant-actions");
    for (const [tab,name,glyph] of [["progress","查看进度","activity"],["changes","查看修改","code"],["tests","查看验收","check"]]) {
      const button = el("button"); button.append(icon(glyph),document.createTextNode(name)); button.addEventListener("click", () => trackRun(turn.job.run_id,tab)); actions.append(button);
    }
    content.append(actions, el("div", "message-run-note", `第 ${index+1} 条需求 · ${turn.job.run_id.slice(0,8)}${turn.job.parent_run_id ? " · 接续上一条需求" : ""}`));
    assistant.append(el("div", "assistant-avatar", "m"),content); article.append(user,assistant); list.append(article);
  });
  if (nearBottom) requestAnimationFrame(() => { scroll.scrollTop = scroll.scrollHeight; });
}

function trackedTurn() {
  if (state.detail?.job.run_id === state.tracked) return {job:state.detail.job,events:state.detail.events,summary:state.detail.summary};
  return state.turns.find((turn) => turn.job.run_id === state.tracked) || null;
}
function eventText(event) {
  const role = event.role === "planner" ? "规划 Agent" : event.role?.replace(/_developer$/, ".py");
  switch (event.kind) {
    case "run_started": return ["开始执行",`并发上限 ${event.concurrency}`];
    case "agent_started": return [`${role} 开始执行`,event.repair_round ? `修复第 ${event.repair_round} 轮` : "首次开发"];
    case "agent_completed": return [`${role} 已完成`,event.elapsed_seconds ? `${event.elapsed_seconds} 秒` : ""];
    case "agent_failed": return [`${role} 执行异常`,event.error_type];
    case "plan_created": return ["任务计划已校验",`${event.tasks.length} 个开发任务`];
    case "batch_scheduled": return [`派发 ${event.tasks.join("、")}`,event.reason];
    case "verification_started": return ["独立验收开始",`第 ${event.repair_round} 轮`];
    case "verification_completed": return [event.passed ? `${event.report.tests_run} 项验收通过` : "验收未通过",event.failure_kind || "独立测试结果已保存"];
    case "repair_scheduled": return [`安排第 ${event.repair_round} 轮修复`,event.targets.join("、")];
    case "run_completed": return ["运行结束",labels[event.outcome]];
    case "controlled_fault_injected": return ["受控故障注入",event.module];
    default: return [event.kind,""];
  }
}
function renderProgress(info) {
  $("tracker-status").className = `status-label ${info.status}`;
  $("tracker-status").textContent = labels[info.status];
  $("progress-percent").replaceChildren(document.createTextNode(String(info.percent)),el("span","","%"));
  $("progress-arc").setAttribute("stroke-dashoffset",(232.48*(1-info.percent/100)).toFixed(2));
  $("progress-stage").textContent = info.stage;
  $("progress-caption").textContent = info.status === "idle" ? "发送需求后开始规划任务" : info.status === "passed"
    ? "代码与独立测试已完成" : `${Object.values(info.modules).filter((s) => s === "done").length} / ${info.tasks.length} 个模块已生成`;
  $("progress-completion").textContent = `${info.complete} / ${info.total} 个阶段已完成`;
  $("task-count").textContent = `${info.tasks.length} 个开发任务`;
  const key = JSON.stringify([state.tracked,info.status,info.modules,info.planState,info.verificationState,info.events.length,info.tasks]);
  if (key === state.progressKey) return;
  state.progressKey = key;
  const list = $("task-checklist"); list.replaceChildren();
  function step(status,title,copy) {
    const row = el("div",`task-step ${status}`), mark = el("span","task-step-mark");
    if (status === "done") mark.append(icon("check"));
    else if (status === "failed") mark.textContent = "!";
    else if (status === "waiting") mark.textContent = "·";
    const text = el("div","task-step-copy"); text.append(el("strong","",title),el("p","",copy));
    row.append(mark,text,el("small","",{waiting:"等待",running:"执行中",done:"已完成",failed:"未完成"}[status])); list.append(row);
  }
  step(info.planState,"需求规划","拆分模块任务，校验依赖关系");
  info.tasks.forEach((task) => step(info.modules[task.filename],task.filename,
    `${roleNames[task.filename] || "开发 Agent"}${task.depends_on.length ? " · 依赖 " + task.depends_on.join("、") : " · 独立任务"}`));
  step(info.verificationState,"独立验收", info.verification?.passed ? `${info.verification.report.tests_run} 项用例全部通过`
    : info.repairs ? `修复 ${info.repairs} 轮 · 随后重新验收` : "按固定测试独立检查代码");
  const levels = $("dag-levels"); levels.replaceChildren();
  const depths = {};
  function depth(task) {
    if (depths[task.filename] !== undefined) return depths[task.filename];
    return depths[task.filename] = task.depends_on.length ? 1+Math.max(...task.depends_on.map((name) => depth(info.tasks.find((row) => row.filename === name)))) : 0;
  }
  const max = Math.max(0,...info.tasks.map(depth));
  for (let level=0;level<=max;level++) {
    const row = el("div","dag-level");
    info.tasks.filter((task) => depths[task.filename] === level).forEach((task) => {
      const node = el("div",`dag-node ${info.modules[task.filename]}`); node.dataset.filename = task.filename;
      node.append(el("strong","",task.filename),el("small","",{waiting:"等待",running:"执行中",done:"已完成",failed:"异常"}[info.modules[task.filename]])); row.append(node);
    });
    levels.append(row);
  }
  $("event-count").textContent = `${info.events.length} 条`;
  $("event-list").replaceChildren(...info.events.map((event) => {
    const [title,copy] = eventText(event), row = el("div","event-entry");
    row.append(el("time","",time(event.time)),el("strong","",title));
    if (copy) row.append(el("p","",copy));
    return row;
  }));
  requestAnimationFrame(drawGraph);
}
function drawGraph() {
  const svg = $("dag-edges"); svg.replaceChildren();
  if (!state.trackerOpen || state.tab !== "progress") return;
  const base = $("dag").getBoundingClientRect();
  if (!base.width || !base.height) return;
  const tasks = state.detail?.plan?.tasks || state.detail?.scenario.tasks || scenario()?.tasks || [];
  const nodes = new Map([...$("dag-levels").querySelectorAll(".dag-node")].map((node) => [node.dataset.filename,node]));
  tasks.forEach((task) => task.depends_on.forEach((dependency) => {
    const from = nodes.get(dependency)?.getBoundingClientRect(), to = nodes.get(task.filename)?.getBoundingClientRect();
    if (!from || !to) return;
    const x1=from.left+from.width/2-base.left, y1=from.bottom-base.top, x2=to.left+to.width/2-base.left, y2=to.top-base.top;
    const path = document.createElementNS("http://www.w3.org/2000/svg","path");
    path.setAttribute("d",`M ${x1} ${y1} C ${x1} ${y1+15}, ${x2} ${y2-15}, ${x2} ${y2}`); svg.append(path);
  }));
}
function renderChanges() {
  const files = state.detail?.changes || [];
  $("change-count").textContent = String(files.length);
  $("file-summary").textContent = files.length ? `正在跟踪 ${files.length} 个文件` : "等待生成代码";
  $("changes-empty").hidden = files.length > 0;
  $("file-viewer").hidden = !files.length;
  const key = JSON.stringify([state.tracked,files]);
  if (key === state.fileKey) return;
  state.fileKey = key;
  if (!files.some((file) => file.filename === state.selectedFile)) { state.selectedFile = files[0]?.filename; state.followLatest=true; }
  const list = $("file-list"); list.replaceChildren();
  files.forEach((file) => {
    const version = file.versions.at(-1), button = el("button",`file-button ${file.filename === state.selectedFile ? "selected" : ""}`);
    button.dataset.filename = file.filename;
    const stats = el("span","file-delta");
    if (version.added || version.removed) stats.append(el("span","added",`+${version.added}`),el("span","removed",`−${version.removed}`));
    else stats.append(el("span","unchanged","未变化"));
    button.append(icon("code"),el("strong","",file.filename),stats);
    button.addEventListener("click",() => { state.selectedFile=file.filename;state.followLatest=true;renderFile(); });
    list.append(button);
  });
  renderFile();
}
function selectedSnapshot() {
  const file = state.detail?.changes.find((row) => row.filename === state.selectedFile);
  if (!file) return null;
  return {file,version:file.versions.find((version) => String(version.round) === state.selectedVersion) || file.versions.at(-1)};
}
function renderFile() {
  const file = state.detail?.changes.find((row) => row.filename === state.selectedFile);
  if (!file) { $("code-view").replaceChildren(); return; }
  if (state.followLatest || !file.versions.some((v) => String(v.round) === state.selectedVersion)) state.selectedVersion=String(file.versions.at(-1).round);
  $("file-version").replaceChildren(...file.versions.map((version) => {
    const option=el("option","",version.round ? `修复第 ${version.round} 轮` : "首次生成"); option.value=String(version.round);return option;
  }));
  $("file-version").value=state.selectedVersion;
  $("selected-file").textContent=file.filename;
  document.querySelectorAll(".file-button").forEach((button) => button.classList.toggle("selected",button.dataset.filename===file.filename));
  const {version} = selectedSnapshot(), first=file.versions[0] === version;
  $("comparison-label").textContent = state.codeMode === "source" ? "当前选中版本的完整源文件" : first
    ? file.based_on_previous_task ? "与上一条需求的代码比较" : "从空文件开始生成" : "与上一文件版本比较";
  $("mode-diff").classList.toggle("active",state.codeMode === "diff");
  $("mode-source").classList.toggle("active",state.codeMode === "source");
  const lines = state.codeMode === "source" ? version.content.split("\n") : version.diff;
  const code = $("code-view"); code.replaceChildren();
  if (!lines.length) { code.append(el("span","code-line diff-empty","此版本与基础代码一致。")); return; }
  const fragment=document.createDocumentFragment();
  lines.forEach((line) => {
    let style="";
    if (state.codeMode === "diff") style = /^(---|\+\+\+|@@)/.test(line) ? "diff-meta" : line.startsWith("+") ? "diff-add" : line.startsWith("-") ? "diff-remove" : "";
    fragment.append(el("span",`code-line ${style}`,line || " "));
  });
  code.append(fragment);
}
function renderTests() {
  const rounds=state.detail?.rounds || [], key=JSON.stringify([state.tracked,rounds]);
  $("tests-empty").hidden = rounds.length > 0;
  if (key === state.testKey) return;
  state.testKey=key;
  $("test-rounds").replaceChildren(...rounds.map((round) => {
    const card=el("article","test-round"), heading=el("div","test-round-title"), report=round.report;
    heading.append(el("strong","",round.round ? `修复后验收 · 第 ${round.round} 轮` : "首次验收"),el("span",`status-label ${round.passed ? "passed" : "failed"}`,round.passed ? "通过" : "未通过"));
    card.append(heading,el("p","",report ? `执行 ${report.tests_run} 项用例 · ${report.failures} 条失败 · ${report.errors} 条错误\n耗时 ${round.elapsed_seconds} 秒`
      : `没有获得完整验收报告 · ${round.failure_kind}`));
    if (report?.failed_cases?.length || !report) {
      const details=el("details"); details.append(el("summary","","查看测试反馈"),el("pre","",report?.failed_cases?.map((row) => `${row.test}\n${row.traceback}`).join("\n\n") || round.output)); card.append(details);
    }
    const repair=state.detail.events.find((event) => event.kind === "repair_scheduled" && event.repair_round===round.round+1);
    if (repair) card.append(el("p","test-repair",`下一轮修复：${repair.targets.join("、")}`));
    return card;
  }));
}
function renderTracker() {
  const optionsKey=JSON.stringify(state.turns.map((turn) => [turn.job.run_id,turn.job.status]));
  if ($("tracked-turn").dataset.key !== optionsKey) {
    $("tracked-turn").dataset.key=optionsKey;
    $("tracked-turn").replaceChildren(...state.turns.map((turn,index) => {
      const option=el("option","",`第 ${index+1} 条需求 · ${labels[turn.job.status]}`);option.value=turn.job.run_id;return option;
    }));
    if (!state.turns.length) $("tracked-turn").append(el("option","","新任务预览"));
  }
  if (state.tracked) $("tracked-turn").value=state.tracked;
  const turn=trackedTurn(), info=stageInfo(turn,state.detail);
  renderProgress(info); renderChanges(); renderTests();
  let seconds=turn?.summary?.elapsed_seconds;
  if (seconds === undefined && info.events.length) {
    const end=active(info.status) ? Date.now() : new Date(info.events.at(-1).time).getTime();
    seconds=Math.max(0,(end-new Date(info.events[0].time).getTime())/1000);
  }
  $("run-duration").textContent = seconds === undefined ? "— 秒" : `${Number(seconds).toFixed(1)} 秒`;
  const tokens=turn?.summary?.usage?.total_tokens ?? info.events.reduce((sum,event) => sum+Object.values(event.usage || {}).reduce((n,usage) => n+(usage?.total_tokens || 0),0),0);
  $("token-usage").textContent = turn ? `${tokens.toLocaleString("en-US")} tokens` : "— tokens";
  $("download-result").hidden = !turn || active(info.status);
  if (turn && !active(info.status)) $("download-result").href=`/api/runs/${turn.job.run_id}/download`;
}
function render() { renderList();renderChat();renderTracker();updateComposer(); }

async function refresh() {
  clearTimeout(state.timer);
  if (state.refreshing) return;
  state.refreshing=true;
  const epoch=state.epoch, id=state.conversation;
  try {
    const responses=await Promise.all([api("/api/conversations"),id ? api(`/api/conversations/${id}`) : Promise.resolve(null)]);
    state.conversations=responses[0].conversations;renderList();updateComposer();
    if (epoch===state.epoch && id) {
      state.turns=responses[1].turns;
      if (!state.turns.some((turn) => turn.job.run_id===state.tracked)) state.tracked=state.turns.at(-1)?.job.run_id;
      renderChat(); await loadTracked(epoch); updateComposer();
    }
    connection(true);
  } catch (error) { if (epoch===state.epoch) connection(false,error.message); }
  finally {
    state.refreshing=false;
    state.timer=setTimeout(refresh,state.conversations.some((row) => active(row.status)) ? 1200 : 4000);
  }
}
async function initialize() {
  clearTimeout(state.timer);
  try {
    const [meta,list]=await Promise.all([api("/api/meta"),api("/api/conversations")]);
    state.scenarios=meta.scenarios;state.conversations=list.conversations;state.ready=true;
    $("scenario-select").replaceChildren(...state.scenarios.map((row) => {const option=el("option","",row.title);option.value=row.id;return option;}));
    const saved=localStorage.getItem("myself-conversation");
    if (saved && state.conversations.some((row) => row.conversation_id===saved)) await selectConversation(saved);
    else render();
    connection(true);refresh();
  } catch (error) { connection(false,error.message);state.timer=setTimeout(initialize,5000); }
}

$("chat-form").addEventListener("submit",async (event) => {
  event.preventDefault();
  if ($("send-message").disabled) return;
  const epoch=state.epoch, brief=$("brief").value.trim();state.sending=true;updateComposer();$("form-message").textContent="";
  const data={scenario:scenario().id,brief,concurrency:Number($("concurrency").value),max_repairs:Number($("max-repairs").value),parent_run_id:state.turns.at(-1)?.job.run_id || null};
  const key=JSON.stringify([state.conversation,data]);
  if (!state.pending || state.pending.key!==key) state.pending={key,body:{...data,request_id:crypto.randomUUID().replaceAll("-",""),conversation_id:state.conversation || crypto.randomUUID().replaceAll("-","")}};
  try {
    const run=await api("/api/runs",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(state.pending.body)});
    state.pending=null;
    if (epoch===state.epoch) {
      $("brief").value="";$("brief").style.height="";$("brief-count").textContent="0 / 1500";
      await selectConversation(run.conversation_id);
    }
    await refresh();
  } catch (error) { if (epoch===state.epoch) $("form-message").textContent=error.message; }
  finally {state.sending=false;updateComposer();}
});
$("brief").addEventListener("input",() => {
  $("brief-count").textContent=`${$("brief").value.length} / 1500`;
  $("brief").style.height="auto";$("brief").style.height=Math.min(210,Math.max(82,$("brief").scrollHeight))+"px";
  updateComposer();
});
$("brief").addEventListener("keydown",(event) => {
  if (event.key==="Enter" && !event.shiftKey && !event.isComposing && event.keyCode!==229) {event.preventDefault();if (!$("send-message").disabled) $("chat-form").requestSubmit();}
});
$("new-conversation").addEventListener("click",newConversation);
$("conversation-search").addEventListener("input",renderList);
$("scenario-select").addEventListener("change",() => {state.progressKey="";render();});
document.querySelectorAll("[data-scenario]").forEach((button) => button.addEventListener("click",() => {
  $("scenario-select").value=button.dataset.scenario;
  $("brief").value={"csv-orders":"开发订单 CSV 解析和汇总工具，正确处理金额精度与异常输入。","text-insights":"开发文本分词与词频统计工具，保持模块职责清晰。","order-report":"开发订单分析报告生成器，并验证解析、汇总和报告模块协作。"}[button.dataset.scenario];
  $("brief").dispatchEvent(new Event("input"));state.progressKey="";render();$("brief").focus();
}));
$("toggle-tracker").addEventListener("click",() => setTracker(!state.trackerOpen));
$("close-tracker").addEventListener("click",() => {setTracker(false);$("toggle-tracker").focus();});
$("tracker-backdrop").addEventListener("click",() => setTracker(false));
$("collapse-navigation").addEventListener("click",() => setNavigation(false));
$("expand-navigation").addEventListener("click",() => setNavigation(!state.navigationOpen));
$("navigation-backdrop").addEventListener("click",() => setNavigation(false));
$("tracked-turn").addEventListener("change",() => trackRun($("tracked-turn").value));
document.querySelectorAll("[data-tab]").forEach((button) => button.addEventListener("click",() => setTab(button.dataset.tab)));
$("file-version").addEventListener("change",() => {state.selectedVersion=$("file-version").value;state.followLatest=false;renderFile();});
$("mode-diff").addEventListener("click",() => {state.codeMode="diff";renderFile();});
$("mode-source").addEventListener("click",() => {state.codeMode="source";renderFile();});
$("copy-source").addEventListener("click",async () => {
  const selected=selectedSnapshot();if (!selected) return;
  try {await navigator.clipboard.writeText(selected.version.content);$("copy-source").replaceChildren(icon("check"));setTimeout(() => $("copy-source").replaceChildren(icon("copy")),1500);}
  catch {$("form-message").textContent="浏览器没有授予剪贴板权限，可以在源码视图中手动复制。";}
});
function settings(open) {$("run-settings").hidden=!open;$("open-settings").setAttribute("aria-expanded",String(open));}
$("open-settings").addEventListener("click",() => settings($("run-settings").hidden));
$("close-settings").addEventListener("click",() => settings(false));
$("concurrency").addEventListener("change",updateComposer);$("max-repairs").addEventListener("change",updateComposer);
$("retry-connection").addEventListener("click",() => state.ready ? refresh() : initialize());
document.addEventListener("keydown",(event) => {if (event.key==="Escape") {if (!$("run-settings").hidden) settings(false);else if (state.trackerOpen) {setTracker(false);$("toggle-tracker").focus();}else if (innerWidth<=760) setNavigation(false);}});
document.addEventListener("click",(event) => {if (!$("run-settings").hidden && !$("run-settings").contains(event.target) && !$("open-settings").contains(event.target)) settings(false);});
window.addEventListener("resize",() => {$("tracker-backdrop").hidden=!state.trackerOpen || innerWidth>1100;$("navigation-backdrop").hidden=!state.navigationOpen || innerWidth>760;requestAnimationFrame(drawGraph);});
new ResizeObserver(drawGraph).observe($("dag"));
setTracker(state.trackerOpen,false);setNavigation(state.navigationOpen);initialize();
