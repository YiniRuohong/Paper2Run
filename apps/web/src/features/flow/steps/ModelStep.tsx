import { useState } from 'react';
import type { AutonomousApi, ModelProfile, ProjectConfig } from '../../../api-client/autonomous';
import { Segmented } from '../../../ui/Controls';
import { Icon } from '../../../ui/Icon';
import { modelReference, profileRequest } from '../../autonomous/config';
import { connectionLabel, mergeConfig, profileIdFor, refreshOnFailure, saveConfigWithRetry, usableConnection } from '../draft';
import { readConfig } from '../draftData';
import StepFrame, { type Act, type StepFeedback } from '../StepFrame';
import { SETUP_TITLES } from '../timeline';

const NEW = '';
const API_OPTIONS = [{ value: 'openai-completions', label: 'OpenAI 兼容' }, { value: 'anthropic-messages', label: 'Anthropic' }] as const;

// 03 连接模型: reuse a saved connection, or enter address, model name, API key
// and the API kind. The profile ID, auth mode and credential reuse are not shown.
// Saving cannot prove the connection works; the server checks it at start.
// A failed save re-reads the page, so a connection that was saved is offered on retry.
export default function ModelStep({ projectId, config, profiles, connectionLost, objective, api, busy, act, feedback, refresh, onDone }: {
  projectId: string;
  config: ProjectConfig | null;
  profiles: ModelProfile[];
  connectionLost: boolean;
  objective: string;
  api: Pick<AutonomousApi, 'config' | 'saveConfig' | 'saveProfile'>;
  busy: boolean;
  act: Act;
  feedback: StepFeedback | null;
  /** Reads the draft again (profiles, config, version) after a failed save. */
  refresh(): Promise<unknown>;
  onDone(): Promise<void>;
}) {
  const saved = profiles.filter(usableConnection);
  const current = config?.model.provider_profile ?? '';
  const [choice, setChoice] = useState(() => connectionLost ? NEW : saved.some(p => p.id === current) ? current : saved[0]?.id ?? NEW);
  const [baseUrl, setBaseUrl] = useState('');
  const [model, setModel] = useState('');
  const [kind, setKind] = useState<ModelProfile['api']>('openai-completions');
  const [key, setKey] = useState('');
  const creating = choice === NEW;
  const unchanged = !creating && !!config && current === choice;

  const reason = busy ? '处理中…'
    : !creating ? ''
    : !baseUrl.trim() ? '请填写服务地址'
    : !model.trim() ? '请填写模型名称'
    : !key.trim() ? '请填写 API Key'
    : '';

  const save = () => void act(() => refreshOnFailure(async () => {
    let chosen: ModelProfile | undefined;
    if (creating) {
      // After a restart the lost connection is re-created under its old ID, so the saved config stays valid.
      // Otherwise the project gets its own ID: a new connection never overwrites one another project may share.
      const id = profileIdFor(projectId, connectionLost ? config : null);
      chosen = await api.saveProfile(profileRequest({ id, base_url: baseUrl, model, api: kind, auth_mode: 'api-key' }, key.trim(), '', profiles));
      setKey('');
      setChoice(chosen.id); // if the config save below fails, the retry reuses this connection
    } else {
      chosen = saved.find(p => p.id === choice);
      if (!chosen) throw new Error('所选连接已不可用，请新建连接。');
    }
    if (!unchanged) {
      const reference = modelReference(chosen);
      await saveConfigWithRetry(() => readConfig(api, projectId), (next, version) => api.saveConfig(projectId, next, version),
        latest => mergeConfig(latest, projectId, { model: reference, objective }));
    }
  }, refresh).then(onDone));

  return <StepFrame title={SETUP_TITLES.model} guide="AI 用这个模型阅读论文、写代码、推进实验。" feedback={feedback}
    next={{ label: creating ? '保存并继续' : unchanged ? '下一步' : '使用此连接并继续', disabled: reason || undefined, onClick: save }}>
    {connectionLost && <p className="ui-callout tone-warn" role="status"><Icon name="alert" /><span>应用重启后模型连接已清空，请重新填写。</span></p>}
    <div className="ui-form">
      {saved.length > 0 && <label className="ui-field"><span className="ui-field-label">使用已保存的连接</span>
        <select value={choice} disabled={busy} onChange={e => setChoice(e.target.value)}>
          {saved.map(p => <option key={p.id} value={p.id}>{connectionLabel(p)}</option>)}
          <option value={NEW}>新建连接</option>
        </select>
      </label>}
      {creating && <>
        <label className="ui-field"><span className="ui-field-label">服务地址</span>
          <input type="url" value={baseUrl} disabled={busy} placeholder="https://dashscope.aliyuncs.com/compatible-mode/v1" onChange={e => setBaseUrl(e.target.value)} />
        </label>
        <label className="ui-field"><span className="ui-field-label">模型名称</span>
          <input value={model} disabled={busy} placeholder="qwen-max" onChange={e => setModel(e.target.value)} />
        </label>
        <div className="ui-field"><span className="ui-field-label">接口类型</span>
          <Segmented label="接口类型" value={kind} options={API_OPTIONS} disabled={busy} onChange={setKind} />
        </div>
        <label className="ui-field"><span className="ui-field-label">API Key<small>只保存在本机服务内存中，重启后需重新填写</small></span>
          <input type="password" autoComplete="off" value={key} disabled={busy} onChange={e => setKey(e.target.value)} />
        </label>
      </>}
    </div>
  </StepFrame>;
}
