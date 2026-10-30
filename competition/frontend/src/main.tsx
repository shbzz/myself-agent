import React, {useEffect, useRef, useState} from 'react';
import {createRoot} from 'react-dom/client';
import './style.css';
import ReactMarkdown from 'react-markdown';
import {SettingsPanel} from './SettingsPanel';
import {WorkspacePanel} from './WorkspacePanel';

type Workspace = {id: string; name: string};
type Session = {id: string; title: string};
type Run = {id: string; status: string; text: string; attachment_ids: string[]; created: string; updated: string; error_type?: string};
type Attachment = {id: string; filename: string};
type Message = {sequence: number; message: {type: string; data: {content: unknown}}};
type Trace = {sequence: number; run_id: string; kind: string; timestamp: string; payload: Record<string, any>};
const labels: Record<string, string> = {queued: '排队中', running: '执行中', succeeded: '已结束', failed: '执行失败', cancelled: '已停止', interrupted: '执行中断'};

async function api(path: string, body?: unknown) {
  const response = await fetch('/next/api' + path, body === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw Error(typeof data.detail === 'string' ? data.detail : '请求失败，请检查输入');
  return data;
}

function text(content: unknown): string {
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) return content.filter(c => c.type === 'text').map(c => c.text).join('\n');
  return '';
}

function toolTitle(event: Trace) {
  const names: Record<string, string> = {execute: '运行命令', read_file: '读取文件', write_file: '创建文件', edit_file: '修改文件', ls: '浏览目录', grep: '搜索内容', glob: '查找文件', write_todos: '更新计划'};
  const args = event.payload.inputs || {};
  let description = args.command || args.file_path || args.path || args.pattern || '';
  if (!description && event.payload.input) description = event.payload.input;
  const image = event.payload.name === 'read_file' && /\.(png|jpe?g|gif|webp)$/i.test(args.file_path || '');
  return `${image ? '读取图片' : names[event.payload.name] || event.payload.name}${description ? ' · ' + String(description).split('\n')[0].slice(0, 100) : ''}`;
}

function ToolActivity({event, events}: {event: Trace; events: Trace[]}) {
  const end = events.find(e => ['tool.finished', 'tool.failed'].includes(e.kind) && e.payload.tool_call_id === event.payload.tool_call_id);
  const failed = end && (end.kind === 'tool.failed' || end.payload.failed || /Command failed with exit code/.test(end.payload.output || ''));
  const elapsed = end ? end.payload.elapsed_ms ?? new Date(end.timestamp).getTime() - new Date(event.timestamp).getTime() : null;
  const args = event.payload.inputs;
  return <details className="inline-tool"><summary><span className="tool-glyph">{event.payload.name === 'execute' ? '⌘' : '↳'}</span><span className="tool-title">{toolTitle(event)}</span><small>{end ? `${failed ? '失败' : '已结束'} · ${(elapsed / 1000).toFixed(2)} 秒` : '运行中…'}</small><span className="disclosure">⌄</span></summary><div className="tool-expanded"><h4>{event.payload.name === 'execute' ? '执行的命令' : '调用参数'}</h4><pre>{args?.command || (args ? JSON.stringify(args, null, 2) : event.payload.input)}</pre><h4>运行结果</h4><pre>{end ? end.payload.output || end.payload.error_type || '完成，无文本输出' : '等待工具返回…'}</pre><small>开始于 {new Date(event.timestamp).toLocaleTimeString('zh-CN', {hour12: false})}{end && ` · 耗时 ${(elapsed / 1000).toFixed(2)} 秒`}</small></div></details>;
}

function AssistantMessage({data}: {data: any}) {
  const reasoning = data.additional_kwargs?.reasoning_content;
  const answer = text(data.content);
  if (!reasoning && !answer) return null;
  return <article className="message assistant">{reasoning && <details className="reasoning"><summary>✦ 思考过程 <span>点击展开</span></summary><div>{reasoning}</div></details>}{answer && <div><ReactMarkdown>{answer}</ReactMarkdown></div>}</article>;
}

