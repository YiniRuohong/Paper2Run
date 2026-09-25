import { useNavigate } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { getClient, dataMode, type SessionInfo } from '../api-client';
import { SCENARIOS } from '../api-client/mock/scenarios';
import { currentScenarioId, expireNextSession, setScenario } from '../api-client/mock/adapter';

export function TopBar({ session }: { session: SessionInfo }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const capabilities = useQuery({
    queryKey: ['capabilities'],
    queryFn: () => getClient().getCapabilities(),
    staleTime: 60_000,
  });
  const models = capabilities.data
    ? capabilities.data.models.filter((m) => m.available).map((m) => m.display_name).join(' + ')
    : '模型能力读取中';

  async function onScenarioChange(id: string) {
    setScenario(id);
    queryClient.clear();
    const def = SCENARIOS.find((s) => s.id === id);
    navigate(def?.entry ?? '/projects');
  }

  async function onLogout() {
    await getClient().logout();
    queryClient.clear();
    navigate('/login');
  }

  return (
    <header className="topbar">
      <div className="brand">
        <span className="brand-name">Paper<em>2</em>Run</span>
        <span className="brand-sub">论文复现工作台 · /api/v1 契约 v0.2</span>
      </div>
      <div className="topbar-right">
        <a href="/lab" className="mode-tag">打开 LIVE 实验工作台</a>
        {dataMode === 'mock' && (
          <div className="scenario-box">
            <label htmlFor="scenario">Mock 场景</label>
            <select
              id="scenario"
              className="scenario-select"
              value={currentScenarioId()}
              onChange={(e) => void onScenarioChange(e.target.value)}
            >
              {SCENARIOS.map((s) => (
                <option key={s.id} value={s.id}>{s.label}</option>
              ))}
            </select>
            <button type="button" className="scenario-act" onClick={() => { expireNextSession(); queryClient.invalidateQueries(); }}>
              模拟会话过期
            </button>
          </div>
        )}
        <span className={`mode-tag ${dataMode}`} title={dataMode === 'mock' ? '当前为测试数据，不是真实实验' : undefined}>
          {dataMode === 'mock' ? 'MOCK · 测试数据' : dataMode.toUpperCase()}
        </span>
        <span className="model-chip"><span className="live-dot"></span>{models}</span>
        <span className="user-chip">{session.user.display_name}</span>
        <button type="button" className="logout-btn" onClick={() => void onLogout()}>退出</button>
      </div>
    </header>
  );
}
