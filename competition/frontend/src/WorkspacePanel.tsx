import React, {useEffect, useRef, useState} from 'react';

type Workspace = {id: string; name: string};
type Entry = {name: string; kind: 'directory' | 'file' | 'blocked'; size: number};
type Preview = {path: string; size: number; text: string | null; reason: string | null};

async function request(url: string, body?: unknown) {
  const response = await fetch('/next/api' + url, body === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw Error(typeof data.detail === 'string' ? data.detail : '请求失败，请检查文件');
  return data;
}

function size(bytes: number) {
  return bytes < 1024 ? `${bytes} B` : bytes < 1024 * 1024 ? `${(bytes / 1024).toFixed(1)} KiB` : `${(bytes / 1024 / 1024).toFixed(1)} MiB`;
}

export function WorkspacePanel({workspace, onImported, onClose}: {
  workspace?: Workspace; onImported: (workspace: Workspace) => void; onClose: () => void;
}) {
  const [path, setPath] = useState('');
  const [entries, setEntries] = useState<Entry[]>([]);
  const [selected, setSelected] = useState('');
  const [preview, setPreview] = useState<Preview | null>(null);
  const [archive, setArchive] = useState<File | null>(null);
  const [name, setName] = useState('');
  const [status, setStatus] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  const panel = useRef<HTMLDivElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const base = workspace ? '/workspaces/' + workspace.id : '';

  useEffect(() => {
    const before = document.activeElement as HTMLElement | null;
    panel.current?.querySelector<HTMLButtonElement>('button')?.focus();
    return () => before?.focus();
  }, []);
  useEffect(() => {setPath(''); setSelected(''); setPreview(null); setEntries([]);}, [workspace?.id]);
  useEffect(() => {
    if (!workspace) return;
    let valid = true;
    async function refresh() {
      try {
        const [listing, content] = await Promise.all([
          request(base + '/files?path=' + encodeURIComponent(path)),
          selected ? request(base + '/file?path=' + encodeURIComponent(selected)).catch(e => ({
            path: selected, size: 0, text: null, reason: e instanceof Error ? e.message : '文件已不可用',
          })) : Promise.resolve(null),
        ]);
        if (valid) {setEntries(listing.entries); setPreview(content);}
      } catch (e) {if (valid) setError(e instanceof Error ? e.message : '文件读取失败');}
    }
    refresh();
    const timer = window.setInterval(refresh, 5000);
    return () => {valid = false; window.clearInterval(timer);};
  }, [workspace?.id, path, selected, revision]);

  function navigate(value: string) {
    setPath(value); setSelected(''); setPreview(null); setEntries([]); setError('');
  }
  async function importProject(event: React.FormEvent) {
    event.preventDefault();
    if (!archive || !name.trim() || busy) return;
    if (archive.size > 8 * 1024 * 1024) {setError('ZIP 文件不能超过 8 MiB'); return;}
    setBusy(true); setError(''); setStatus('正在导入项目…');
    try {
      const data = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result).split(',')[1]);
        reader.onerror = () => reject(Error('无法读取选中的文件'));
        reader.readAsDataURL(archive);
      });
      const result = await request('/workspaces/import', {name: name.trim(), data_base64: data});
      onImported(result.workspace);
      setStatus(`已导入 ${result.file_count} 个文件，共 ${size(result.total_bytes)}。关闭文件面板后即可新建会话处理这个项目。`);
      setArchive(null); setName('');
      if (fileInput.current) fileInput.current.value = '';
    } catch (e) {setStatus(''); setError(e instanceof Error ? e.message : '导入失败');}
    finally {setBusy(false);}
  }
  async function download(url: string, filename: string) {
    if (busy) return;
    setBusy(true); setError('');
    try {
      const response = await fetch('/next/api' + url);
      if (!response.ok) {
        const data = await response.json(); throw Error(data.detail || '下载失败');
      }
      const link = document.createElement('a');
      const objectUrl = URL.createObjectURL(await response.blob());
      link.href = objectUrl; link.download = filename; document.body.append(link); link.click(); link.remove();
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 10000);
    } catch (e) {setError(e instanceof Error ? e.message : '下载失败');}
    finally {setBusy(false);}
  }
  function keyboard(event: React.KeyboardEvent) {
    if (event.key === 'Escape' && !busy) {event.stopPropagation(); onClose();}
    if (event.key !== 'Tab') return;
    const focusable = panel.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), [tabindex="0"]');
    if (!focusable?.length) return;
    const first = focusable[0], last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {event.preventDefault(); last.focus();}
    if (!event.shiftKey && document.activeElement === last) {event.preventDefault(); first.focus();}
  }
  return <div className="settings-overlay" role="dialog" aria-modal="true" aria-label="工作区文件" onKeyDown={keyboard}>
    <div className="settings-panel workspace-panel" ref={panel}>
      <header><strong>工作区文件{workspace ? ' · ' + workspace.name : ''}</strong><button disabled={busy} onClick={onClose} aria-label="关闭工作区文件">×</button></header>
      <div className="workspace-content">
        {error && <p className="error" role="alert">{error}</p>}
        {status && <p className="settings-status" role="status">{status}</p>}
        <details className="project-import" open={!workspace || !!archive}>
          <summary>＋ 从 ZIP 导入项目</summary>
          <form onSubmit={importProject} className="import-form">
            <label>项目 ZIP<input ref={fileInput} type="file" accept=".zip,application/zip" disabled={busy} onChange={e => {
              const file = e.target.files?.[0] || null; setArchive(file); setName(file?.name.replace(/\.zip$/i, '') || ''); setError('');
            }}/></label>
            <label>新工作区名称<input aria-label="导入工作区名称" value={name} maxLength={80} required disabled={busy} onChange={e => setName(e.target.value)}/></label>
            <button disabled={busy || !archive || !name.trim()} type="submit">{busy ? '处理中…' : '导入为新工作区'}</button>
          </form>
          <p className="settings-note">保留 ZIP 目录结构，导入到新工作区。ZIP 最多 8 MiB，解压后最多 32 MiB / 1000 项；请移除 .git、依赖目录和链接。</p>
        </details>
        {workspace ? <>
          <div className="file-toolbar"><strong>/{path}</strong><button disabled={!path} onClick={() => navigate(path.split('/').slice(0, -1).join('/'))}>上一级</button><button onClick={() => {setError(''); setRevision(v => v + 1);}}>刷新文件</button><button disabled={busy} onClick={() => download(base + '/archive', workspace.name + '.zip')}>导出工作区 ZIP</button></div>
          <p className="settings-note">文件由此工作区的所有会话共享，每 5 秒更新。ZIP 导出仅包含普通文件和目录。</p>
          <div className="file-browser"><div className="file-list" aria-label="工作区目录">
            {entries.map(entry => {
              const relative = path ? path + '/' + entry.name : entry.name;
              return <button key={entry.name} className={selected === relative ? 'selected' : ''} disabled={entry.kind === 'blocked'} onClick={() => {
                setError('');
                if (entry.kind === 'directory') navigate(relative);
                else {setPreview(null); setSelected(relative);}
              }}><span>{entry.kind === 'directory' ? '▸' : entry.kind === 'blocked' ? '⊘' : '·'} {entry.name}</span><small>{entry.kind === 'file' ? size(entry.size) : entry.kind === 'blocked' ? '链接或特殊文件' : '目录'}</small></button>;
            })}
            {!entries.length && <p className="settings-note">此目录暂时没有文件</p>}
          </div><section className="file-preview" aria-label="文件预览">
            {selected ? <><div className="preview-heading"><strong>{selected}</strong><button disabled={busy} onClick={() => download(base + '/download?path=' + encodeURIComponent(selected), selected.split('/').at(-1)!)}>下载文件</button></div>{preview ? preview.text !== null ? <pre tabIndex={0}>{preview.text || '（空文件）'}</pre> : <p className="settings-note">{preview.reason}</p> : <p className="settings-note">正在读取文件…</p>}</> : <p className="settings-note">选择文件查看内容，或下载生成的结果。</p>}
          </section></div>
        </> : <p className="settings-note">先创建工作区，或从 ZIP 导入现有项目。</p>}
      </div>
    </div>
  </div>;
}
