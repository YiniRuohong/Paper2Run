import { useEffect, useRef, useState, type ReactNode } from 'react';
import { createAutonomousApi, type AutonomousTask, type AutonomousEvent, type AutonomousFiles, type ProjectConfig, type Capability, type ModelProfile } from '../../api-client/autonomous';
import { Workbench, type WbContent, type WbFile } from '../../ui/workbench/Workbench';
import { buildStages, toWbFiles } from './stages';
import { GuidedApiError, type GuidedApi } from '../../api-client/guided';
import { defaultConfig, mergeEvents, modelReference, parseProtocol, profileRequest, protocolApproval, researchTaskTypes, reusableProfiles, stateLabel, terminalState, validateConfig } from './config';
import './autonomous.css';

const api = createAutonomousApi();
const pretty = (value: unknown) => JSON.stringify(value, null, 2);
const detail = (error: unknown) => error instanceof GuidedApiError ? `${error.message} (${error.code}${error.requestId ? ` · ${error.requestId}` : ''})` : error instanceof Error ? error.message : String(error);
function Field({ title, children }: { title: string; children: ReactNode }) { return <label className="auto-field"><span>{title}</span>{children}</label>; }
function Section({ title, children }: { title: string; children: ReactNode }) { return <section className="ui-card auto-section"><h2>{title}</h2>{children}</section>; }
interface Paper { id: string; version: string; title: string }
export default function AutonomousPage({ guided, active = true }: { guided: GuidedApi; active?: boolean }) {
  const query = new URLSearchParams(window.location.hash.split('?')[1] ?? '');
  const [projectId, setProjectId] = useState(query.get('project') ?? '');
  const [projects, setProjects] = useState<{ id: string; name: string }[]>([]);
  const [papers, setPapers] = useState<Paper[]>([]);
  const [paperVersion, setPaperVersion] = useState('');
  const [config, setConfig] = useState<ProjectConfig>(() => defaultConfig(projectId));
  const [version, setVersion] = useState<number>();
  const [skills, setSkills] = useState<Capability[]>([]);
  const [profiles, setProfiles] = useState<ModelProfile[]>([]);
  const [profile, setProfile] = useState<ModelProfile>({ id: 'research-model', base_url: '', model: '', api: 'openai-completions', auth_mode: 'api-key' });
  const [key, setKey] = useState('');
  const [source, setSource] = useState('');
  const [protocol, setProtocol] = useState('');
  const [authorized, setAuthorized] = useState(false);
  const [tasks, setTasks] = useState<AutonomousTask[]>([]);
  const [taskId, setTaskId] = useState(query.get('task') ?? '');
  const [task, setTask] = useState<AutonomousTask | null>(null);
  const [events, setEvents] = useState<AutonomousEvent[]>([]);
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const generation = useRef(0);
  const taskScope = useRef({ id: taskId, projectId, generation: 0 });
  if (taskScope.current.id !== taskId || taskScope.current.projectId !== projectId) {
    taskScope.current = { id: taskId, projectId, generation: taskScope.current.generation + 1 };
  }
  async function controlTask(id: string, action: 'pause' | 'resume' | 'stop' | 'message', text?: string) {
    const scope = taskScope.current;
    const result = await api.control(id, action, text);
    if (taskScope.current !== scope || taskScope.current.id !== id || result.id !== id) return;
    setTask(result);
    setTasks(old => old.map(item => item.id === result.id ? result : item));
    if (action === 'message') setMessage('');
  }
  async function act(work: () => Promise<void>) {
    setBusy(true); setError(''); setNotice('');
    try { await work(); } catch (e) { setError(detail(e)); } finally { setBusy(false); }
  }
  useEffect(() => {
    if (!active) return;
    let alive = true;
    api.initializeSession().then(() => Promise.all([guided.readArray('/projects', v => v as { id: string; name?: string }), api.capabilities(), api.profiles()])).then(([p, s, m]) => {
      if (!alive) return;
      setProjects(p.items.map(x => ({ id: x.id, name: x.name ?? x.id }))); setSkills(s); setProfiles(m);
      if (!projectId && p.items.length) setProjectId(p.items[p.items.length - 1].id);
    }).catch(e => { if (alive) setError(detail(e)); });
    return () => { alive = false; };
  }, [guided, active]);
  useEffect(() => {
    const run = ++generation.current;
    setConfig(defaultConfig(projectId)); setVersion(undefined); setPapers([]); setPaperVersion(''); setTasks([]); setAuthorized(false); setProtocol(''); setMessage('');
    if (!projectId) return;
    setLoading(true); setError('');
    Promise.all([
      api.config(projectId).catch(e => { if (e instanceof GuidedApiError && e.status === 404) return null; throw e; }),
      guided.readArray(`/projects/${encodeURIComponent(projectId)}/papers`, v => v as Record<string, unknown>),
      api.tasks(projectId),
    ]).then(async ([saved, p, t]) => {
      // The existing paper detail API contains the immutable version identifier.
      const resolved = await Promise.all(p.items.map(async row => {
        const id = String(row.paper_id ?? row.id ?? '');
        const info = await guided.read(`/papers/${encodeURIComponent(id)}`, v => v as Record<string, unknown>);
        return { id, version: String(info.paper_version_id ?? info.current_version_id ?? ''), title: String(info.title ?? row.title ?? id) };
      }));
      if (generation.current !== run) return;
      if (saved) { setConfig(saved.config); setVersion(saved.version); }
      setPapers(resolved); setPaperVersion(resolved.find(paper => paper.version)?.version ?? ''); setTasks(t);
    }).catch(e => { if (generation.current === run) setError(detail(e)); }).finally(() => { if (generation.current === run) setLoading(false); });
    return () => { generation.current++; };
  }, [projectId, guided, active]);
  useEffect(() => {
    setTask(null); setEvents([]);
    if (!taskId) return;
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>; let cursor = 0;
    async function poll() {
      try {
        const scope = taskScope.current;
        const [current, stream] = await Promise.all([api.task(taskId, controller.signal), api.events(taskId, cursor, controller.signal)]);
        if (controller.signal.aborted || taskScope.current !== scope || current.id !== scope.id || (scope.projectId && current.project_id !== scope.projectId)) return;
        setTask(current); setTasks(old => old.map(item => item.id === current.id ? current : item));
        setEvents(old => mergeEvents(old, stream.events)); cursor = stream.next_cursor;
        if (!projectId) setProjectId(current.project_id);
      } catch (e) { if (!controller.signal.aborted) setError(detail(e)); }
      if (!controller.signal.aborted) timer = setTimeout(poll, 2000);
    }
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [taskId]);
  // Workbench state. Appended after every existing hook so earlier hook order is unchanged.
  const [view, setView] = useState<'workbench' | 'setup'>(query.get('task') ? 'workbench' : 'setup');
  const [files, setFiles] = useState<AutonomousFiles | null>(null);
  const [filesError, setFilesError] = useState('');
  const [filesTick, setFilesTick] = useState(0);
  const [reuseCredentials, setReuseCredentials] = useState('');
  // New frozen evidence or a finished tool call may have produced files: re-list.
  const fileSignal = events.filter(e => e.type === 'artifact_frozen' || e.type === 'tool_completed' || e.type === 'report_created').length;
  useEffect(() => {
    setFilesError('');
    if (!taskId) { setFiles(null); return; }
    const controller = new AbortController();
    Promise.resolve().then(() => api.files(taskId, controller.signal)).then(result => {
      if (!controller.signal.aborted && taskScope.current.id === taskId) setFiles(result);
    }).catch(e => {
      if (controller.signal.aborted) return;
      setFiles(old => old ?? { items: [], truncated: false, workspace_available: false }); setFilesError(`文件列表读取失败：${detail(e)}`);
    });
    return () => controller.abort();
  }, [taskId, filesTick, fileSignal]);
  function selectTask(id: string) { if (id) setView('workbench'); setTaskId(id); window.history.replaceState(null, '', `#/autonomous?project=${encodeURIComponent(projectId)}&task=${encodeURIComponent(id)}`); }
  const approval = protocolApproval(task?.pending_decision);
  const limits = config.limits;
  const chosenSkills = new Set(config.harness.skills);
  const specialists = config.specialists ?? [];
  const credentialSources = reusableProfiles(profile, profiles);
  const update = <K extends keyof ProjectConfig>(name: K, value: ProjectConfig[K]) => setConfig(old => ({ ...old, [name]: value }));
  async function saveConfig() {
    validateConfig(config);
    const saved = await api.saveConfig(projectId, config, version);
    setVersion(saved.version); setConfig(saved.config); return saved;
  }
  const timeline = buildStages(task, events);
  const eventsLog = events.map(e => `#${e.sequence}  ${e.created_at}  ${e.type}  ${JSON.stringify(e.payload)}`).join('\n');
  const records = new Map<string, string>();
  if (task && events.length) records.set('logs/events.log', eventsLog);
  if (task?.report != null) records.set('task/report.json', pretty(task.report));
  if (task) records.set('task/config.json', pretty(task.config_snapshot));
  const sources = new Map((files?.items ?? []).map(f => [f.path, f.source]));
  const wbFiles: WbFile[] | null = !taskId ? [] : files === null ? null : [
    ...toWbFiles(files.items),
    ...[...records].map(([path, body]) => ({ path, bytes: new TextEncoder().encode(body).length, mediaType: path.endsWith('.json') ? 'application/json' : 'text/plain', badge: '记录' })),
  ];
  const loadFile = async (file: WbFile, signal: AbortSignal): Promise<WbContent> => {
    const body = records.get(file.path);
    if (body !== undefined) return { text: body };
    const source = sources.get(file.path);
    if (!source || !taskId) throw new Error('该文件已不在当前任务中');
    return { blob: await api.fileBlob(taskId, { source, path: file.path }, signal) };
  };
  const usage = (task?.usage ?? {}) as Record<string, unknown>;
  const usageNum = (k: string) => typeof usage[k] === 'number' ? (usage[k] as number).toLocaleString('zh-CN') : '—';
  const modelCalls = usageNum('calls') !== '—' ? usageNum('calls') : usageNum('model_calls');
  const running = task ? !terminalState(task.state) && task.state !== 'paused' : false;
  return <div className="autonomous">
    <div className="auto-heading">
      <div><h1>自主实验</h1><p className="ui-help">模型与科研 Skills 推进实验，任务过程与证据持续记录。</p></div>
      <div className="auto-switch" role="tablist" aria-label="自主实验视图">
        <button type="button" role="tab" aria-selected={view === 'workbench'} className={view === 'workbench' ? 'is-on' : ''} onClick={() => setView('workbench')}>任务工作台</button>
        <button type="button" role="tab" aria-selected={view === 'setup'} className={view === 'setup' ? 'is-on' : ''} onClick={() => setView('setup')}>配置与启动</button>
      </div>
      <a className="btn btn-secondary btn-sm" href="#/new">引导式任务</a>
    </div>
    {error && <div className="auto-alert" role="alert">{error}<button className="btn btn-sm" onClick={() => window.location.reload()}>重新读取</button></div>}
    {notice && <p role="status">{notice}</p>}
    <div className="auto-setup" hidden={view !== 'setup'}>
    <Section title="项目与论文">
      <Field title="项目"><select value={projectId} disabled={busy} onChange={e => { setProjectId(e.target.value); setTaskId(''); window.history.replaceState(null, '', `#/autonomous?project=${encodeURIComponent(e.target.value)}`); }}><option value="">选择已有项目</option>{projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select></Field>
      <a className="btn btn-secondary" href="#/new?return=autonomous">创建项目 / 上传 PDF / 粘贴论文</a>
      <p className="ui-help">打开现有论文流程，在「上传论文」步骤创建项目并上传 PDF 或粘贴正文；PDF 沿用真实解析模式选择与进度。解析完成后点击顶部「返回自主实验并读取论文」，在此选择项目和论文版本，无需继续旧流程的模型抽取。</p>
      {loading && <p role="status">读取项目配置与论文…</p>}
      <Field title="论文版本"><select value={paperVersion} disabled={loading || busy} onChange={e => setPaperVersion(e.target.value)}><option value="">请选择已上传的论文版本</option>{papers.map(p => <option key={p.id} value={p.version} disabled={!p.version}>{p.title} · {p.version || '版本不可用'}</option>)}</select></Field>
      <Field title="实验目标"><textarea rows={3} value={config.experiment.objective} onChange={e => update('experiment', { ...config.experiment, objective: e.target.value })} placeholder="描述待验证的问题、期望指标和允许的实验范围" /></Field>
      <details><summary>实验协议 JSON（可选）</summary><p className="ui-help">填写冻结的指标、数据划分与授权范围；越界变更需版本记录并暂停。</p><Field title="协议对象"><textarea rows={6} value={protocol} onChange={e => setProtocol(e.target.value)} placeholder={'{"metrics": ["accuracy"]}'} /></Field></details>
    </Section>
    <Section title="主模型与模型连接">
      <Field title="主模型（编排器）"><select value={config.model.provider_profile} disabled={busy || loading} onChange={e => update('model', modelReference(profiles.find(p => p.id === e.target.value)))}><option value="">请选择已有模型配置</option>{config.model.provider_profile && !profiles.some(p => p.id === config.model.provider_profile) && <option value={config.model.provider_profile}>{config.model.provider_profile}（配置不可用，请重新连接）</option>}{profiles.map(p => <option key={p.id} value={p.id}>{p.id} · {p.model}</option>)}</select></Field>
      <p className="ui-help">主模型负责实验编排，根据任务从下方专家白名单动态选择咨询对象；未配置专家时保持单模型执行。</p>
      <div className="auto-grid">
        <Field title="配置 ID"><input value={profile.id} disabled={busy} onChange={e => setProfile({ ...profile, id: e.target.value })} /></Field>
        <Field title="API 协议"><select value={profile.api} disabled={busy} onChange={e => { setProfile({ ...profile, api: e.target.value as ModelProfile['api'] }); setReuseCredentials(''); }}><option value="openai-completions">OpenAI Completions</option><option value="anthropic-messages">Anthropic Messages</option></select></Field>
        <Field title="Base URL"><input type="url" value={profile.base_url} disabled={busy} onChange={e => { setProfile({ ...profile, base_url: e.target.value }); setReuseCredentials(''); }} placeholder="https://provider.example/v1" /></Field>
        <Field title="模型名称"><input value={profile.model} disabled={busy} onChange={e => setProfile({ ...profile, model: e.target.value })} /></Field>
        <Field title="认证方式"><select value={profile.auth_mode ?? 'api-key'} disabled={busy} onChange={e => { setProfile({ ...profile, auth_mode: e.target.value as ModelProfile['auth_mode'] }); setKey(''); setReuseCredentials(''); }}><option value="api-key">API Key（默认）</option><option value="none">无认证（仅 HTTP 回环地址）</option></select></Field>
        {profile.auth_mode !== 'none' && <>
          <Field title="复用已有配置凭据（同端点 / 协议）"><select value={reuseCredentials} disabled={busy} onChange={e => { setReuseCredentials(e.target.value); setKey(''); }}><option value="">不复用 / 输入新密钥</option>{credentialSources.map(p => <option key={p.id} value={p.id}>{p.id} · {p.model}</option>)}</select></Field>
          <Field title="API Key（仅本次内存）"><input type="password" autoComplete="off" value={key} disabled={busy || !!reuseCredentials} onChange={e => setKey(e.target.value)} /></Field>
        </>}
      </div>
      <p className="ui-help">密钥仅保存在当前 React 内存中，保存后清空，不写入浏览器存储或 URL；项目配置仅保存 credential_ref。复用时只提交源配置 ID，由服务端校验同一用户、Base URL 与 API 协议；不会读取或展示已有密钥。无认证仅适用于 HTTP localhost / 回环地址。保存不代表连接已验证。</p>
      <div className="auto-actions">{[false, true].map(useAsMain => <button key={String(useAsMain)} className="btn btn-secondary" disabled={busy || !profile.id || !profile.model || !profile.base_url} onClick={() => void act(async () => {
        const saved = await api.saveProfile(profileRequest(profile, key, reuseCredentials, profiles)); setKey(''); setReuseCredentials('');
        setProfiles(old => [...old.filter(p => p.id !== saved.id), saved]);
        if (useAsMain) update('model', modelReference(saved));
        setNotice('模型配置已保存，可供主模型或专家选择；运行时将检查实际连接。');
      })}>{useAsMain ? '保存并用作主模型' : '保存模型连接'}</button>)}</div>
    </Section>
    <Section title="科研专家白名单（可选）">
      <p className="ui-help">当前 {specialists.length} / 16 位专家。主模型自主分配咨询，无需逐次选择专家或确认。专家输出仅建议；代码 / 实验执行及可信评估仍走主模型受控工具，所有主模型与专家调用共享下方总预算。专家的科研审查不等于可信评估。</p>
      {specialists.length === 0 && <p className="ui-help">尚未添加专家：使用原有单模型模式。</p>}
      {specialists.map((specialist, index) => <fieldset className="auto-specialist" key={index} disabled={busy || loading}>
        <legend>专家 {index + 1}</legend>
        <div className="auto-grid">
          <Field title="专家 ID"><input value={specialist.id} maxLength={128} onChange={e => update('specialists', specialists.map((item, i) => i === index ? { ...item, id: e.target.value } : item))} placeholder="例如 method-advisor" /></Field>
          <Field title="专家模型配置"><select value={specialist.model.provider_profile} onChange={e => update('specialists', specialists.map((item, i) => i === index ? { ...item, model: modelReference(profiles.find(p => p.id === e.target.value)) } : item))}><option value="">请选择已有模型配置</option>{specialist.model.provider_profile && !profiles.some(p => p.id === specialist.model.provider_profile) && <option value={specialist.model.provider_profile}>{specialist.model.provider_profile}（配置不可用，请重新连接）</option>}{profiles.map(p => <option key={p.id} value={p.id}>{p.id} · {p.model}</option>)}</select></Field>
        </div>
        <Field title="专长描述（供主模型选择专家）"><textarea rows={3} maxLength={8192} value={specialist.description} onChange={e => update('specialists', specialists.map((item, i) => i === index ? { ...item, description: e.target.value } : item))} placeholder="描述擅长的领域、方法和适用问题" /></Field>
        <fieldset className="auto-specialist-types"><legend>可咨询任务类型（至少一种）</legend>{researchTaskTypes.map(type => <label className="auto-check" key={type.value}><input type="checkbox" checked={specialist.task_types.includes(type.value)} onChange={e => update('specialists', specialists.map((item, i) => i === index ? { ...item, task_types: e.target.checked ? [...item.task_types.filter(value => value !== type.value), type.value] : item.task_types.filter(value => value !== type.value) } : item))} />{type.label}</label>)}</fieldset>
        <button className="btn btn-sm btn-secondary" onClick={() => update('specialists', specialists.filter((_, i) => i !== index))}>删除专家 {index + 1}</button>
      </fieldset>)}
      <button className="btn btn-secondary" disabled={busy || loading || specialists.length >= 16} onClick={() => {
        let suffix = 1; while (specialists.some(item => item.id === `specialist-${suffix}`)) suffix++;
        update('specialists', [...specialists, { id: `specialist-${suffix}`, description: '', task_types: [], model: modelReference() }]);
      }}>增加科研专家</button>
    </Section>
    <Section title="执行目标与预算">
      <div className="auto-grid">
        <Field title="执行位置"><select value={config.target.kind} onChange={e => { const { ssh_config_alias, remote_workspace, ...common } = config.target; update('target', e.target.value === 'ssh' ? { ...common, kind: 'ssh', ssh_config_alias: ssh_config_alias ?? '', remote_workspace: remote_workspace ?? `/workspace/paper2run/${projectId}` } : { ...common, kind: 'local' }); }}><option value="local">本地隔离环境</option><option value="ssh">SSH 隔离环境</option></select></Field>
        <Field title="目标配置名称"><input value={config.target.profile} onChange={e => update('target', { ...config.target, profile: e.target.value })} /></Field>
        <Field title="本地项目工作区"><input value={config.project.workspace} onChange={e => update('project', { ...config.project, workspace: e.target.value })} /></Field>
        {config.target.kind === 'ssh' && <><Field title="SSH config 别名"><input value={config.target.ssh_config_alias ?? ''} onChange={e => update('target', { ...config.target, ssh_config_alias: e.target.value })} /></Field><Field title="远程工作区"><input value={config.target.remote_workspace ?? ''} onChange={e => update('target', { ...config.target, remote_workspace: e.target.value })} /></Field></>}
        {(['wall_time_seconds', 'max_model_calls', 'max_model_tokens', 'storage_bytes'] as const).map((name, i) => <Field key={name} title={['最长运行时间（秒）', '模型调用上限', '模型 Token 上限', '存储上限（字节）'][i]}><input type="number" min="1" step="1" value={limits[name]} onChange={e => update('limits', { ...limits, [name]: Number(e.target.value) })} /></Field>)}
        <Field title="GPU 设备（逗号分隔，可留空）"><input value={limits.gpu_devices.join(',')} onChange={e => update('limits', { ...limits, gpu_devices: e.target.value.split(',').map(s => s.trim()).filter(Boolean) })} /></Field>
      </div>
      <p className="ui-help">目标须专用且强制隔离，镜像 paper2run-experiment:1。允许实验环境访问公网，禁止访问宿主与元数据服务。SSH 使用本机预配置别名与凭据，界面不收集私钥或密码；请先配置 known_hosts 与非交互认证。尚未验证真实 SSH / GPU 可用性，最终能力以运行事件为准。</p>
    </Section>
    <Section title="科研 Skills">
      {skills.length === 0 && <p className="ui-help">暂无可用 Skills。</p>}
      {skills.map(s => <div className="auto-skill" key={s.id}><label><input type="checkbox" checked={chosenSkills.has(s.id)} disabled={!s.enabled || busy} onChange={e => update('harness', { ...config.harness, skills: e.target.checked ? [...config.harness.skills, s.id] : config.harness.skills.filter(id => id !== s.id) })} /> {s.name} <code>{s.version}</code><span className="ui-help">{s.description}</span></label><button className="btn btn-sm btn-secondary" disabled={busy} onClick={() => void act(async () => { await api.enableSkill(s.id, !s.enabled); setSkills(await api.capabilities()); if (s.enabled) update('harness', { ...config.harness, skills: config.harness.skills.filter(id => id !== s.id) }); })}>{s.enabled ? '禁用' : '启用'}</button></div>)}
      <Field title="导入 Skill 的本地源路径"><input value={source} onChange={e => setSource(e.target.value)} placeholder="本机服务可读取的目录" /></Field>
      <button className="btn btn-secondary" disabled={busy || !source.trim()} onClick={() => void act(async () => { await api.importSkill(source.trim()); setSkills(await api.capabilities()); setSource(''); setNotice('Skill 已导入，版本与启用状态见列表。'); })}>导入 Skill</button>
    </Section>
    <Section title="授权与启动">
      <label className="auto-check"><input type="checkbox" checked={authorized} onChange={e => setAuthorized(e.target.checked)} />我授权在上述目标与预算内联网、获取数据与代码、安装依赖和运行实验；超出协议授权的变更需暂停。</label>
      <p className="ui-help">保存会包含主模型、专家白名单与总预算。重新加载会以服务端已保存配置覆盖当前编辑。</p>
      <div className="auto-actions"><button className="btn btn-secondary" disabled={busy || loading || !projectId} onClick={() => void act(async () => { validateConfig(config); setNotice('配置校验通过；实际连接与凭据权限仍由服务端检查。'); })}>校验配置</button><button className="btn btn-secondary" disabled={busy || loading || !projectId} onClick={() => void act(async () => { const [saved, available] = await Promise.all([api.config(projectId), api.profiles()]); setConfig(saved.config); setVersion(saved.version); setProfiles(available); setAuthorized(false); setKey(''); setReuseCredentials(''); setNotice('已重新加载项目配置与模型连接。'); })}>重新加载配置</button><button className="btn btn-secondary" disabled={busy || loading || !projectId} onClick={() => void act(async () => { await saveConfig(); setNotice('项目配置已保存。'); })}>保存配置{version !== undefined ? ` · v${version}` : ''}</button><button className="btn btn-primary" disabled={busy || loading || !authorized || !projectId || !paperVersion} onClick={() => void act(async () => { const parsed = parseProtocol(protocol); await saveConfig(); const created = await api.start(projectId, { objective: config.experiment.objective, ...(paperVersion ? { paper_version_id: paperVersion } : {}), ...(parsed ? { protocol: parsed } : {}), authorization: { confirmed: true } }); setTasks(old => [created, ...old]); selectTask(created.id); })}>{busy ? '处理中…' : '启动自主实验'}</button></div>
    </Section>
    </div>
    <div hidden={view !== 'workbench'}>
      <Workbench
        eyebrow={<>自主实验{task ? <> · <span className="ui-mono">{task.id}</span></> : null}</>}
        title={task?.objective ?? '选择或启动一个自主实验任务'}
        status={task ? { text: stateLabel(task.state), tone: task.state === 'failed' ? 'bad' : terminalState(task.state) ? 'ok' : task.state === 'paused' || task.pending_decision != null ? 'warn' : 'run' } : null}
        meta={task ? <><span>更新于 {task.updated_at || '—'}</span><span>模型调用 {modelCalls}</span><span>Token {usageNum('total_tokens') !== '—' ? usageNum('total_tokens') : usageNum('model_tokens')}</span></> : <span>任务的阶段、命令输出和产物会在这里持续更新。</span>}
        progress={`进度 · ${timeline.stages.filter(st => st.mark === 'done').length} / ${timeline.stages.length}`}
        stages={timeline.stages}
        activeStageId={task ? timeline.active : null}
        files={wbFiles}
        filesError={filesError || null}
        filesEmpty={task ? '还没有文件。实验写入工作区或冻结证据后会出现在这里。' : '选择任务后显示它的工作区与证据文件。'}
        filesNote={files ? <>「证据」是已冻结并按 SHA-256 校验的产物{files.workspace_available ? '；workspace 为本机隔离工作区的实时文件' : '；远程工作区不可浏览，仅显示冻结证据'}{files.truncated ? '；文件过多，仅列出前 2000 个' : ''}。</> : null}
        onRefreshFiles={taskId ? () => setFilesTick(x => x + 1) : undefined}
        loadFile={loadFile}
      >
        <div className="auto-live">
          <div className="auto-taskbar">
            <Field title="项目任务"><select value={taskId} disabled={busy || loading} onChange={e => selectTask(e.target.value)}><option value="">选择任务以恢复查看</option>{tasks.map(t => <option key={t.id} value={t.id}>{t.objective} · {stateLabel(t.state)}</option>)}</select></Field>
            <button className="btn btn-secondary" disabled={busy || !projectId} onClick={() => void act(async () => { setTasks(await api.tasks(projectId)); })}>刷新任务列表</button>
            <button className="btn btn-secondary" onClick={() => setView('setup')}>新建任务</button>
          </div>
          {!task && <div className="wb-empty-live"><span>{taskId ? '正在读取任务…' : tasks.length ? '从上方选择一个任务，查看它的实时进展与产物。' : '这个项目还没有自主实验任务。'}</span>{!taskId && <button className="btn btn-primary btn-sm" onClick={() => setView('setup')}>去配置并启动</button>}</div>}
          {task && <>
            <p className="wb-live-head">{running && <span className="wb-live-dot" aria-hidden="true" />}<b>{stateLabel(task.state)}</b> · 第 {timeline.stages.findIndex(st => st.id === timeline.active) + 1}/{timeline.stages.length} 阶段 · {events.length} 条事件 · 断线重连会从事件游标继续读取</p>
            <div className="wb-stat-grid">
              <div className="wb-stat"><span>状态</span><b>{stateLabel(task.state)}</b></div>
              <div className="wb-stat"><span>模型调用</span><b>{modelCalls}</b></div>
              <div className="wb-stat"><span>冻结证据</span><b>{(files?.items ?? []).filter(f => f.source === 'artifact').length}</b></div>
              <div className="wb-stat"><span>最近事件</span><b>{events.length ? events[events.length - 1].type : '—'}</b></div>
            </div>
            <div className="auto-actions">{(['pause', 'resume', 'stop'] as const).map((action, i) => <button key={action} className="btn btn-secondary" disabled={busy || terminalState(task.state) || (action === 'resume' && (task.state !== 'paused' || task.pending_decision != null))} onClick={() => void act(async () => { await controlTask(task.id, action); })}>{['暂停', '恢复 / 继续', '停止'][i]}</button>)}<button className="btn btn-secondary" disabled={busy} onClick={() => void act(() => api.exportTask(task.id))}>导出 ZIP</button></div>
            {task.pending_decision != null && <div className="auto-alert"><h3>等待方案审批</h3>{approval ? <>
              <p>{approval.reason}</p><p>变更字段：{approval.changed.join('、') || '无'}</p>
              <h3>提议方案</h3><pre>{pretty(approval.proposed.protocol)}</pre>
              <h3>变更字段的新值</h3><pre>{pretty(Object.fromEntries(approval.changed.map(field => [field, approval.proposed.protocol[field]])))}</pre>
              <p className="ui-mono">方案 SHA256：{approval.proposed.sha256}</p><p className="ui-mono">审批 ID：{approval.id}</p>
              <p>批准将采用上述冻结方案并继续实验。普通恢复和追加指令不会批准此方案。</p>
              <button className="btn btn-primary" disabled={busy || task.state !== 'paused'} onClick={() => void act(async () => { await controlTask(task.id, 'resume', `approve:${approval.id}`); })}>批准此方案并继续</button>
            </> : <><pre>{pretty(task.pending_decision)}</pre><p>待决策内容无法识别，请刷新任务记录后处理。</p></>}</div>}
            {task.error != null && <div className="auto-alert" role="alert"><h3>阻塞 / 错误</h3><pre>{pretty(task.error)}</pre></div>}
            <div className="auto-compose">
              <Field title="追加指令 / 决策回复"><textarea rows={2} value={message} onChange={e => setMessage(e.target.value)} placeholder="例如：先用小数据集跑通，再扩大规模" /></Field>
              <button className="btn btn-primary" disabled={busy || !message.trim() || terminalState(task.state)} onClick={() => void act(async () => { await controlTask(task.id, 'message', message.trim()); })}>发送指令</button>
            </div>
            {task.report != null && <details className="auto-details" open><summary>实验报告（完成不代表复现成立）</summary><pre>{pretty(task.report)}</pre></details>}
            <details className="auto-details"><summary>使用量</summary><pre>{pretty(task.usage)}</pre></details>
            <details className="auto-details"><summary>任务配置快照</summary><pre>{pretty(task.config_snapshot)}</pre></details>
            <details className="auto-details"><summary>原始事件流（{events.length}）</summary><ol className="auto-events" aria-label="任务事件">{events.map(event => <li key={event.sequence}><b>#{event.sequence} {event.type}</b><time>{event.created_at}</time><pre>{pretty(event.payload)}</pre></li>)}</ol>{events.length === 0 && <p className="ui-help">等待任务事件…</p>}</details>
          </>}
        </div>
      </Workbench>
    </div>
  </div>;
}
