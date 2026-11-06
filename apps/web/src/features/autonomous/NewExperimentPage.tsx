import { useCallback, useEffect, useRef, useState } from 'react';
import {
  createAutonomousApi, type AutonomousApi, type Capability, type ModelProfile, type ProjectConfig,
} from '../../api-client/autonomous';
import { createPapersApi, type DocParseMode, type DocParseStatus, type PapersApi, type PaperText, type ProjectInfo } from '../../api-client/papers';
import { Card, CardHeader } from '../../ui/Card';
import { Disclosure } from '../../ui/Disclosure';
import { Switch } from '../../ui/Controls';
import { Icon } from '../../ui/Icon';
import ParseProgress from '../docparse/ParseProgress';
import ModeDialog from '../docparse/ModeDialog';
import { canParseNow } from '../docparse/docparse';
import { DEFAULT_REPAIR_ROUNDS, MAX_REPAIR_ROUNDS, defaultConfig, modelReference, parseProtocol, profileRequest, researchTaskTypes, reusableProfiles, validateConfig } from './config';
import { launchBlockReason, paperIsPdf } from './launch';
import { parseProbe, probeMissingNotice, probeStale } from './probe';
import TargetProbeView, { type ProbeState } from './TargetProbeView';
import { errorDetail } from './TaskPage';
import './autonomous.css';

const autonomous = createAutonomousApi();
const papersApi = createPapersApi('demo-user');

interface PaperOption { id: string; version: string; title: string }

