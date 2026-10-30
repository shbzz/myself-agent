import React, {useEffect, useState} from 'react';

export async function settingsApi(path: string, body?: unknown) {
  const response = await fetch('/next/api' + path, body === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail));
  return data;
}

const blankProvider = {id: '', name: '', protocol: 'openai_compatible', base_url: '', api_key: '', remove_key: false};
const blankModel = {id: '', name: '', provider_id: '', model_id: '', context_window_tokens: 131072, max_output_tokens: 8192, images: false, tools: true, streaming: true, reasoning_parameter: 'none', reasoning_off_value: null, output_token_parameter: 'max_tokens', reasoning_levels: ['off']};

export function SettingsPanel({workspace, session, onClose, onSaved}: {
  workspace: string; session: string; onClose: () => void; onSaved: () => void;
}) {
  const [tab, setTab] = useState('providers');
  const [providers, setProviders] = useState<any[]>([]);
  const [models, setModels] = useState<any[]>([]);
  const [provider, setProvider] = useState<any>(blankProvider);
  const [model, setModel] = useState<any>(blankModel);
  const [policy, setPolicy] = useState<any>({});
  const [records, setRecords] = useState<any[]>([]);
  const [scope, setScope] = useState(session ? 'session' : workspace ? 'workspace' : 'global');
  const [status, setStatus] = useState('');
  const [busy, setBusy] = useState(false);
  async function reload() {
    const query = session ? '?session_id=' + session : workspace ? '?workspace_id=' + workspace : '';
    const [p, m, c] = await Promise.all([settingsApi('/providers'), settingsApi('/models'), settingsApi('/configuration' + query)]);
    setProviders(p.providers); setModels(m.models); setPolicy(c.policy);
    if (session) setRecords((await settingsApi('/sessions/' + session + '/context')).records);
  }
  useEffect(() => {reload().catch(e => setStatus(e.message));}, []);
  async function action(work: () => Promise<void>) {
    setBusy(true); setStatus('');
    try {await work(); onSaved();} catch(e) {setStatus(e instanceof Error ? e.message : '操作失败');}
    finally {setBusy(false);}
  }
  async function saveProvider() {
    await action(async () => {
      const value = {...provider}; if (!value.id) delete value.id;
      const saved = await settingsApi('/providers', value);
      const {version, has_key, ...fields} = saved; setProvider({...blankProvider, ...fields, api_key: ''}); await reload(); setStatus('供应商已保存；密钥不会回显。');
    });
  }
  async function saveModel() {
    await action(async () => {
      const value = {...model}; if (!value.id) delete value.id;
      const saved = await settingsApi('/models', value);
      const {version, ...fields} = saved; setModel(fields); await reload(); setStatus('模型能力声明已保存；连接测试不代表能力已验证。');
    });
  }
  async function savePolicy() {
    await action(async () => {
      const id = scope === 'global' ? 'global' : scope === 'session' ? session : workspace;
      await settingsApi('/settings/' + scope + '/' + id, policy);
      await reload(); setStatus('设置已保存，将用于后续运行；正在执行的任务保持原配置。');
    });
  }
  const field = (label: string, value: any, change: (v: string) => void, type='text') => <label>{label}<input type={type} value={value ?? ''} onChange={e => change(e.target.value)}/></label>;
  return <div className="settings-overlay" role="dialog" aria-modal="true" aria-label="设置">
    <section className="settings-panel"><header><strong>设置</strong><button onClick={onClose} aria-label="关闭设置">×</button></header>
      <nav className="settings-tabs">{[['providers','供应商'],['models','模型能力'],['context','上下文与执行'],['records','压缩记录']].map(([id,label]) => <button key={id} onClick={() => setTab(id)} className={tab === id ? 'active' : ''}>{label}</button>)}</nav>
      <div className="settings-content">{status && <p role="status" className="settings-status">{status}</p>}
      {tab === 'providers' && <div className="settings-form">
        <label>编辑供应商<select aria-label="编辑供应商" value={provider.id} onChange={e => {const p = providers.find(p => p.id === e.target.value); if (p) {const {version, has_key, ...fields} = p; setProvider({...blankProvider, ...fields});} else setProvider(blankProvider);}}><option value="">＋ 新供应商</option>{providers.map(p => <option key={p.id} value={p.id}>{p.name} · v{p.version}{p.has_key ? ' · 已配置密钥' : ''}</option>)}</select></label>
        {field('名称', provider.name, v => setProvider({...provider, name:v}))}
        <label>接口协议<select aria-label="接口协议" value={provider.protocol} onChange={e => setProvider({...provider, protocol:e.target.value})}><option value="openai_compatible">OpenAI Chat Completions 兼容</option><option value="deepseek">DeepSeek</option></select></label>
        {field('Base URL（兼容接口通常包含 /v1）', provider.base_url, v => setProvider({...provider, base_url:v}))}
        {field('API Key（留空保留原值）', provider.api_key, v => setProvider({...provider, api_key:v}), 'password')}
        <label className="check"><input type="checkbox" checked={provider.remove_key} onChange={e => setProvider({...provider, remove_key:e.target.checked})}/>清除已保存的密钥</label>
        <div className="settings-actions"><button disabled={busy} onClick={saveProvider}>保存供应商</button><button disabled={busy || !provider.id} onClick={() => action(async () => {const r = await settingsApi('/providers/'+provider.id+'/test', {}); setStatus('连接成功；发现模型：'+r.models.join('、'));})}>检查连接并发现模型</button></div>
      </div>}
      {tab === 'models' && <div className="settings-form">
        <label>编辑模型<select aria-label="编辑模型" value={model.id || ''} onChange={e => {const m = models.find(m => m.id === e.target.value); if (m) {const {version, ...fields} = m; setModel(fields);} else setModel(blankModel);}}><option value="">＋ 新模型</option>{models.map(m => <option value={m.id} key={m.id}>{m.name} · v{m.version}</option>)}</select></label>
        {field('显示名称', model.name, v => setModel({...model, name:v}))}
        <label>供应商<select aria-label="供应商" value={model.provider_id} onChange={e => setModel({...model, provider_id:e.target.value})}><option value="">选择供应商</option>{providers.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label>
        {field('实际模型 ID（可手工填写）', model.model_id, v => setModel({...model, model_id:v}))}
        {field('模型上下文容量（K，1K = 1024 tokens）', model.context_window_tokens / 1024, v => setModel({...model, context_window_tokens:Math.round(Number(v)*1024)}), 'number')}
        {field('模型最大输出 tokens', model.max_output_tokens, v => setModel({...model, max_output_tokens:Number(v)}), 'number')}
        {[['images','支持图片输入'],['tools','支持工具调用'],['streaming','支持流式输出']].map(([key,label]) => <label className="check" key={key}><input type="checkbox" checked={model[key]} onChange={e => setModel({...model,[key]:e.target.checked})}/>{label}</label>)}
        <label>思考参数映射<select aria-label="思考参数映射" value={model.reasoning_parameter} onChange={e => setModel({...model, reasoning_parameter:e.target.value})}><option value="none">不发送思考参数</option><option value="deepseek">DeepSeek thinking + reasoning_effort</option><option value="reasoning_effort">reasoning_effort</option></select></label>
        <label>输出额度参数<select aria-label="输出额度参数" value={model.output_token_parameter || 'max_tokens'} onChange={e => setModel({...model, output_token_parameter:e.target.value})}><option value="max_tokens">max_tokens</option><option value="max_completion_tokens">max_completion_tokens</option></select></label>
        {field('关闭思考时的参数值（兼容接口，留空不发送）', model.reasoning_off_value || '', v => setModel({...model, reasoning_off_value:v || null}))}
        {field('声明支持的思考等级（逗号分隔，包含 off）', model.reasoning_levels.join(','), v => setModel({...model, reasoning_levels:v.split(',').map(x => x.trim())}))}
        <p className="settings-note">这里只声明该模型支持的能力。未实现的 Responses / Anthropic 协议不作为可用选项。</p><button disabled={busy} onClick={saveModel}>保存模型声明</button>
      </div>}
      {tab === 'context' && <div className="settings-form">
        <label>保存范围<select aria-label="保存范围" value={scope} onChange={e => setScope(e.target.value)}><option value="global">系统默认</option>{workspace && <option value="workspace">当前工作区</option>}{session && <option value="session">当前会话</option>}</select></label>
        <label>默认模型<select aria-label="默认模型" value={policy.model_profile_id || ''} onChange={e => setPolicy({...policy, model_profile_id:e.target.value})}>{models.map(m => <option key={m.id} value={m.id}>{m.name}</option>)}</select></label>
        {field('有效上下文预算（K）', (policy.effective_context_tokens || 0)/1024, v => setPolicy({...policy, effective_context_tokens:Math.round(Number(v)*1024)}), 'number')}
        {field('输出预留（tokens）', policy.output_reserve_tokens, v => setPolicy({...policy, output_reserve_tokens:Number(v)}), 'number')}
        {field('自动压缩触发比例（0.5–0.95）', policy.compression_trigger_ratio, v => setPolicy({...policy, compression_trigger_ratio:Number(v)}), 'number')}
        {field('压缩目标比例（小于触发比例）', policy.compression_target_ratio, v => setPolicy({...policy, compression_target_ratio:Number(v)}), 'number')}
        {field('至少保留的最近消息数', policy.keep_recent_messages, v => setPolicy({...policy, keep_recent_messages:Number(v)}), 'number')}
        {field('摘要中保留的最近图片数（其他图片保留引用）', policy.retained_images, v => setPolicy({...policy, retained_images:Number(v)}), 'number')}
        {field('每轮最多模型调用数', policy.max_model_calls, v => setPolicy({...policy, max_model_calls:Number(v)}), 'number')}
        {field('每轮执行超时（秒）', policy.timeout_seconds, v => setPolicy({...policy, timeout_seconds:Number(v)}), 'number')}
        {field('单个工具超时（秒，最多60）', policy.tool_timeout_seconds, v => setPolicy({...policy, tool_timeout_seconds:Number(v)}), 'number')}
        <p className="settings-note">容量是估算预算，包含消息、图片、系统提示、工具与输出预留。设置继承顺序：系统 → 工作区 → 会话 → 本次运行。</p><button disabled={busy} onClick={savePolicy}>保存上下文与执行设置</button>
      </div>}
      {tab === 'records' && <div>{!records.length && <p>当前会话还没有压缩记录。</p>}{records.map(r => <details key={r.id} className="tool-card"><summary>压缩 #{r.id} · {r.value.estimated_before_tokens} → {r.value.estimated_after_tokens} tokens（估算）</summary><pre>{r.value.summary}</pre><small>保留图片引用：{r.value.attachment_ids.join(', ') || '无'}</small></details>)}</div>}
      </div>
    </section>
  </div>;
}
