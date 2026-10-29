import React, {useEffect, useRef, useState} from 'react';
import {createRoot} from 'react-dom/client';
import './style.css';
import ReactMarkdown from 'react-markdown';

type Workspace = {id: string; name: string};
type Session = {id: string; title: string};
type Run = {id: string; status: string; error_type?: string};
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
  const pendingRequest = useRef<{session: string; text: string; id: string} | null>(null);
  function fail(e: unknown) {setError(e instanceof Error ? e.message : '连接失败');}
  function setDraft(value: string) {setDrafts(old => ({...old, [session || workspace]: value}));}

  useEffect(() => {localStorage.setItem('agent-drafts', JSON.stringify(drafts));}, [drafts]);
  useEffect(() => {localStorage.setItem('agent-widths', JSON.stringify(widths));}, [widths]);
  useEffect(() => {
    api('/workspaces').then(data => {
      setWorkspaces(data.workspaces); setWorkspace(data.workspaces[0]?.id || '');
    }).catch(fail);
  }, []);
  useEffect(() => {
    let valid = true;
    setSession(''); setSessions([]); setMessages([]); setEvents([]); setRuns([]);
    currentSession.current = '';
    if (workspace) api('/sessions?workspace_id=' + workspace).then(data => {
      if (valid) {setSessions(data.sessions); setSession(data.sessions[0]?.id || '');}
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
      setWorkspaces(old => [...old, data]); setWorkspace(data.id);
    } catch (e) {fail(e);}
  }
  async function newSession() {
    if (!workspace) return;
    try {
      const data = await api('/sessions', {workspace_id: workspace});
      setSessions(old => [data, ...old]); setSession(data.id);
      return data.id as string;
    } catch (e) {fail(e);}
  }
  async function send() {
    if (!draft.trim() || active) return;
    setSending(true); setError('');
    const content = draft;
    try {
      const id = session || await newSession(); if (!id) return;
      const previous = pendingRequest.current;
      const requestId = previous?.session === id && previous.text === content ? previous.id : crypto.randomUUID().replaceAll('-', '');
      pendingRequest.current = {session: id, text: content, id: requestId};
      const run = await api('/sessions/' + id + '/messages', {request_id: requestId, text: content});
      pendingRequest.current = null;
      if (currentSession.current === id || !session) {
        setRuns(old => old.some(r => r.id === run.id) ? old : [...old, run]);
        setDrafts(old => ({...old, [id]: '', [workspace]: ''}));
      }
    } catch (e) {fail(e);} finally {setSending(false);}
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
      <div className="workspace-picker"><select disabled={sending} aria-label="选择工作区" value={workspace} onChange={e => setWorkspace(e.target.value)}><option value="" disabled>选择工作区</option>{workspaces.map(w => <option key={w.id} value={w.id}>{w.name}</option>)}</select><button disabled={sending} onClick={createWorkspace} aria-label="创建工作区">＋</button></div>
      <button className="new-session" disabled={!workspace || sending} onClick={newSession}>＋ 新会话</button>
      <input className="search" aria-label="搜索会话" placeholder="搜索会话" value={search} onChange={e => setSearch(e.target.value)}/>
      <button className="group" onClick={() => setGroupOpen(!groupOpen)} aria-expanded={groupOpen}>{groupOpen ? '⌄' : '›'} 工作区会话 <span>{sessions.length}</span></button>
      {groupOpen && <nav>{sessions.filter(s => s.title.includes(search)).map(s => <button className={session === s.id ? 'selected session' : 'session'} key={s.id} disabled={sending} onClick={() => setSession(s.id)}>{s.title}<small>{s.id.slice(0, 8)}</small></button>)}</nav>}
      <footer><span className={connected ? 'dot online' : 'dot'}/> {connected ? '执行追踪已连接' : '选择会话开始'}<small>通用 Agent · 预览</small></footer>
      <div className="resizer right" role="separator" tabIndex={0} aria-label="调整左侧栏宽度" aria-orientation="vertical" onPointerDown={e => resize(0, e)} onKeyDown={e => {if (['ArrowLeft','ArrowRight'].includes(e.key)) setWidths([Math.max(220, Math.min(550, widths[0] + (e.key === 'ArrowRight' ? 20 : -20))), widths[1]]);}}/>
    </aside>}
    <main className="chat">
      <header><div>{!nav && <button onClick={() => setNav(true)} aria-label="展开左侧栏">◨</button>}<span>{sessions.find(s => s.id === session)?.title || '新任务'}<small>{workspaces.find(w => w.id === workspace)?.name || '创建一个工作区开始'}</small></span></div><button onClick={() => setTracker(!tracker)} aria-expanded={tracker}>◫ 任务追踪</button></header>
      {error && <div role="alert" className="error">{error}<button onClick={() => setError('')}>关闭</button></div>}
      <section className="messages" aria-label="会话消息">
        {!messages.length && <div className="welcome"><div className="mark">m·</div><h1>从一个想法，到真实完成。</h1><p>描述任务，让 Agent 在你的工作区读取、修改并验证。</p><button onClick={() => setDraft('创建一个待办事项命令行工具，使用 JSON 保存数据，支持添加、完成和列表，编写 unittest 并运行测试。')}>试试：创建待办事项工具并测试 ↗</button></div>}
        {messages.filter(m => ['human', 'ai'].includes(m.message.type) && text(m.message.data.content)).map(m => <article className={m.message.type === 'human' ? 'message user' : 'message assistant'} key={m.sequence}><small>{m.message.type === 'human' ? '你' : 'Myself Agent'}</small><div>{m.message.type === 'ai' ? <ReactMarkdown>{text(m.message.data.content)}</ReactMarkdown> : text(m.message.data.content)}</div></article>)}
        {active && <div className="working"><span className="pulse"/>正在处理任务，右侧可查看工具执行记录…</div>}
        <div ref={bottom}/>
      </section>
      <div className="composer-zone"><div className="composer-meta"><span>⌂ {workspaces.find(w => w.id === workspace)?.name || '尚未选择工作区'}</span><span>DeepSeek Flash</span></div><div className="composer"><textarea aria-label="任务输入" placeholder="提出任何任务，或继续说明需要修改的地方…" value={draft} disabled={!workspace} onChange={e => setDraft(e.target.value)} onKeyDown={e => {if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {e.preventDefault(); send();}}}/><div><span>文件 · 搜索 · Shell / Python</span>{active ? <button className="send stop" onClick={stop} disabled={sending}>■ 停止</button> : <button className="send" onClick={send} disabled={!workspace || !draft.trim()}>↑ 发送</button>}</div></div><small>独立工作区 · Python 离线执行 · Enter 发送，Shift+Enter 换行</small></div>
    </main>
    {tracker && <aside className="tracker">
      <div className="resizer left" role="separator" tabIndex={0} aria-label="调整右侧栏宽度" aria-orientation="vertical" onPointerDown={e => resize(1, e)} onKeyDown={e => {if (['ArrowLeft','ArrowRight'].includes(e.key)) setWidths([widths[0], Math.max(220, Math.min(550, widths[1] + (e.key === 'ArrowLeft' ? 20 : -20)))]);}}/>
      <header><strong>⌁ 执行追踪</strong><button onClick={() => setTracker(false)} aria-label="收起右侧栏">◫</button></header>
      <div className="run-state"><span className={active ? 'pulse' : 'dot online'}/>{labels[last?.status || ''] || '等待任务'}<small>{tools.length} 次工具调用 · {changes.length} 次文件变更</small></div>
      {plan.length > 0 && <div className="plan">{plan.map((t: any, i: number) => <div key={i}>{t.status === 'completed' ? '✓' : t.status === 'in_progress' ? '◉' : '○'} {t.content}</div>)}<small>{plan.filter((t: any) => t.status === 'completed').length} / {plan.length} 项计划已完成</small></div>}
      <div className="tabs">{[['tools','工具'],['changes','修改'],['events','事件']].map(([id,name]) => <button className={tab === id ? 'active' : ''} key={id} onClick={() => setTab(id)}>{name}</button>)}</div>
      <section className="trace-body">{tab === 'tools' && tools.map(e => {
        const end = events.find(f => ['tool.finished','tool.failed'].includes(f.kind) && f.payload.tool_call_id === e.payload.tool_call_id);
        return <details className="tool-card" key={e.sequence}><summary><span>{end ? end.kind === 'tool.failed' ? '!' : '✓' : '◉'} {e.payload.name}</span><small>{new Date(e.timestamp).toLocaleTimeString('zh-CN', {hour12: false})}</small></summary><pre>{e.payload.input}</pre><pre>{end ? end.payload.output || end.payload.error_type : '正在执行…'}</pre></details>;
      })}{tab === 'changes' && changes.map(e => <details className="tool-card" key={e.sequence}><summary>{e.payload.path}</summary><pre className="diff">{e.payload.diff}</pre></details>)}{tab === 'events' && events.map(e => <details className="tool-card" key={e.sequence}><summary>{e.kind}<small>#{e.sequence}</small></summary><pre>{JSON.stringify(e.payload, null, 2)}</pre></details>)}{!events.length && <div className="empty-trace">⌁<h3>每一步，都有记录。</h3><p>任务开始后，在这里查看真实工具结果和文件变更。</p></div>}</section>
    </aside>}
  </div>;
}

createRoot(document.getElementById('root')!).render(<App/>);
