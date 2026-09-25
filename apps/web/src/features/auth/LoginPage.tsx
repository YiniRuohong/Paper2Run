import { useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import { getClient } from '../../api-client';
import { ErrorBanner } from '../../components/Feedback';
import { dataMode } from '../../api-client/config';

export function LoginPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const queryClient = useQueryClient();
  const [token, setToken] = useState('');
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const from = (location.state as { from?: string } | null)?.from ?? '/projects';

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await getClient().login(token.trim());
      queryClient.clear();
      navigate(from, { replace: true });
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-wrap">
      <div className="login-card">
        <div className="lc-title">Paper<em style={{ fontStyle: 'italic', color: 'var(--amber)' }}>2</em>Run</div>
        <div className="lc-sub">论文复现工作台为邀请制。输入一次性邀请码兑换访问；未受邀用户请联系项目发起人。</div>
        <form className="login-form" onSubmit={submit}>
          <input
            className="text-input"
            type="password"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder="邀请码（mock 模式：任意非空字符）"
            aria-label="邀请码"
            autoComplete="off"
          />
          {error != null && <ErrorBanner error={error} />}
          <button type="submit" className="btn primary" disabled={busy || !token.trim()}>
            {busy && <span className="spin"></span>}
            {busy ? '兑换中' : '兑换并进入'}
          </button>
        </form>
        <div className="lc-note">
          邀请为一次性兑换；若提交后响应丢失，请先刷新确认会话，未获得会话再联系后端负责人重发。<br />
          当前数据模式：{dataMode}，四视图仅为测试数据，完整产品 live 未接入。<br />
          <a href="/lab">打开 LIVE 实验工作台（本地开发身份，无需登录）</a>
        </div>
      </div>
    </div>
  );
}
