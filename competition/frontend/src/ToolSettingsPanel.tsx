import React, {useEffect, useState} from 'react';

async function api(path: string, body?: unknown) {
  const response = await fetch('/next/api' + path, body === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw Error(typeof data.detail === 'string' ? data.detail : Array.isArray(data.detail) ? data.detail.map((e: any) => e.msg).join('；') : '配置无效，请检查输入');
  return data;
}
const blank = {id: '', name: '', url: '', api_key: '', remove_key: false, allowed_tools: [] as string[]};
const split = (value: string) => value.split(/[\s,]+/).map(v => v.trim()).filter(Boolean);

export function ToolSettingsPanel({workspace, session, onSaved}: {
  workspace: string; session: string; onSaved: () => void;
}) {
  const [scope, setScope] = useState(session ? 'session' : workspace ? 'workspace' : 'global');
  const [policy, setPolicy] = useState<any>({web_enabled: false, browser_enabled: false, max_web_requests: 20, max_mcp_calls: 20, mcp_server_ids: []});
  const [hosts, setHosts] = useState('');
  const [servers, setServers] = useState<any[]>([]);
  const [editor, setEditor] = useState({...blank});
  const [allowed, setAllowed] = useState('');
  const [discovered, setDiscovered] = useState<any[]>([]);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState('');
  const [error, setError] = useState('');
  const [epoch, setEpoch] = useState(0);
  useEffect(() => {
    let valid = true;
    const query = scope === 'session' ? '?session_id=' + session : scope === 'workspace' ? '?workspace_id=' + workspace : '';
    Promise.all([api('/configuration' + query), api('/mcp/servers')]).then(([config, list]) => {
      if (!valid) return;
      setPolicy(config.policy); setHosts((config.policy.allowed_web_hosts || []).join('\n')); setServers(list.servers);
    }).catch(e => {if (valid) setError(e.message);});
    return () => {valid = false;};
  }, [scope, workspace, session, epoch]);
  async function action(work: () => Promise<void>) {
    if (busy) return;
    setBusy(true); setError(''); setStatus('');
    try {await work();} catch(e) {setError(e instanceof Error ? e.message : '操作失败');}
    finally {setBusy(false);}
  }
  function edit(id: string) {
    const server = servers.find(s => s.id === id);
    setEditor(server ? {id: server.id, name: server.name, url: server.url, api_key: '', remove_key: false, allowed_tools: server.allowed_tools} : {...blank});
    setAllowed(server ? server.allowed_tools.join('\n') : ''); setDiscovered([]); setStatus(''); setError('');
  }
  async function savePolicy(event: React.FormEvent) {
    event.preventDefault();
    await action(async () => {
      const id = scope === 'global' ? 'global' : scope === 'session' ? session : workspace;
      await api('/settings/' + scope + '/' + id, {
        web_enabled: !!policy.web_enabled, browser_enabled: !!policy.browser_enabled,
        allowed_web_hosts: split(hosts), max_web_requests: Number(policy.max_web_requests),
        mcp_server_ids: policy.mcp_server_ids || [], max_mcp_calls: Number(policy.max_mcp_calls || 20),
      });
      setEpoch(v => v + 1); onSaved(); setStatus('工具策略已保存，后续运行使用新配置；已有运行保持原快照。');
    });
  }
  async function saveServer(event: React.FormEvent) {
    event.preventDefault();
    await action(async () => {
      const value: any = {...editor, allowed_tools: split(allowed)};
      if (!value.id) delete value.id;
      const saved = await api('/mcp/servers', value);
      setEditor({id: saved.id, name: saved.name, url: saved.url, api_key: '', remove_key: false, allowed_tools: saved.allowed_tools});
      setAllowed(saved.allowed_tools.join('\n')); setEpoch(v => v + 1); onSaved();
      setStatus('MCP 服务已保存；密钥不回显。在上方勾选服务并保存策略后，Agent 才能使用。');
    });
  }
  async function discover() {
    await action(async () => {
      const data = await api('/mcp/servers/' + editor.id + '/test', {});
      setDiscovered(data.tools); setStatus(`连接成功，发现 ${data.tools.length} 个工具。加入白名单后仍需保存服务。`);
    });
  }
  return <div className="tool-settings">
    {error && <p role="alert" className="error">{error}</p>}
    {status && <p role="status" className="settings-status">{status}</p>}
    <form className="settings-form" onSubmit={savePolicy}><fieldset disabled={busy}>
      <h3>本次运行可用的工具</h3>
      <label>工具设置范围<select aria-label="工具设置范围" value={scope} onChange={e => {setScope(e.target.value); setError(''); setStatus('');}}><option value="global">系统默认</option>{workspace && <option value="workspace">当前工作区</option>}{session && <option value="session">当前会话</option>}</select></label>
      <p className="settings-note">系统 → 工作区 → 会话逐级继承。这里仅修改工具策略，不覆盖模型和上下文设置。</p>
      <label className="check"><input type="checkbox" checked={!!policy.web_enabled} onChange={e => setPolicy({...policy, web_enabled: e.target.checked, browser_enabled: e.target.checked ? policy.browser_enabled : false})}/>允许读取白名单网页</label>
      <label className="check"><input type="checkbox" disabled={!policy.web_enabled || busy} checked={!!policy.browser_enabled} onChange={e => setPolicy({...policy, browser_enabled: e.target.checked})}/>启用只读浏览器和截图</label>
      <label>允许访问的完整域名<textarea aria-label="网页域名白名单" value={hosts} placeholder="docs.python.org\nexample.com" onChange={e => setHosts(e.target.value)}/></label>
      <label>每轮网页请求上限<input aria-label="网页请求上限" type="number" min={1} max={50} value={policy.max_web_requests || 20} onChange={e => setPolicy({...policy, max_web_requests: Number(e.target.value)})}/></label>
      <p className="settings-note">仅支持 HTTPS 公网地址和精确域名；资源与重定向也计入额度。浏览器禁用 JavaScript、登录与表单操作。文件 / Shell 容器仍无网络。没有内置通用搜索引擎，可接入可信 MCP 搜索服务。</p>
      <label>每轮 MCP 操作上限<input aria-label="MCP 操作上限" type="number" min={1} max={50} value={policy.max_mcp_calls || 20} onChange={e => setPolicy({...policy, max_mcp_calls: Number(e.target.value)})}/></label>
      <strong>允许 Agent 使用的 MCP 服务</strong>
      {servers.map(server => <label className="check" key={server.id}><input type="checkbox" disabled={busy || (!server.allowed_tools.length && !(policy.mcp_server_ids || []).includes(server.id))} checked={(policy.mcp_server_ids || []).includes(server.id)} onChange={e => setPolicy({...policy, mcp_server_ids: e.target.checked ? [...(policy.mcp_server_ids || []), server.id] : (policy.mcp_server_ids || []).filter((id: string) => id !== server.id)})}/>{server.name}<small>{server.allowed_tools.length ? ` · ${server.allowed_tools.length} 个授权工具` : ' · 无授权工具（可取消勾选）'}</small></label>)}
      {!servers.length && <p className="settings-note">尚未添加 MCP 服务，可在下方配置。</p>}
      <button type="submit">保存工具策略</button>
    </fieldset></form>
    <hr/>
    <form className="settings-form" onSubmit={saveServer}><fieldset disabled={busy}>
      <h3>MCP 服务连接</h3>
      <label>编辑 MCP 服务<select aria-label="编辑 MCP 服务" value={editor.id} onChange={e => edit(e.target.value)}><option value="">＋ 新 MCP 服务</option>{servers.map(server => <option key={server.id} value={server.id}>{server.name} · v{server.version}{server.has_key ? ' · 已配置密钥' : ''}</option>)}</select></label>
      <label>服务名称<input aria-label="MCP 服务名称" required maxLength={80} value={editor.name} onChange={e => setEditor({...editor, name: e.target.value})}/></label>
      <label>Streamable HTTP 地址<input aria-label="MCP 服务地址" required type="url" value={editor.url} placeholder="https://your-server.example/mcp" onChange={e => setEditor({...editor, url: e.target.value})}/></label>
      <label>Bearer 密钥（留空保留原值）<input aria-label="MCP Bearer 密钥" type="password" autoComplete="new-password" value={editor.api_key} onChange={e => setEditor({...editor, api_key: e.target.value})}/></label>
      <label className="check"><input type="checkbox" checked={editor.remove_key} onChange={e => setEditor({...editor, remove_key: e.target.checked})}/>移除已保存的 MCP 密钥</label>
      <label>允许的工具名（逐行填写，不支持通配符）<textarea aria-label="MCP 工具白名单" value={allowed} onChange={e => setAllowed(e.target.value)}/></label>
      <div className="settings-actions"><button type="submit">保存 MCP 服务</button><button type="button" disabled={busy || !editor.id} onClick={discover}>连接并发现工具</button></div>
      <p className="settings-note">仅添加可信服务。MCP 工具可能修改外部系统；只授权所需工具。清空白名单会禁止后续运行调用该服务的全部工具，已有运行保持原快照。支持 Streamable HTTP 与可选 Bearer 密钥，不在宿主机启动任意 stdio 命令。</p>
      {discovered.map(tool => <div className="mcp-discovery" key={tool.name}><strong>{tool.name}</strong><p>{String(tool.description || '').slice(0, 300)}</p><button type="button" onClick={() => setAllowed(old => Array.from(new Set([...split(old), tool.name])).join('\n'))}>加入白名单</button><details><summary>输入参数</summary><pre>{JSON.stringify(tool.inputSchema, null, 2)}</pre></details></div>)}
    </fieldset></form>
  </div>;
}
