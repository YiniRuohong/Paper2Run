import { useMemo, useRef, useState } from 'react';
import { ApiError, buildCheckpoint, createApi, snapshotRefreshError } from '../../api-client/phase01';
import type { Action, Cell, Checkpoint, Event, Recovery, Session } from '../../api-client/phase01';
import './lab.css';

const initialCode = 'values = [2, 4, 6, 8]\nprint("Local kernel ready")\nprint("mean =", sum(values) / len(values))';
const time = (value: string) => new Date(value).toLocaleTimeString('zh-CN', { hour12: false });
const shortId = (id: string) => id.slice(0, 8);

async function collectSnapshot<T extends { sequence_no: number }>(read: (after: number) => Promise<T[]>): Promise<T[]> {
  const rows: T[] = [];
  let after = -1;
  for (let page = 0; page < 100; page++) {
    const batch = await read(after);
    rows.push(...batch);
    if (batch.length < 100) return rows;
    const next = Math.max(...batch.map((row) => row.sequence_no));
    if (next <= after) throw new Error('时间线游标未前进，请检查 API。');
    after = next;
  }
  throw new Error('时间线超过本 Demo 的 10,000 条快照上限，未使用截断数据创建 checkpoint。');
}

export default function Phase01Lab() {
  const [projectInput, setProjectInput] = useState('demo-project');
  const [actorInput, setActorInput] = useState('demo-user');
  const [context, setContext] = useState({ project: 'demo-project', actor: 'demo-user' });
  const api = useMemo(() => createApi(context.actor), [context.actor]);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [session, setSession] = useState<Session | null>(null);
  const [actions, setActions] = useState<Action[]>([]);
  const [events, setEvents] = useState<Event[]>([]);
  const [checkpoints, setCheckpoints] = useState<Checkpoint[]>([]);
  const [cell, setCell] = useState<Cell | null>(null);
  const [source, setSource] = useState(initialCode);
  const [checkpointName, setCheckpointName] = useState('baseline');
  const [checkpointId, setCheckpointId] = useState('');
  const [recovery, setRecovery] = useState<Recovery | null>(null);
  const [health, setHealth] = useState('未检查');
  const [busy, setBusy] = useState('');
  const lock = useRef(false);
  const [error, setError] = useState<Error | null>(null);
  const [notice, setNotice] = useState('本页始终 LIVE，不读取 Mock。打开项目或创建 session，开始本地验证。');
  const [updatedAt, setUpdatedAt] = useState('');
  const [tab, setTab] = useState<'events' | 'actions'>('events');
  const dirtyContext = projectInput.trim() !== context.project || actorInput.trim() !== context.actor;
  const active = session?.status === 'active';

  async function perform(label: string, operation: () => Promise<void>) {
    if (lock.current) return;
    lock.current = true;
    setBusy(label);
    setError(null);
    setNotice('');
    try { await operation(); }
    catch (reason) { setError(reason instanceof Error ? reason : new Error(String(reason))); }
    finally { lock.current = false; setBusy(''); }
  }

  function clearSession() {
    setSession(null); setCell(null); setActions([]); setEvents([]);
    setCheckpoints([]); setCheckpointId(''); setRecovery(null); setUpdatedAt('');
  }

  async function refresh(id: string) {
    const [current, nextActions, nextEvents, nextCheckpoints] = await Promise.all([
      api.session(id), collectSnapshot((after) => api.actions(id, after)),
      collectSnapshot((after) => api.events(id, after)), api.checkpoints(id),
    ]);
    setSession(current); setActions(nextActions); setEvents(nextEvents); setCheckpoints(nextCheckpoints);
    setCheckpointId((selected) => nextCheckpoints.some((item) => item.id === selected) ? selected : nextCheckpoints[0]?.id || '');
    setCell((previous) => previous && nextActions[nextActions.length - 1]?.cell_id === previous.id ? previous : null);
    setUpdatedAt(new Date().toLocaleTimeString('zh-CN', { hour12: false }));
  }

  async function refreshAfterWrite(id: string) {
    try { await refresh(id); }
    catch (reason) { throw snapshotRefreshError(reason); }
  }

  function openProject() {
    void perform('加载项目', async () => {
      const next = { project: projectInput.trim(), actor: actorInput.trim() };
      clearSession(); setSessions([]); setContext(next);
      const rows = await createApi(next.actor).sessions(next.project);
      setSessions(rows);
      setNotice(`已打开项目标识 ${next.project}，返回 ${rows.length} 个 session。此操作不创建项目记录。`);
    });
  }

  function createSession() {
    void perform('创建 session', async () => {
      const created = await api.createSession(context.project);
      clearSession(); setSession(created); setSessions((rows) => [created, ...rows]);
      setNotice(`Session ${shortId(created.id)} 已创建。`);
      await refreshAfterWrite(created.id);
    });
  }

  function executeCell() {
    if (!session || !source.trim()) return;
    const id = session.id;
    void perform('执行 cell', async () => {
      setRecovery(null); setCell(null);
      const result = await api.cell(id, source);
      setCell(result);
      setNotice(`Cell 返回状态：${result.status}。以输出和 error_code 为准。`);
      await refreshAfterWrite(id);
    });
  }

  function createCheckpoint() {
    if (!session || !cell) return;
    void perform('创建 checkpoint', async () => {
      const latestActions = await collectSnapshot<Action>((after) => api.actions(session.id, after));
      const payload = await buildCheckpoint(checkpointName.trim(), cell, latestActions);
      const created = await api.checkpoint(session.id, payload);
      setNotice(`Checkpoint ${created.name} 已创建；这是执行元数据记录，不是完整 Python 内存快照。`);
      await refreshAfterWrite(session.id);
      setCheckpointId(created.id);
    });
  }

  function restoreCheckpoint() {
    if (!session || !checkpointId) return;
    if (!window.confirm('Restore 可能重启本地 Kernel 并丢失当前变量。Phase 0/1 不保证内存变量重放。继续？')) return;
    void perform('执行 restore', async () => {
      setRecovery(null); setCell(null);
      const result = await api.restore(session.id, checkpointId);
      setRecovery(result);
      setNotice(`Restore 服务端状态：${result.status}。请检查 reasons；不代表训练或变量重放完成。`);
      await refreshAfterWrite(session.id);
    });
  }

  const disabled = Boolean(busy) || dirtyContext;
  return <div className="phase01-lab app-shell">
    <header className="topbar">
      <a className="brand" href="#workspace" aria-label="Paper2Run 工作台"><span className="brand-mark" aria-hidden="true">P₂R</span><span>Paper<em>2</em>Run</span></a>
      <span className="edition">REPRODUCTION LAB / 001</span>
      <a href="/mock/projects">返回四视图 MOCK</a><span className="demo-badge">LIVE · Phase 0/1</span>
    </header>
    <div className="apparatus" aria-label="当前实现范围">
      <span><b>01</b> 项目标识</span><i aria-hidden="true" />
      <span className={session ? 'ready' : ''}><b>02</b> Session</span><i aria-hidden="true" />
      <span className={cell ? 'ready' : ''}><b>03</b> 本地 Kernel</span><i aria-hidden="true" />
      <span><b>04</b> Checkpoint / Restore</span>
      <span className="future">论文 → 训练 → Report · 未实现</span>
    </div>
    <div className="safety-strip"><strong>非生产 sandbox</strong><span>完整产品 live 未接入；本页仅为 Session / Cell / Checkpoint / Restore 的真实 API。代码在本机执行。仅运行自己信任的代码；X-Actor-ID 仅作开发身份，不是生产鉴权。</span></div>
    <main id="workspace" className="workspace">
      <aside className="panel sidebar" aria-label="项目与会话">
        <div className="section-title"><span>01 / CONTEXT</span><span className="tiny">开发环境</span></div>
        <h1>实验工作台</h1><p className="muted">从一个 cell 开始，记录真实执行证据。</p>
        <form onSubmit={(event) => { event.preventDefault(); openProject(); }}>
          <label htmlFor="project">Project ID</label>
          <input id="project" value={projectInput} onChange={(event) => setProjectInput(event.target.value)} required maxLength={256} disabled={Boolean(busy)} spellCheck={false} />
          <label htmlFor="actor">开发身份 · X-Actor-ID</label>
          <input id="actor" value={actorInput} onChange={(event) => setActorInput(event.target.value)} required maxLength={256} disabled={Boolean(busy)} spellCheck={false} />
          <button className="wide" disabled={Boolean(busy) || !projectInput.trim() || !actorInput.trim()}>打开 / 刷新项目</button>
        </form>
        <p className="hint">无独立创建项目接口：使用项目标识建立会话，不伪造项目记录。切换身份会清空当前视图。</p>
        {dirtyContext && <p className="amber">身份或项目已修改，请先打开项目。</p>}
        <div className="section-title separated"><span>SESSIONS</span><span>{sessions.length.toString().padStart(2, '0')}</span></div>
        <button className="primary wide" disabled={disabled} onClick={createSession}>＋ 创建 session</button>
        <nav className="session-list" aria-label="会话列表">
          {sessions.length === 0 && <p className="empty">尚未加载会话。打开项目或创建新的 session。</p>}
          {sessions.map((item) => <button key={item.id} className={`session-card ${session?.id === item.id ? 'selected' : ''}`} aria-current={session?.id === item.id ? 'true' : undefined} disabled={disabled} onClick={() => void perform('选择 session', async () => { clearSession(); await refresh(item.id); setNotice('已加载会话。历史 cell 输出不在当前读取接口中，可查看事件快照。'); })}>
            <span className="mono">{shortId(item.id)} <span className="status">{item.status}</span></span><small>{new Date(item.created_at).toLocaleString('zh-CN')}</small>
          </button>)}
        </nav>
        <p className="hint">会话列表按后端返回范围展示。</p>
        <div className="connection"><span>API / <strong>{health}</strong></span><button disabled={Boolean(busy)} onClick={() => void perform('检查 API', async () => { setHealth('检查中'); try { const result = await api.health(); setHealth(result.status === 'ok' ? '在线' : result.status); } catch (reason) { setHealth('不可用'); throw reason; } })}>检查连接</button></div>
      </aside>

      <section className="center-column" aria-label="Cell 执行" aria-busy={Boolean(busy)}>
        <div className="workspace-heading"><div><span className="eyebrow">LOCAL EXECUTION / PYTHON</span><h2>让代码给出答案。</h2></div><button disabled={disabled || !session} onClick={() => session && void perform('刷新快照', async () => { await refresh(session.id); setNotice('Session、actions、events 和 checkpoints 快照已刷新。'); })}>↻ 刷新 session</button></div>
        <div className="feedback" aria-live="polite">
          {busy && <p className="loading">{busy}中… 请勿重复提交。</p>}
          {notice && <p className="notice">{notice}</p>}
        </div>
        {error && <div className="error-banner" role="alert"><strong>操作未完成</strong><p>{error.message}</p>
          {error instanceof ApiError && <><p className="mono">{error.status ? `HTTP ${error.status}` : 'NETWORK'} · {error.code || 'UNKNOWN'}{error.requestId ? ` · request_id: ${error.requestId}` : ''}</p>{error.status === 404 && <p>项目或会话不可用，请检查左侧 Project ID 与 X-Actor-ID；未创建任何假成功记录。</p>}{error.details != null && <details><summary>错误详情</summary><pre>{JSON.stringify(error.details, null, 2)}</pre></details>}</>}
        </div>}
        <section className="panel editor-panel">
          <div className="section-title"><span>02 / CELL EDITOR</span><span className="tiny">本地 Kernel · Python</span></div>
          <div className="session-meta"><span>{session ? `SESSION ${session.id}` : 'NO SESSION SELECTED'}</span><span>{session?.status || '等待会话'}</span></div>
          <label className="sr-only" htmlFor="source">Python cell 代码</label>
          <textarea id="source" className="code-editor" value={source} onChange={(event) => setSource(event.target.value)} spellCheck={false} disabled={Boolean(busy)} maxLength={256 * 1024} onKeyDown={(event) => { if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') { event.preventDefault(); if (!disabled && active) executeCell(); } }} />
          <div className="editor-footer"><span className="hint">{source.split('\n').length} 行 · Ctrl / ⌘ + Enter</span><button className="primary" disabled={disabled || !active || !source.trim()} onClick={executeCell}>▶ 执行 cell</button></div>
        </section>
        <section className="panel output-panel" aria-label="执行输出">
          <div className="section-title"><span>03 / EXECUTION OUTPUT</span>{cell && <span className={cell.status === 'succeeded' ? 'green' : 'red'}>{cell.status}</span>}</div>
          {!cell ? <div className="empty output-empty"><span className="terminal-glyph" aria-hidden="true">&gt;_</span><p>等待当前会话的执行结果</p><small>不填充演示输出。切换会话或 restore 后请重新执行。</small></div> : <>
            <div className="output-meta"><span>CELL {shortId(cell.id)}</span><time>{time(cell.created_at)}</time></div>
            {cell.error_code && <p className="red">error_code: {cell.error_code}</p>}
            <h3>stdout</h3><pre>{cell.stdout || '（无标准输出）'}</pre>
            <h3>stderr</h3><pre className={cell.stderr ? 'stderr' : ''}>{cell.stderr || '（无标准错误）'}</pre>
            <h3>result_summary</h3><pre>{cell.result_summary || '（无结果摘要）'}</pre>
            <details><summary>执行元数据与 artifact IDs</summary><pre>{JSON.stringify({ namespace_hash: cell.namespace_hash, environment_id: cell.environment_id, artifact_ids: cell.artifact_ids }, null, 2)}</pre></details>
          </>}
        </section>
        <div className="scope-note"><strong>本阶段边界</strong><p>仅接入 Session、Cell、事件及 Checkpoint API。论文解析、真实训练、Runner、Report 均未实现；不会生成模拟训练指标或成功报告。</p></div>
      </section>

      <aside className="right-column" aria-label="证据与恢复">
        <section className="panel checkpoint-panel"><div className="section-title"><span>04 / CHECKPOINT</span><span className="tiny">状态记录</span></div>
          <label htmlFor="checkpoint-name">Checkpoint 名称</label><input id="checkpoint-name" value={checkpointName} onChange={(event) => setCheckpointName(event.target.value)} maxLength={256} disabled={Boolean(busy)} />
          <button className="wide" disabled={disabled || !active || !cell || !checkpointName.trim()} onClick={createCheckpoint}>＋ 创建 checkpoint</button>
          <p className="hint">先执行 cell；使用真实 namespace hash、environment ID 与最新 action cursor。记录元数据，不承诺完整变量快照。</p>
          <label htmlFor="checkpoint">恢复目标 · {checkpoints.length} 条</label><select id="checkpoint" value={checkpointId} onChange={(event) => { setCheckpointId(event.target.value); setRecovery(null); }} disabled={disabled || !checkpoints.length}><option value="">选择 checkpoint</option>{checkpoints.map((item) => <option key={item.id} value={item.id}>{item.name} · {shortId(item.id)}</option>)}</select>
          {!checkpoints.length && <p className="empty compact">暂无 checkpoint。</p>}
          {checkpoints.filter((item) => item.id === checkpointId).map((item) => <dl className="checkpoint-detail" key={item.id}><dt>状态</dt><dd>{item.status}</dd><dt>Cursor</dt><dd>{item.action_cursor}</dd><dt>创建时间</dt><dd>{new Date(item.created_at).toLocaleString('zh-CN')}</dd><dt>Environment</dt><dd>{item.environment_id}</dd></dl>)}
          {checkpoints.length >= 100 && <p className="amber">当前仅展示接口返回的前 100 条 checkpoint。</p>}
          <button className="wide restore-button" disabled={disabled || !active || !checkpointId} onClick={restoreCheckpoint}>↶ Restore 选中记录</button>
          <p className="hint">可能重启 Kernel；不保证内存变量重放。服务端可返回 recovered、partial 或 failed。</p>
          {recovery && <div className={`recovery ${recovery.status === 'recovered' ? 'green' : 'red'}`} role="status"><strong>{recovery.status}</strong><pre>{recovery.reasons.length ? recovery.reasons.join('\n') : '服务端未返回 reasons；请执行代码核对变量。'}</pre></div>}
        </section>
        <section className="panel timeline-panel"><div className="section-title"><span>05 / EVIDENCE LOG</span><span className="tiny">快照刷新</span></div>
          <div className="timeline-tabs" aria-label="日志类型"><button aria-pressed={tab === 'events'} onClick={() => setTab('events')}>Events <span>{events.length}</span></button><button aria-pressed={tab === 'actions'} onClick={() => setTab('actions')}>Actions <span>{actions.length}</span></button></div>
          <p className="hint">非实时 SSE；点击“刷新 session”读取有限快照。{updatedAt ? `更新于 ${updatedAt}` : '尚未刷新'}</p>
          <ol className="timeline">{tab === 'events' ? events.slice(-100).map((item) => <li key={item.sequence_no}><span className="timeline-dot" /><div><div className="log-heading"><strong>{item.type}</strong><time>{time(item.created_at)}</time></div><small>#{item.sequence_no}</small><details><summary>事件 payload</summary><pre>{JSON.stringify(item.payload, null, 2)}</pre></details></div></li>) : actions.slice(-100).map((item) => <li key={item.sequence_no}><span className="timeline-dot" /><div><div className="log-heading"><strong>{item.kind}</strong><time>{time(item.created_at)}</time></div><p>#{item.sequence_no} · {item.status}</p><small>{item.actor_id} · {item.permission_decision}</small>{item.cell_id && <p className="mono">cell {shortId(item.cell_id)}</p>}</div></li>)}</ol>
          {(tab === 'events' ? events.length : actions.length) === 0 && <p className="empty">暂无{tab === 'events' ? '事件' : '动作'}。执行 cell 后刷新查看。</p>}
          {(tab === 'events' ? events.length : actions.length) > 100 && <p className="hint">画面展示最近 100 条；checkpoint 使用完整已读取游标。</p>}
        </section>
      </aside>
    </main>
    <footer className="page-footer"><span>Paper2Run / Phase 0 → Phase 1</span><span>本地 Kernel · 实际 API · 非生产 sandbox</span></footer>
  </div>;
}
