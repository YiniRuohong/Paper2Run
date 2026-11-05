import type { AutonomousEvent, AutonomousTask } from '../../api-client/autonomous';
import { Badge, Card, CardHeader, KV, KVList, StatusWord, type Tone } from '../../ui/Card';
import { CopyButton } from '../../ui/Controls';
import { Disclosure } from '../../ui/Disclosure';
import { Icon } from '../../ui/Icon';
import { evidenceKindLabel, maxRepairRounds, own } from './config';
import { parseCandidate, parseReviews, type ReviewView } from './report';
import { reviewFailures } from './stages';

// 候选与独立复核（v3 P1-2）：Pi 提交候选后，程序冻结快照、独立评估，
// 再在新会话里复核；发现问题时进入有上限的修复轮次。这里只摊开程序记录的事实：
// 快照、正式运行、冻结产物、复核结论与未完成的复核尝试。Pi 的提交说明单独折叠为模型叙述。

const REVIEW_STATUS: Record<string, { text: string; tone: Tone }> = {
  pending: { text: '待复核', tone: 'run' },
  issues: { text: '发现问题', tone: 'warn' },
  pass: { text: '复核通过', tone: 'ok' },
  unavailable: { text: '复核不可用', tone: 'warn' },
};
const VERDICT: Record<string, { text: string; tone: Tone }> = {
  pass: { text: '复核通过', tone: 'ok' },
  issues: { text: '发现问题', tone: 'warn' },
};

export const EMPTY_CANDIDATE_TEXT = 'Pi 还没有提交候选结果。提交时程序会检查：协议已冻结；本轮登记了 code / environment / data / result；入口代码已登记；协议要求的产物齐全；入口在最后一次登记代码之后成功运行。';

function Sha({ value, label }: { value: string | null; label: string }) {
  if (!value) return <span className="ui-mono">未知</span>;
  return <><span className="ui-mono" title={value}>{value.slice(0, 16)}…</span><CopyButton value={value} label={`复制${label}`} /></>;
}