// 新建实验（#/new）：论文上传与解析、实验配置、授权启动在同一个流程里。
// 布局（P0-3）：首屏只放论文、实验目标、主模型、执行位置与「授权并启动」；
// 预算、专家、Skills、协议对象收进 Disclosure。解析失败不允许启动。
export default function NewExperimentPage({ api = autonomous, papers = papersApi, initialProject = '' }: {
  api?: AutonomousApi; papers?: PapersApi; initialProject?: string;
}) {
  const [projects, setProjects] = useState<ProjectInfo[]>([]);
  const [projectId, setProjectId] = useState(initialProject);
  const [newProjectName, setNewProjectName] = useState('');
  const [paperOptions, setPaperOptions] = useState<PaperOption[]>([]);
  const [paperId, setPaperId] = useState('');
  const [paperVersion, setPaperVersion] = useState('');
  const [config, setConfig] = useState<ProjectConfig>(() => defaultConfig(initialProject));
  const [configVersion, setConfigVersion] = useState<number>();
  const [skills, setSkills] = useState<Capability[]>([]);
  const [profiles, setProfiles] = useState<ModelProfile[]>([]);
  const [profile, setProfile] = useState<ModelProfile>({ id: 'research-model', base_url: '', model: '', api: 'openai-completions', auth_mode: 'api-key' });
  const [key, setKey] = useState('');
  const [reuseCredentials, setReuseCredentials] = useState('');
  const [skillSource, setSkillSource] = useState('');
  const [protocol, setProtocol] = useState('');
  const [authorized, setAuthorized] = useState(false);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  // 上传与解析
  const [pdfFile, setPdfFile] = useState<File | null>(null);
  const [textTitle, setTextTitle] = useState('');
  const [textBody, setTextBody] = useState('');
  const [parseStatus, setParseStatus] = useState<DocParseStatus | null>(null);
  const [paperText, setPaperText] = useState<PaperText | null>(null);
  const [isPdf, setIsPdf] = useState(false);
  const [modeDialog, setModeDialog] = useState(false);
  const [parseBusy, setParseBusy] = useState(false);
  const generation = useRef(0);
  const parsePoll = useRef<ReturnType<typeof setTimeout> | null>(null);
  // 执行位置能力探测（C6）：读取已保存配置，项目切换或重新探测时中止上一次请求。
  const [probe, setProbe] = useState<ProbeState>({ phase: 'idle' });
  const probeAbort = useRef<AbortController | null>(null);
  // 最近一次保存的配置：探测读的是它，不是表单里尚未保存的值。
  const [savedConfig, setSavedConfig] = useState<{ version: number; config: ProjectConfig } | null>(null);
  const runProbe = useCallback(async (id: string, saved: { version: number; config: ProjectConfig }) => {
    probeAbort.current?.abort();
    const controller = new AbortController();
    probeAbort.current = controller;
    setProbe({ phase: 'loading' });
    try {
      const parsed = parseProbe(await api.probeTarget(id, controller.signal));
      if (controller.signal.aborted) return;
      setProbe(parsed ? { phase: 'ready', probe: parsed, version: saved.version, config: saved.config } : { phase: 'error', error: '探测结果格式无法识别' });
    } catch (e) {
      if (!controller.signal.aborted) setProbe({ phase: 'error', error: errorDetail(e) });
    }
  }, [api]);
  useEffect(() => () => probeAbort.current?.abort(), []);

  async function act(work: () => Promise<void>) {
    setBusy(true); setError(''); setNotice('');
    try { await work(); } catch (e) { setError(errorDetail(e)); } finally { setBusy(false); }
  }

  // 初始：项目列表、Skills、模型配置。
  useEffect(() => {
    let alive = true;
    api.initializeSession()
      .then(() => Promise.all([papers.listProjects(), api.capabilities(), api.profiles()]))
      .then(([list, caps, profs]) => {
        if (!alive) return;
        setProjects(list); setSkills(caps); setProfiles(profs);
        if (!projectId && list.length) setProjectId(list[list.length - 1].id);
      })
      .catch(e => { if (alive) setError(errorDetail(e)); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api, papers]);

  const stopParsePoll = () => { if (parsePoll.current) { clearTimeout(parsePoll.current); parsePoll.current = null; } };
  useEffect(() => stopParsePoll, []);

  // 轮询解析进度，直到 ready / failed。
  const pollText = useCallback((id: string) => {
    stopParsePoll();
    const tick = async () => {
      try {
        const text = await papers.getPaperText(id);
        setPaperText(text);
        if (text.status === 'parsing' || text.status === 'none') parsePoll.current = setTimeout(() => void tick(), 2000);
      } catch { /* 下次轮询再试 */ parsePoll.current = setTimeout(() => void tick(), 4000); }
    };
    void tick();
  }, [papers]);

  // 项目切换：读配置与论文列表。
  useEffect(() => {
    const run = ++generation.current;
    setPaperOptions([]); setPaperId(''); setPaperVersion(''); setPaperText(null); setIsPdf(false);
    setConfig(defaultConfig(projectId)); setConfigVersion(undefined); setSavedConfig(null);
    probeAbort.current?.abort(); setProbe({ phase: 'idle' });
    if (!projectId) return;
    Promise.all([
      api.config(projectId).catch(e => { if (e instanceof Error && 'status' in e && (e as { status: number }).status === 404) return null; throw e; }),
      papers.listPapers(projectId),
    ]).then(async ([saved, list]) => {
      const resolved = await Promise.all(list.map(async row => {
        try {
          const info = await papers.getPaper(row.paper_id);
          return { id: row.paper_id, version: info.paper_version_id ?? '', title: info.title ?? row.title ?? row.paper_id };
        } catch {
          return { id: row.paper_id, version: '', title: row.title ?? row.paper_id };
        }
      }));
      if (generation.current !== run) return;
      if (saved) { setConfig(saved.config); setConfigVersion(saved.version); setSavedConfig(saved); void runProbe(projectId, saved); }
      setPaperOptions(resolved);
    }).catch(e => { if (generation.current === run) setError(errorDetail(e)); });
  }, [projectId, api, papers, runProbe]);

  const update = <K extends keyof ProjectConfig>(name: K, value: ProjectConfig[K]) => setConfig(old => ({ ...old, [name]: value }));

  async function afterPaperAccepted(id: string, pdf: boolean) {
    const info = await papers.getPaper(id);
    setPaperId(id);
    setPaperVersion(info.paper_version_id ?? '');
    setIsPdf(pdf);
    setPaperText(null);
    if (pdf) {
      const status = await papers.getDocParseStatus();
      setParseStatus(status);
      if (status.mode === null) setModeDialog(true);
      else if (canParseNow(status)) {
        await papers.parsePaper(id, crypto.randomUUID());
        pollText(id);
      } else pollText(id);
    }
    setPaperOptions(old => old.some(p => p.id === id) ? old : [...old, { id, version: info.paper_version_id ?? '', title: info.title ?? id }]);
  }

  async function chooseParseMode(mode: DocParseMode) {
    if (!paperId) return;
    setParseBusy(true); setError('');
    try {
      const status = await papers.setDocParseMode(mode);
      setParseStatus(status); setModeDialog(false);
      if (canParseNow(status, mode)) {
        await papers.parsePaper(paperId, crypto.randomUUID());
      }
      pollText(paperId);
    } catch (e) { setError(errorDetail(e)); } finally { setParseBusy(false); }
  }

  async function retryParse() {
    if (!paperId) return;
    setParseBusy(true); setError('');
    try { await papers.parsePaper(paperId, crypto.randomUUID()); pollText(paperId); }
    catch (e) { setError(errorDetail(e)); } finally { setParseBusy(false); }
  }

  async function selectExistingPaper(id: string) {
    stopParsePoll();
    setPaperId(id); setPaperVersion(''); setPaperText(null); setIsPdf(false);
    if (!id) return;
    try {
      const info = await papers.getPaper(id);
      setPaperVersion(info.paper_version_id ?? '');
      // 只有 PDF 需要解析；粘贴的正文没有解析记录（/text 状态为 none），不显示解析控件。
      if (!paperIsPdf(info)) return;
      setIsPdf(true);
      const text = await papers.getPaperText(id);
      setPaperText(text);
      if (text.status !== 'ready') {
        setParseStatus(await papers.getDocParseStatus());
        if (text.status === 'parsing') pollText(id);
      }
    } catch (e) { setError(errorDetail(e)); }
  }

  async function saveConfig() {
    validateConfig(config);
    const saved = await api.saveConfig(projectId, config, configVersion);
    setConfigVersion(saved.version); setConfig(saved.config); setSavedConfig(saved);
    return saved;
  }

  const startBlockReason = launchBlockReason({
    projectId, paperVersion, pdf: isPdf, text: paperText, authorized,
    mainProfile: config.model.provider_profile, objective: config.experiment.objective,
  });

  const probeDisabledReason = !projectId ? '请先选择或创建项目' : savedConfig === null ? '保存项目配置后才能探测执行位置。' : '';
  // 探测只是提示：缺项写在启动按钮旁，不禁用启动；准备阶段才是真正的关卡。
  const probeNotice = probe.phase === 'ready' ? probeMissingNotice(probe.probe) : '';
  const probeOutdated = probe.phase === 'ready' && probeStale(probe.config, config);

  const credentialSources = reusableProfiles(profile, profiles);
  const specialists = config.specialists ?? [];
  const chosenSkills = new Set(config.harness.skills);
  const limits = config.limits;

  return <div className="auto-setup-page">
    <header className="auto-page-head">
      <h1>新建实验</h1>
      <p className="ui-help">上传论文并完成解析，配置模型与预算后授权启动；任务过程与证据会持续记录。</p>
    </header>
    {error && <div className="ui-callout tone-bad" role="alert"><Icon name="alert" /><span>{error}</span></div>}
    {notice && <p role="status" className="ui-callout tone-idle"><Icon name="info" /><span>{notice}</span></p>}
    {loading && <p role="status" className="ui-help">读取项目、模型与 Skills…</p>}

    <Card>
      <CardHeader icon="doc" title="论文" subtitle="PDF 需先解析成文字；解析失败或未完成时不能启动实验。" />
      <div className="ui-card-section">
        <div className="ui-form">
          <label className="ui-field"><span className="ui-field-label">项目</span>
            <select value={projectId} disabled={busy || loading} onChange={e => setProjectId(e.target.value)}>
              <option value="">选择已有项目</option>
              {projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
          </label>
          <div className="ui-field">
            <span className="ui-field-label">新建项目</span>
            <div className="auto-inline">
              <input value={newProjectName} disabled={busy} onChange={e => setNewProjectName(e.target.value)} placeholder="项目名称" />
              <button type="button" className="btn btn-secondary" disabled={busy || !newProjectName.trim()} onClick={() => void act(async () => {
                const created = await papers.createProject(newProjectName.trim());
                setProjects(old => [...old, { id: created.id, name: created.name }]);
                setProjectId(created.id); setNewProjectName('');
                setNotice('项目已创建。');
              })}>创建</button>
            </div>
          </div>
        </div>

        {projectId && <>
          {paperOptions.length > 0 && <div className="ui-form">
            <label className="ui-field"><span className="ui-field-label">选择已有论文</span>
              <select value={paperId} disabled={busy} onChange={e => void selectExistingPaper(e.target.value)}>
                <option value="">不上传，选择已有论文</option>
                {paperOptions.map(p => <option key={p.id} value={p.id} disabled={!p.version}>{p.title}{p.version ? '' : '（版本不可用）'}</option>)}
              </select>
            </label>
          </div>}
          <div className="ui-form">
            <label className="ui-field"><span className="ui-field-label">上传 PDF</span>
              <input type="file" accept="application/pdf,.pdf" disabled={busy} onChange={e => setPdfFile(e.target.files?.[0] ?? null)} />
            </label>
            <div className="ui-field">
              <span className="ui-field-label" />
              <div className="auto-inline">
                <button type="button" className="btn btn-secondary" disabled={busy || !pdfFile} onClick={() => void act(async () => {
                  if (!pdfFile) return;
                  const accepted = await papers.uploadPaperPdf(projectId, pdfFile);
                  await afterPaperAccepted(accepted.paper_id, true);
                  setPdfFile(null);
                  setNotice('PDF 已上传。');
                })}>上传并解析</button>
                {paperVersion && <span className="ui-help">当前论文版本 <code className="ui-mono">{paperVersion}</code></span>}
              </div>
            </div>
          </div>
          <Disclosure variant="sub" title="没有 PDF？粘贴论文正文">
            <div className="ui-form">
              <label className="ui-field"><span className="ui-field-label">标题（可选）</span>
                <input value={textTitle} disabled={busy} onChange={e => setTextTitle(e.target.value)} />
              </label>
              <label className="ui-field"><span className="ui-field-label">正文</span>
                <textarea rows={5} value={textBody} disabled={busy} onChange={e => setTextBody(e.target.value)} placeholder="粘贴论文正文（Markdown 或纯文本）" />
              </label>
              <div className="ui-field">
                <span className="ui-field-label" />
                <button type="button" className="btn btn-secondary" disabled={busy || !textBody.trim()} onClick={() => void act(async () => {
                  const accepted = await papers.createTextPaper(projectId, { text: textBody.trim(), titleHint: textTitle.trim() || undefined });
                  await afterPaperAccepted(accepted.paper_id, false);
                  setTextBody(''); setTextTitle('');
                  setNotice('论文正文已提交。');
                })}>提交正文</button>
              </div>
            </div>
          </Disclosure>
          {isPdf && <ParseProgress
            docparse={{ pdf: isPdf, needsChoice: parseStatus?.mode === null, status: parseStatus, text: paperText }}
            busy={busy || parseBusy}
            onRetry={() => void retryParse()}
            onChoose={mode => void chooseParseMode(mode)}
            onReopenChoice={() => setModeDialog(true)}
            onRefresh={() => { if (paperId) pollText(paperId); }}
          />}
        </>}
      </div>
    </Card>

    <Card>
      <CardHeader icon="flask" title="实验目标与主模型" />
      <div className="ui-card-section">
        <div className="ui-form">
          <label className="ui-field"><span className="ui-field-label">实验目标</span>
            <textarea rows={3} value={config.experiment.objective} onChange={e => update('experiment', { ...config.experiment, objective: e.target.value })} placeholder="描述待验证的问题、期望指标和允许的实验范围" />
          </label>
          <label className="ui-field"><span className="ui-field-label">主模型（编排器）<small>负责实验编排；未配置专家时保持单模型执行</small></span>
            <select value={config.model.provider_profile} disabled={busy || loading} onChange={e => update('model', modelReference(profiles.find(p => p.id === e.target.value)))}>
              <option value="">请选择已有模型配置</option>
              {config.model.provider_profile && !profiles.some(p => p.id === config.model.provider_profile) && <option value={config.model.provider_profile}>{config.model.provider_profile}（配置不可用，请重新连接）</option>}
              {profiles.map(p => <option key={p.id} value={p.id}>{p.id} · {p.model}</option>)}
            </select>
          </label>
          <label className="ui-field"><span className="ui-field-label">执行位置</span>
            <select value={config.target.kind} onChange={e => {
              const { ssh_config_alias, remote_workspace, ...common } = config.target;
              update('target', e.target.value === 'ssh'
                ? { ...common, kind: 'ssh', ssh_config_alias: ssh_config_alias ?? '', remote_workspace: remote_workspace ?? `/workspace/paper2run/${projectId}` }
                : { ...common, kind: 'local' });
            }}>
              <option value="local">本地隔离环境</option>
              <option value="ssh">SSH 隔离环境（实验性：远端 GPU 尚未实机验收）</option>
            </select>
          </label>
          {config.target.kind === 'ssh' && <>
            <label className="ui-field"><span className="ui-field-label">SSH config 别名</span>
              <input value={config.target.ssh_config_alias ?? ''} onChange={e => update('target', { ...config.target, ssh_config_alias: e.target.value })} />
            </label>
            <label className="ui-field"><span className="ui-field-label">远程工作区</span>
              <input value={config.target.remote_workspace ?? ''} onChange={e => update('target', { ...config.target, remote_workspace: e.target.value })} />
            </label>
          </>}
        </div>
        <p className="ui-help">密钥仅保存在当前内存中，保存后清空，不写入浏览器存储或 URL；重启应用后需重新输入。</p>
        <TargetProbeView state={probe} ssh={config.target.kind === 'ssh'} disabledReason={probeDisabledReason}
          onProbe={() => { if (projectId && savedConfig) void runProbe(projectId, savedConfig); }} />
        {probeOutdated && <p className="ui-help auto-probe-meta">表单中的执行位置、工作区、镜像、GPU 或网络设置尚未保存；上面的结果对应已保存的配置，保存后会重新探测。</p>}
      </div>
    </Card>

    <Disclosure variant="card" title="新建模型连接">
      <div className="ui-form">
        <label className="ui-field"><span className="ui-field-label">配置 ID</span>
          <input value={profile.id} disabled={busy} onChange={e => setProfile({ ...profile, id: e.target.value })} /></label>
        <label className="ui-field"><span className="ui-field-label">API 协议</span>
          <select value={profile.api} disabled={busy} onChange={e => { setProfile({ ...profile, api: e.target.value as ModelProfile['api'] }); setReuseCredentials(''); }}>
            <option value="openai-completions">OpenAI Completions</option>
            <option value="anthropic-messages">Anthropic Messages</option>
          </select></label>
        <label className="ui-field"><span className="ui-field-label">Base URL</span>
          <input type="url" value={profile.base_url} disabled={busy} onChange={e => { setProfile({ ...profile, base_url: e.target.value }); setReuseCredentials(''); }} placeholder="https://provider.example/v1" /></label>
        <label className="ui-field"><span className="ui-field-label">模型名称</span>
          <input value={profile.model} disabled={busy} onChange={e => setProfile({ ...profile, model: e.target.value })} /></label>
        <label className="ui-field"><span className="ui-field-label">认证方式</span>
          <select value={profile.auth_mode ?? 'api-key'} disabled={busy} onChange={e => { setProfile({ ...profile, auth_mode: e.target.value as ModelProfile['auth_mode'] }); setKey(''); setReuseCredentials(''); }}>
            <option value="api-key">API Key（默认）</option>
            <option value="none">无认证（仅 HTTP 回环地址）</option>
          </select></label>
        {profile.auth_mode !== 'none' && <>
          <label className="ui-field"><span className="ui-field-label">复用已有配置凭据</span>
            <select value={reuseCredentials} disabled={busy} onChange={e => { setReuseCredentials(e.target.value); setKey(''); }}>
              <option value="">不复用 / 输入新密钥</option>
              {credentialSources.map(p => <option key={p.id} value={p.id}>{p.id} · {p.model}</option>)}
            </select></label>
          <label className="ui-field"><span className="ui-field-label">API Key<small>仅本次内存，重启后需重新输入</small></span>
            <input type="password" autoComplete="off" value={key} disabled={busy || !!reuseCredentials} onChange={e => setKey(e.target.value)} /></label>
        </>}
      </div>
      <p className="ui-help">项目配置仅保存 credential_ref；复用时只提交源配置 ID，由服务端校验同一用户、Base URL 与 API 协议。保存不代表连接已验证。</p>
      <div className="auto-actions">
        {[false, true].map(useAsMain => <button key={String(useAsMain)} type="button" className="btn btn-secondary" disabled={busy || !profile.id || !profile.model || !profile.base_url} onClick={() => void act(async () => {
          const saved = await api.saveProfile(profileRequest(profile, key, reuseCredentials, profiles));
          setKey(''); setReuseCredentials('');
          setProfiles(old => [...old.filter(p => p.id !== saved.id), saved]);
          if (useAsMain) update('model', modelReference(saved));
          setNotice('模型配置已保存，可供主模型或专家选择；运行时将检查实际连接。');
        })}>{useAsMain ? '保存并用作主模型' : '保存模型连接'}</button>)}
      </div>
    </Disclosure>

    <Disclosure variant="card" title="预算上限与执行细节">
      <div className="ui-form">
        <label className="ui-field"><span className="ui-field-label">目标配置名称</span>
          <input value={config.target.profile} onChange={e => update('target', { ...config.target, profile: e.target.value })} /></label>
        <label className="ui-field"><span className="ui-field-label">本地项目工作区</span>
          <input value={config.project.workspace} onChange={e => update('project', { ...config.project, workspace: e.target.value })} /></label>
        {(['wall_time_seconds', 'max_model_calls', 'max_model_tokens', 'storage_bytes'] as const).map((name, i) => (
          <label key={name} className="ui-field"><span className="ui-field-label">{['最长运行时间（秒）', '模型调用上限', '模型 Token 上限', '存储上限（字节）'][i]}</span>
            <input type="number" min="1" step="1" value={limits[name]} onChange={e => update('limits', { ...limits, [name]: Number(e.target.value) })} /></label>
        ))}
        <label className="ui-field"><span className="ui-field-label">修复轮次上限<small>独立复核发现问题后 Pi 最多修复的轮数，0–{MAX_REPAIR_ROUNDS}；0 表示只复核一次</small></span>
          <input type="number" min="0" max={MAX_REPAIR_ROUNDS} step="1" value={limits.max_repair_rounds === undefined ? DEFAULT_REPAIR_ROUNDS : Number.isNaN(limits.max_repair_rounds) ? '' : limits.max_repair_rounds} onChange={e => update('limits', { ...limits, max_repair_rounds: e.target.value === '' ? NaN : Number(e.target.value) })} /></label>
        <label className="ui-field"><span className="ui-field-label">GPU 设备（逗号分隔，可留空）</span>
          <input value={limits.gpu_devices.join(',')} onChange={e => update('limits', { ...limits, gpu_devices: e.target.value.split(',').map(s => s.trim()).filter(Boolean) })} /></label>
      </div>
      <p className="ui-help">目标须专用且强制隔离，镜像 paper2run-experiment:1。允许实验环境访问公网，禁止访问宿主与元数据服务。SSH 使用本机预配置别名与凭据，界面不收集私钥或密码。</p>
    </Disclosure>

    <Disclosure variant="card" title={`科研专家（可选，当前 ${specialists.length} / 16 位）`}>
      <p className="ui-help">主模型自主分配咨询，无需逐次选择专家或确认。专家输出仅建议；代码与实验执行及可信评估仍走主模型受控工具，所有调用共享总预算。专家的科研审查不等于可信评估。</p>
      {specialists.map((specialist, index) => <fieldset className="auto-specialist" key={index} disabled={busy}>
        <legend>专家 {index + 1}</legend>
        <div className="ui-form">
          <label className="ui-field"><span className="ui-field-label">专家 ID</span>
            <input value={specialist.id} maxLength={128} onChange={e => update('specialists', specialists.map((item, i) => i === index ? { ...item, id: e.target.value } : item))} placeholder="例如 method-advisor" /></label>
          <label className="ui-field"><span className="ui-field-label">专家模型配置</span>
            <select value={specialist.model.provider_profile} onChange={e => update('specialists', specialists.map((item, i) => i === index ? { ...item, model: modelReference(profiles.find(p => p.id === e.target.value)) } : item))}>
              <option value="">请选择已有模型配置</option>
              {specialist.model.provider_profile && !profiles.some(p => p.id === specialist.model.provider_profile) && <option value={specialist.model.provider_profile}>{specialist.model.provider_profile}（配置不可用，请重新连接）</option>}
              {profiles.map(p => <option key={p.id} value={p.id}>{p.id} · {p.model}</option>)}
            </select></label>
          <label className="ui-field"><span className="ui-field-label">专长描述</span>
            <textarea rows={3} maxLength={8192} value={specialist.description} onChange={e => update('specialists', specialists.map((item, i) => i === index ? { ...item, description: e.target.value } : item))} placeholder="描述擅长的领域、方法和适用问题" /></label>
        </div>
        <fieldset className="auto-specialist-types"><legend>可咨询任务类型（至少一种）</legend>
          {researchTaskTypes.map(type => <label className="auto-check" key={type.value}>
            <input type="checkbox" checked={specialist.task_types.includes(type.value)} onChange={e => update('specialists', specialists.map((item, i) => i === index ? { ...item, task_types: e.target.checked ? [...item.task_types.filter(v => v !== type.value), type.value] : item.task_types.filter(v => v !== type.value) } : item))} />
            {type.label}
          </label>)}
        </fieldset>
        <div className="auto-actions">
          <button type="button" className="btn btn-sm btn-secondary" onClick={() => update('specialists', specialists.filter((_, i) => i !== index))}>删除专家 {index + 1}</button>
        </div>
      </fieldset>)}
      <div className="auto-actions">
        <button type="button" className="btn btn-secondary" disabled={busy || specialists.length >= 16} onClick={() => {
          let suffix = 1; while (specialists.some(item => item.id === `specialist-${suffix}`)) suffix++;
          update('specialists', [...specialists, { id: `specialist-${suffix}`, description: '', task_types: [], model: modelReference() }]);
        }}>增加科研专家</button>
      </div>
    </Disclosure>

    <Disclosure variant="card" title={`科研 Skills（已选 ${chosenSkills.size}）`}>
      {skills.length === 0 && <p className="ui-help">暂无可用 Skills。</p>}
      {skills.length > 0 && <ul className="ui-list">
        {skills.map(s => <li key={s.id} className="ui-row">
          <span className="ui-row-main"><span className="ui-row-title">{s.name} <code className="ui-mono">{s.version}</code></span>
            <span className="ui-row-sub">{s.description}</span></span>
          <span className="ui-row-end">
            <Switch checked={chosenSkills.has(s.id)} disabled={!s.enabled || busy} onChange={on => update('harness', { ...config.harness, skills: on ? [...config.harness.skills, s.id] : config.harness.skills.filter(id => id !== s.id) })}>选用</Switch>
            <button type="button" className="btn btn-sm btn-secondary" disabled={busy} onClick={() => void act(async () => {
              await api.enableSkill(s.id, !s.enabled);
              setSkills(await api.capabilities());
              if (s.enabled) update('harness', { ...config.harness, skills: config.harness.skills.filter(id => id !== s.id) });
            })}>{s.enabled ? '禁用' : '启用'}</button>
          </span>
        </li>)}
      </ul>}
      <div className="ui-form">
        <label className="ui-field"><span className="ui-field-label">导入 Skill 的本地源路径</span>
          <div className="auto-inline">
            <input value={skillSource} onChange={e => setSkillSource(e.target.value)} placeholder="本机服务可读取的目录" />
            <button type="button" className="btn btn-secondary" disabled={busy || !skillSource.trim()} onClick={() => void act(async () => {
              await api.importSkill(skillSource.trim());
              setSkills(await api.capabilities());
              setSkillSource('');
              setNotice('Skill 已导入，版本与启用状态见列表。');
            })}>导入</button>
          </div>
        </label>
      </div>
    </Disclosure>

    <Disclosure variant="card" title="实验协议 JSON（可选）">
      <p className="ui-help">填写冻结的指标、数据划分与授权范围；越界变更需版本记录并暂停。</p>
      <div className="ui-form">
        <label className="ui-field"><span className="ui-field-label">协议对象</span>
          <textarea rows={6} value={protocol} onChange={e => setProtocol(e.target.value)} placeholder={'{"metrics": ["accuracy"]}'} /></label>
      </div>
    </Disclosure>

    <Card>
      <CardHeader icon="shield" title="授权与启动" />
      <div className="ui-card-section">
        <Switch checked={authorized} onChange={setAuthorized}
          hint="超出协议授权的变更需暂停并等待批准。">
          我授权在上述目标与预算内联网、获取数据与代码、安装依赖和运行实验
        </Switch>
        <div className="auto-actions auto-launch">
          <button type="button" className="btn btn-secondary" disabled={busy || loading || !projectId} onClick={() => void act(async () => { validateConfig(config); setNotice('配置校验通过；实际连接与凭据权限仍由服务端检查。'); })}>校验配置</button>
          <button type="button" className="btn btn-secondary" disabled={busy || loading || !projectId} onClick={() => void act(async () => {
            const saved = await saveConfig();
            setNotice('项目配置已保存，正在重新探测执行位置。');
            void runProbe(projectId, saved);
          })}>保存配置{configVersion !== undefined ? ` · v${configVersion}` : ''}</button>
          <button type="button" className="btn btn-primary" disabled={busy || loading || !!startBlockReason} title={startBlockReason || undefined} onClick={() => void act(async () => {
            const parsed = parseProtocol(protocol);
            await saveConfig();
            const created = await api.start(projectId, {
              objective: config.experiment.objective,
              paper_version_id: paperVersion,
              ...(parsed ? { protocol: parsed } : {}),
              authorization: { confirmed: true },
            });
            window.location.hash = `#/task/${encodeURIComponent(created.id)}`;
          })}>{busy ? '处理中…' : '授权并启动'}</button>
          {startBlockReason && <span className="ui-help">{startBlockReason}</span>}
          {probeNotice && <span className="ui-help auto-probe-missing" role="status">{probeNotice}</span>}
        </div>
      </div>
    </Card>

    {modeDialog && <ModeDialog status={parseStatus} busy={parseBusy} onChoose={mode => void chooseParseMode(mode)} onLater={() => setModeDialog(false)} />}
  </div>;
}