function App() {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [workspace, setWorkspace] = useState('');
  const [sessions, setSessions] = useState<Session[]>([]);
  const [session, setSession] = useState('');
  const [messages, setMessages] = useState<Message[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [events, setEvents] = useState<Trace[]>([]);
  const [drafts, setDrafts] = useState<Record<string, string>>(() => JSON.parse(localStorage.getItem('agent-drafts') || '{}'));
  const [error, setError] = useState('');
  const [sending, setSending] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [attachments, setAttachments] = useState<Record<string, Attachment[]>>({});
  const [thinking, setThinking] = useState('low');
  const [showSettings, setShowSettings] = useState(false);
  const [showFiles, setShowFiles] = useState(false);
  const [models, setModels] = useState<any[]>([]);
  const [configuration, setConfiguration] = useState<any>(null);
  const [configEpoch, setConfigEpoch] = useState(0);
  const [imagePreview, setImagePreview] = useState('');
  const fileInput = useRef<HTMLInputElement>(null);
  const [connected, setConnected] = useState(false);
  const [groupOpen, setGroupOpen] = useState(true);
  const [nav, setNav] = useState(window.innerWidth > 760);
  const [tracker, setTracker] = useState(window.innerWidth > 1100);
  const [tab, setTab] = useState('tools');
  const [search, setSearch] = useState('');
  const [widths, setWidths] = useState(() => JSON.parse(localStorage.getItem('agent-widths') || '[270,380]'));
  const bottom = useRef<HTMLDivElement>(null);
  const currentSession = useRef('');
  const last = runs.at(-1);
  const active = sending || ['running', 'queued'].includes(last?.status || '');
  const draft = drafts[session || workspace] || '';
  const attached = attachments[session] || [];
  const pendingRequest = useRef<{session: string; text: string; id: string} | null>(null);
  function fail(e: unknown) {setError(e instanceof Error ? e.message : '连接失败');}
  function setDraft(value: string) {setDrafts(old => ({...old, [session || workspace]: value}));}

  useEffect(() => {
    let valid = true;
    const query = session ? '?session_id=' + session : workspace ? '?workspace_id=' + workspace : '';
    Promise.all([api('/models'), api('/configuration' + query)]).then(([m,c]) => {
      if (valid) {setModels(m.models); setConfiguration(c); setThinking(previous => c.profile.reasoning_levels.includes(previous) ? previous : 'off');}
    }).catch(fail);
    return () => {valid = false;};
  }, [session, workspace, configEpoch]);

  useEffect(() => {localStorage.setItem('agent-drafts', JSON.stringify(drafts));}, [drafts]);
  useEffect(() => {localStorage.setItem('agent-widths', JSON.stringify(widths));}, [widths]);
  useEffect(() => {
    api('/workspaces').then(data => {
      setWorkspaces(data.workspaces); const preferred = localStorage.getItem('agent-workspace'); setWorkspace(data.workspaces.some((w: any) => w.id === preferred) ? preferred! : data.workspaces[0]?.id || '');
    }).catch(fail);
  }, []);
  useEffect(() => {
    let valid = true;
    setSession(''); setSessions([]); setMessages([]); setEvents([]); setRuns([]);
    currentSession.current = '';
    if (workspace) api('/sessions?workspace_id=' + workspace).then(data => {
      if (valid) {setSessions(data.sessions); const saved = localStorage.getItem('agent-session:'+workspace); setSession(data.sessions.some((s: any) => s.id === saved) ? saved! : data.sessions[0]?.id || '');}
    }).catch(fail);
    return () => {valid = false;};
  }, [workspace]);
  useEffect(() => {
    currentSession.current = session;
    setMessages([]); setEvents([]); setRuns([]); setConnected(false);
    if (!session) return;
    let valid = true;
    let revision = 0;
    async function refresh() {
      const mine = ++revision;
      try {
        const data = await api('/sessions/' + session);
        if (valid && mine === revision) {setMessages(data.messages); setRuns(data.runs);}
      } catch (e) {if (valid) fail(e);}
    }
    refresh();
    const source = new EventSource('/next/api/sessions/' + session + '/events');
    source.onopen = () => {if (valid) setConnected(true);};
    source.onerror = () => {if (valid) setConnected(false);};
    source.onmessage = event => {
      if (!valid) return;
      const item: Trace = JSON.parse(event.data);
      setEvents(old => old.some(e => e.sequence === item.sequence) ? old : [...old, item]);
      if (item.kind.startsWith('run.') || item.kind === 'message.created') refresh();
    };
    const timer = setInterval(refresh, 4000);
    return () => {valid = false; source.close(); clearInterval(timer);};
  }, [session]);
  useEffect(() => {bottom.current?.scrollIntoView({behavior: 'smooth'});}, [messages.length]);

  async function createWorkspace() {
    const name = prompt('新工作区名称'); if (!name?.trim()) return;
    try {
      const data = await api('/workspaces', {name});
      setWorkspaces(old => [...old, data]); localStorage.setItem('agent-workspace', data.id); setWorkspace(data.id);
    } catch (e) {fail(e);}
  }
  async function newSession() {
    if (!workspace) return;
    try {
      const data = await api('/sessions', {workspace_id: workspace});
      setSessions(old => [data, ...old]); localStorage.setItem('agent-session:'+workspace, data.id); setSession(data.id);
      return data.id as string;
    } catch (e) {fail(e);}
  }
  async function send() {
    if ((!draft.trim() && !attached.length) || active || uploading) return;
    setSending(true); setError('');
    const content = draft;
    try {
      const id = session || await newSession(); if (!id) return;
      const signature = JSON.stringify([content, attached.map(a => a.id), thinking, configuration?.profile.id]);
      const previous = pendingRequest.current;
      const requestId = previous?.session === id && previous.text === signature ? previous.id : crypto.randomUUID().replaceAll('-', '');
      pendingRequest.current = {session: id, text: signature, id: requestId};
      const run = await api('/sessions/' + id + '/messages', {request_id: requestId, text: content, attachment_ids: attached.map(a => a.id), reasoning_effort: thinking, model_profile_id: configuration?.profile.id});
      pendingRequest.current = null;
      if (currentSession.current === id || !session) {
        setRuns(old => old.some(r => r.id === run.id) ? old : [...old, run]);
        setDrafts(old => ({...old, [id]: '', [workspace]: ''}));
        setAttachments(old => ({...old, [id]: []}));
      }
    } catch (e) {fail(e);} finally {setSending(false);}
  }
  async function uploadImages(files: File[]) {
    if (!workspace || uploading || sending) return;
    setUploading(true); setError('');
    try {
      const id = session || await newSession(); if (!id) return;
      if (!session) setDrafts(old => ({...old, [id]: draft}));
      let count = (attachments[id] || []).length;
      for (const file of files) {
        if (count >= 4) throw Error('每条消息最多 4 张图片');
        if (!file.type.startsWith('image/')) continue;
        if (file.size > 6 * 1024 * 1024) throw Error('单张图片不能超过 6 MiB');
        const encoded = await new Promise<string>((resolve, reject) => {
          const reader = new FileReader(); reader.onload = () => resolve(String(reader.result).split(',')[1]); reader.onerror = reject; reader.readAsDataURL(file);
        });
        const item = await api('/sessions/' + id + '/attachments', {filename: file.name, data_base64: encoded});
        setAttachments(old => ({...old, [id]: [...(old[id] || []), item]})); count++;
      }
    } catch(e) {fail(e);} finally {setUploading(false);}
  }
  async function chooseModel(identifier: string) {
    const model = models.find(m => m.id === identifier); if (!model || !configuration) return;
    const scope = session ? 'session' : 'workspace'; const id = session || workspace;
    try {
      const budget = Math.min(configuration.policy.effective_context_tokens, model.context_window_tokens);
      await api('/settings/' + scope + '/' + id, {model_profile_id: identifier, effective_context_tokens: budget, output_reserve_tokens: Math.min(configuration.policy.output_reserve_tokens, model.max_output_tokens, budget - 4096)});
      setConfigEpoch(v => v + 1);
    } catch(e) {fail(e);}
  }
  async function compact() {
    if (!session || active) return;
    try {
      const run = await api('/sessions/'+session+'/compact', {request_id:crypto.randomUUID().replaceAll('-', ''), model_profile_id:configuration?.profile.id});
      setRuns(old => [...old, run]);
    } catch(e) {fail(e);}
  }
  async function stop() {
    if (!last) return;
    try {await api('/runs/' + last.id + '/stop', {});} catch (e) {fail(e);}
  }
  function resize(side: number, event: React.PointerEvent) {
    const start = event.clientX, initial = widths[side];
    function move(e: PointerEvent) {
      setWidths((old: number[]) => old.map((v, i) => i === side ? Math.max(220, Math.min(550, initial + (e.clientX - start) * (side ? -1 : 1))) : v));
    }
    function up() {window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up);}
    window.addEventListener('pointermove', move); window.addEventListener('pointerup', up);
  }
  const plan = [...events].reverse().find(e => e.kind === 'plan.updated')?.payload.todos || [];
  const changes = events.filter(e => e.kind === 'file.changed');
  const tools = events.filter(e => e.kind === 'tool.started');
  return <div className={`workbench ${nav ? '' : 'nav-closed'} ${tracker ? '' : 'tracker-closed'}`} style={{'--nav': `${widths[0]}px`, '--tracker': `${widths[1]}px`} as React.CSSProperties}>
    {nav && <aside className="navigation">
      <header><strong className="brand">m<span>·</span> <small>myself agent</small></strong><button onClick={() => setNav(false)} aria-label="收起左侧栏">◧</button></header>
      <div className="workspace-picker"><select disabled={sending || uploading} aria-label="选择工作区" value={workspace} onChange={e => {localStorage.setItem('agent-workspace', e.target.value); setWorkspace(e.target.value);}}><option value="" disabled>选择工作区</option>{workspaces.map(w => <option key={w.id} value={w.id}>{w.name}</option>)}</select><button disabled={sending || uploading} onClick={createWorkspace} aria-label="创建工作区">＋</button></div>
      <button className="workspace-files-button" onClick={() => setShowFiles(true)}>▤ 工作区文件 / 导入项目</button>
      <button className="new-session" disabled={!workspace || sending} onClick={newSession}>＋ 新会话</button>
      <input className="search" aria-label="搜索会话" placeholder="搜索会话" value={search} onChange={e => setSearch(e.target.value)}/>
      <button className="group" onClick={() => setGroupOpen(!groupOpen)} aria-expanded={groupOpen}>{groupOpen ? '⌄' : '›'} 工作区会话 <span>{sessions.length}</span></button>
      {groupOpen && <nav>{sessions.filter(s => s.title.includes(search)).map(s => <button className={session === s.id ? 'selected session' : 'session'} key={s.id} disabled={sending || uploading} onClick={() => {localStorage.setItem('agent-session:'+workspace, s.id); setSession(s.id);}}>{s.title}<small>{s.id.slice(0, 8)}</small></button>)}</nav>}
      <footer><span className={connected ? 'dot online' : 'dot'}/> {connected ? '执行追踪已连接' : '选择会话开始'}<small>通用 Agent · 预览</small><button onClick={() => setShowSettings(true)}>⚙ 设置</button></footer>
      <div className="resizer right" role="separator" tabIndex={0} aria-label="调整左侧栏宽度" aria-orientation="vertical" onPointerDown={e => resize(0, e)} onKeyDown={e => {if (['ArrowLeft','ArrowRight'].includes(e.key)) setWidths([Math.max(220, Math.min(550, widths[0] + (e.key === 'ArrowRight' ? 20 : -20))), widths[1]]);}}/>
    </aside>}
    <main className="chat">
      <header><div>{!nav && <button onClick={() => setNav(true)} aria-label="展开左侧栏">◨</button>}<span>{sessions.find(s => s.id === session)?.title || '新任务'}<small>{workspaces.find(w => w.id === workspace)?.name || '创建一个工作区开始'}</small></span></div><div><button onClick={() => setShowFiles(true)} aria-label="打开工作区文件">▤ 文件</button><button onClick={() => setTracker(!tracker)} aria-expanded={tracker}>◫ 任务追踪</button></div></header>
      {error && <div role="alert" className="error">{error}<button onClick={() => setError('')}>关闭</button></div>}
      <section className="messages" aria-label="会话消息">
        {!runs.length && <div className="welcome"><div className="mark">m·</div><h1>一起，探索想法的下一步。</h1><p>提出任务，添加图片，在这里看见每一步执行。</p><button onClick={() => setDraft('创建一个待办事项命令行工具，使用 JSON 保存数据，支持添加、完成和列表，编写 unittest 并运行测试。')}>创建工具并运行测试 ↗</button></div>}
        {runs.map(run => <section className="conversation-turn" key={run.id}>
          <article className="message user"><small>你</small>{run.attachment_ids?.length > 0 && <div className="message-images">{run.attachment_ids.map(id => <button key={id} onClick={() => setImagePreview(id)} aria-label="放大会话图片"><img src={'/next/api/attachments/' + id} alt="会话附件"/></button>)}</div>}{run.text && <div>{run.text}</div>}</article>
          {events.filter(e => e.run_id === run.id).map(e => e.kind === 'tool.started' ? <ToolActivity key={e.sequence} event={e} events={events}/> : e.kind === 'message.created' && e.payload.type === 'ai' ? <AssistantMessage key={e.sequence} data={e.payload.data}/> : e.kind === 'context.compressed' ? <details className="inline-tool" key={e.sequence}><summary>已压缩上下文 · {e.payload.estimated_before_tokens} → {e.payload.estimated_after_tokens} tokens（估算）</summary><pre>{e.payload.summary}</pre></details> : e.kind === 'context.unchanged' ? <p className="turn-status" key={e.sequence}>{e.payload.reason}</p> : e.kind === 'run.error' ? <div className="error" key={e.sequence}>{e.payload.detail || e.payload.error_type}</div> : e.kind === 'image.submitted' ? <details className="inline-tool image-activity" key={e.sequence}><summary><span className="tool-glyph">▧</span><span>查看图片 · {e.payload.attachment_ids.length} 张</span><small>原生视觉输入</small></summary><div className="message-images">{e.payload.attachment_ids.map((id: string) => <button key={id} onClick={() => setImagePreview(id)}><img src={'/next/api/attachments/' + id} alt="提交给模型的图片"/></button>)}</div></details> : null)}
          {!['queued','running'].includes(run.status) && <div className="turn-status">{labels[run.status]} · {(Math.max(0, new Date(run.updated).getTime() - new Date(run.created).getTime()) / 1000).toFixed(1)} 秒</div>}
        </section>)}
        {active && <div className="working"><span className="pulse"/>正在处理任务…</div>}
        <div ref={bottom}/>
      </section>
      <div className="composer-zone"><div className="composer-meta"><span>⌂ {workspaces.find(w => w.id === workspace)?.name || '尚未选择工作区'}</span><select aria-label="当前模型" disabled={!workspace || active || uploading} value={configuration?.profile.id || ""} onChange={e => chooseModel(e.target.value)}>{models.map(m => <option key={m.id} value={m.id}>{m.name}</option>)}</select></div><div className="composer" onDragOver={e => e.preventDefault()} onDrop={e => {e.preventDefault(); uploadImages(Array.from(e.dataTransfer.files));}}>
        {attached.length > 0 && <div className="draft-images">{attached.map(item => <figure key={item.id}><img src={'/next/api/attachments/' + item.id} alt={item.filename}/><button aria-label={'移除图片 ' + item.filename} onClick={() => setAttachments(old => ({...old, [session]: attached.filter(a => a.id !== item.id)}))}>×</button><figcaption>{item.filename}</figcaption></figure>)}</div>}
        <textarea aria-label="任务输入" placeholder="描述要构建的内容，或粘贴图片开始…" value={draft} disabled={!workspace || uploading} onChange={e => setDraft(e.target.value)} onPaste={e => {const files = Array.from(e.clipboardData.files); if (files.length) {e.preventDefault(); uploadImages(files);}}} onKeyDown={e => {if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {e.preventDefault(); send();}}}/>
        <div><div className="composer-actions"><input ref={fileInput} hidden type="file" accept="image/png,image/jpeg,image/gif,image/webp" multiple onChange={e => {uploadImages(Array.from(e.target.files || [])); e.target.value = '';}}/><button className="attach-button" aria-label="添加图片" onClick={() => fileInput.current?.click()} disabled={!workspace || uploading}>{uploading ? '上传中…' : '＋'}</button><select aria-label="思考程度" value={thinking} onChange={e => setThinking(e.target.value)}>{(configuration?.profile.reasoning_levels || ['off','low','high','max']).map((level: string) => <option key={level} value={level}>{({off:'关闭/默认',low:'轻量思考',high:'深入思考',max:'极致思考'} as Record<string,string>)[level] || level}</option>)}</select></div>{active ? <button className="send stop" onClick={stop} disabled={sending || uploading}>■ 停止</button> : <button className="send" onClick={send} disabled={!workspace || uploading || (!draft.trim() && !attached.length)}>↑ 发送</button>}</div></div><div className="context-controls"><small>{(() => {const c = [...events].reverse().find(e => e.kind === 'context.estimated' && e.run_id === last?.id)?.payload; return c ? `上下文估算 ${(c.estimated_tokens/1024).toFixed(1)}K / ${(c.budget_tokens/1024).toFixed(0)}K（含输出预留）` : `上下文预算 ${(Number(configuration?.policy.effective_context_tokens || 0)/1024).toFixed(0)}K`;})()}</small><button disabled={!session || active} onClick={compact}>压缩上下文</button><button onClick={() => setShowSettings(true)}>设置</button></div></div>
    </main>
    {tracker && <aside className="tracker">
      <div className="resizer left" role="separator" tabIndex={0} aria-label="调整右侧栏宽度" aria-orientation="vertical" onPointerDown={e => resize(1, e)} onKeyDown={e => {if (['ArrowLeft','ArrowRight'].includes(e.key)) setWidths([widths[0], Math.max(220, Math.min(550, widths[1] + (e.key === 'ArrowLeft' ? 20 : -20)))]);}}/>
      <header><strong>⌁ 执行追踪</strong><button onClick={() => setTracker(false)} aria-label="收起右侧栏">◫</button></header>
      <div className="run-state"><span className={active ? 'pulse' : 'dot online'}/>{labels[last?.status || ''] || '等待任务'}<small>{tools.length} 次工具调用 · {changes.length} 次文件变更</small>{(() => {const usage = [...events].reverse().find(e => e.kind === 'run.finished' && e.run_id === last?.id)?.payload.usage; if (!usage) return null; const total = Object.values(usage).reduce((sum: number, value: any) => sum + Number(value.total_tokens || 0), 0); return <small>本轮累计实际用量：{total} tokens</small>;})()}</div>
      {plan.length > 0 && <div className="plan">{plan.map((t: any, i: number) => <div key={i}>{t.status === 'completed' ? '✓' : t.status === 'in_progress' ? '◉' : '○'} {t.content}</div>)}<small>{plan.filter((t: any) => t.status === 'completed').length} / {plan.length} 项计划已完成</small></div>}
      <div className="tabs">{[['tools','工具'],['changes','修改'],['events','事件']].map(([id,name]) => <button className={tab === id ? 'active' : ''} key={id} onClick={() => setTab(id)}>{name}</button>)}</div>
      <section className="trace-body">{tab === 'tools' && tools.map(e => {
        const end = events.find(f => ['tool.finished','tool.failed'].includes(f.kind) && f.payload.tool_call_id === e.payload.tool_call_id);
        return <details className="tool-card" key={e.sequence}><summary><span>{end ? (end.kind === 'tool.failed' || end.payload.failed || /Command failed with exit code/.test(end.payload.output || '')) ? '!' : '✓' : '◉'} {e.payload.name}</span><small>{new Date(e.timestamp).toLocaleTimeString('zh-CN', {hour12: false})}</small></summary><pre>{e.payload.input}</pre><pre>{end ? end.payload.output || end.payload.error_type : '正在执行…'}</pre></details>;
      })}{tab === 'changes' && changes.map(e => <details className="tool-card" key={e.sequence}><summary>{e.payload.path}</summary><pre className="diff">{e.payload.diff}</pre></details>)}{tab === 'events' && events.map(e => <details className="tool-card" key={e.sequence}><summary>{e.kind}<small>#{e.sequence}</small></summary><pre>{JSON.stringify(e.payload, null, 2)}</pre></details>)}{!events.length && <div className="empty-trace">⌁<h3>每一步，都有记录。</h3><p>任务开始后，在这里查看真实工具结果和文件变更。</p></div>}</section>
    </aside>}
    {showFiles && <WorkspacePanel workspace={workspaces.find(w => w.id === workspace)} onClose={() => setShowFiles(false)} onImported={item => {setWorkspaces(old => [...old, item]); localStorage.setItem('agent-workspace', item.id); setWorkspace(item.id);}}/>}
    {showSettings && <SettingsPanel workspace={workspace} session={session} onClose={() => setShowSettings(false)} onSaved={() => setConfigEpoch(v => v + 1)}/>}
    {imagePreview && <div className="image-overlay" role="dialog" aria-modal="true" aria-label="图片预览" onClick={() => setImagePreview('')}><button aria-label="关闭图片预览" onClick={() => setImagePreview('')}>×</button><img src={'/next/api/attachments/' + imagePreview} alt="图片预览" onClick={e => e.stopPropagation()}/></div>}
  </div>;
}

createRoot(document.getElementById('root')!).render(<App/>);