export function CandidateCard({ task, events }: { task: AutonomousTask; events: AutonomousEvent[] }) {
  const candidate = parseCandidate(task.candidate);
  const reviews = parseReviews(task.reviews);
  const failures = reviewFailures(events);
  const round = typeof task.repair_round === 'number' ? task.repair_round : null;
  const max = maxRepairRounds(task.config_snapshot);
  const exhausted = task.outcome === 'completed_with_issues' && round !== null && round >= max;
  const malformed = (task.candidate != null && candidate === null) || reviews === null;
  const status = candidate
    ? own(REVIEW_STATUS, candidate.reviewStatus) ?? { text: `未知（${candidate.reviewStatus ?? '无状态'}）`, tone: 'idle' as Tone }
    : { text: '未提交候选', tone: 'idle' as Tone };
  const history = reviews ?? [];
  const empty = !candidate && history.length === 0 && failures.length === 0 && !malformed;

  return <Card className="auto-candidate" aria-label="候选与独立复核">
    <CardHeader icon="layers" title="候选与独立复核" status={status}
      subtitle={<>{round !== null ? `第 ${round} / ${max} 轮修复` : `修复轮次未知，上限 ${max} 轮`}。复核在独立会话中进行，结论由程序记录。</>} />
    <div className="ui-card-section auto-candidate-body">
      {malformed && <div className="ui-callout tone-idle" role="status"><Icon name="info" /><span>候选或复核记录的结构与前端预期不一致，原始内容见「任务事件」与 report.json。</span></div>}
      {empty && <div className="ui-empty">
        <span className="ui-tile"><Icon name="layers" size={18} /></span>
        <h3>还没有候选结果</h3>
        <p>{EMPTY_CANDIDATE_TEXT}</p>
      </div>}

      {candidate && <div>
        <h3 className="ui-subhead">当前候选</h3>
        <KVList>
          <KV label="快照 SHA256"><Sha value={candidate.snapshotSha256} label="快照 SHA256" /></KV>
          <KV label="轮次" mono={false} value={candidate.round !== null ? `第 ${candidate.round} 轮` : '未知'} />
          <KV label="冻结协议 SHA256"><Sha value={candidate.protocolSha256} label="冻结协议 SHA256" /></KV>
          <KV label="正式运行">
            {candidate.run?.jobId
              ? <><span className="ui-mono" title={candidate.run.jobId}>{candidate.run.jobId.slice(0, 16)}{candidate.run.jobId.length > 16 ? '…' : ''}</span><CopyButton value={candidate.run.jobId} label="复制作业 ID" /></>
              : <span className="ui-mono">未知</span>}
            <span className="ui-mono auto-candidate-exit">exit {candidate.run?.exitCode ?? '未知'}</span>
          </KV>
        </KVList>
        <Disclosure variant="sub" title="冻结产物" count={candidate.artifacts.length}>
          {candidate.artifacts.length === 0
            ? <p className="ui-help">该快照没有登记产物。</p>
            : <ul className="ui-list">
              {candidate.artifacts.map(artifact => <li key={artifact.name} className="ui-row">
                <span className="ui-row-main"><span className="ui-row-title ui-mono" title={artifact.name}>{artifact.name}</span></span>
                <span className="ui-row-end">
                  <Badge title={artifact.kind}>{evidenceKindLabel(artifact.kind)}</Badge>
                  <span className="ui-mono" title={artifact.sha256}>{artifact.sha256.slice(0, 12)}…</span>
                  <CopyButton value={artifact.sha256} label={`复制 ${artifact.name} 哈希`} />
                </span>
              </li>)}
            </ul>}
        </Disclosure>
        {candidate.summary && <Disclosure variant="sub" title="Pi 提交说明（模型叙述，不是执行证据）">
          <pre className="ui-code is-wrap" aria-label="Pi 提交说明（只读）">{candidate.summary}</pre>
        </Disclosure>}
      </div>}

      {history.length > 0 && <div>
        <h3 className="ui-subhead">复核记录（{history.length}）</h3>
        <ul className="ui-list">
          {history.map((review, i) => <li key={`${review.snapshotSha256}-${review.attempt ?? i}`} className="auto-review">
            {exhausted && i === history.length - 1 && <div className="ui-callout tone-warn" role="status"><Icon name="alert" /><span>修复轮次已用尽，以下问题未解决</span></div>}
            <ReviewEntry review={review} />
          </li>)}
        </ul>
      </div>}

      {failures.length > 0 && <div>
        <h3 className="ui-subhead">未完成的复核（{failures.length}）</h3>
        <ul className="ui-list">
          {failures.map(failure => <li key={failure.sequence} className="ui-row">
            <span className="ui-row-main">
              <span className="ui-row-title">{failure.attempt !== null ? `第 ${failure.attempt} 次复核未完成` : '复核未完成'}</span>
              <span className="ui-row-sub ui-mono">{failure.reason ?? '原因未知'}</span>
            </span>
            <span className="ui-row-end">
              {failure.snapshotSha256 && <span className="ui-mono" title={failure.snapshotSha256}>{failure.snapshotSha256.slice(0, 12)}…</span>}
              {failure.time && <time>{failure.time}</time>}
            </span>
          </li>)}
        </ul>
      </div>}
    </div>
  </Card>;
}

function ReviewEntry({ review }: { review: ReviewView }) {
  const verdict = own(VERDICT, review.verdict) ?? { text: `未知（${review.verdict}）`, tone: 'idle' as Tone };
  return <>
    <div className="auto-review-head">
      <StatusWord tone={verdict.tone}>{verdict.text}</StatusWord>
      <span>{review.round !== null ? `第 ${review.round} 轮` : '轮次未知'}，第 {review.attempt ?? '?'} 次复核</span>
      <span className="ui-mono auto-review-sha" title={review.snapshotSha256}>{review.snapshotSha256.slice(0, 12)}…</span>
      <CopyButton value={review.snapshotSha256} label="复制被复核的快照 SHA256" />
    </div>
    {review.findings.length === 0
      ? <p className="ui-help">{review.verdict === 'pass' ? '复核没有提出问题。' : '复核没有列出具体问题。'}</p>
      : <ol className="auto-findings">
        {review.findings.map((finding, j) => <li key={j}>
          <span>{finding.description || '（无描述）'}</span>
          {finding.evidence.length > 0 && <span className="auto-finding-refs">
            {finding.evidence.map((ref, k) => <code key={k} className="ui-mono">{ref}</code>)}
          </span>}
        </li>)}
      </ol>}
  </>;
}
