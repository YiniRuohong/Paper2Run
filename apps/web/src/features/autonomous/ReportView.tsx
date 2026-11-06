import { Badge, Card, CardHeader, KV, KVList, StatusWord } from '../../ui/Card';
import { CopyButton } from '../../ui/Controls';
import { Disclosure } from '../../ui/Disclosure';
import { Icon } from '../../ui/Icon';
import { outcomeLabel, outcomeReasonLabel, outcomeTone } from './config';
import { parseReport, type ExperimentReport } from './report';

// 报告视图（P0-6）：程序判定与模型叙述严格分区。
// status / verdict / completion_gate / 缺失项 / 执行观测 / 指标全部来自程序；
// model_narrative 单独折叠并固定标注「模型叙述（不是执行证据）」。
// not_comparable 是诚实状态：不出现任何绿色「成功」标识。
// 任务页把报告分两处放：ReportSummary（结局、结论、指标、证据完备性）在中间栏上方，
// ReportDetails（完成门槛、执行观测、模型叙述、局限）收进折叠的「详细记录」。
export function ReportView({ report, onOpenEvent }: { report: unknown; onOpenEvent?: (sequence: number) => void }) {
  return <div className="auto-report">
    <ReportSummary report={report} />
    <ReportDetails report={report} onOpenEvent={onOpenEvent} />
  </div>;
}

/** 结论性内容：结局在前、结论在后，然后是指标。证据完备性在第 08 步的抽屉里（ReportEvidence）。 */
export function ReportSummary({ report }: { report: unknown }) {
  const parsed = parseReport(report);
  if (!parsed) {
    return <Card>
      <CardHeader icon="doc" title="实验报告" status={{ text: '结构未识别', tone: 'idle' }}
        subtitle="报告结构与前端预期不一致，原始内容见下方折叠区。" />
      <div className="ui-card-section">
        <Disclosure variant="sub" title="报告原始 JSON">
          <pre className="ui-code" aria-label="报告原始 JSON（只读）">{JSON.stringify(report, null, 2)}</pre>
        </Disclosure>
      </div>
    </Card>;
  }
  return <div className="auto-report">
    <OutcomeCard report={parsed} />
    <ConclusionCard report={parsed} />
    <MetricsCard report={parsed} />
  </div>;
}

/** 中间栏的报告摘要：结局、比较结论、报告状态、前两条原因与指标；全文在「报告」抽屉里（onOpen）。 */
export function ReportBrief({ report, onOpen }: { report: unknown; onOpen: () => void }) {
  const parsed = parseReport(report);
  if (!parsed) return <div className="auto-brief">
    <p className="ui-help">报告结构与前端预期不一致。</p>
    <button type="button" className="btn btn-secondary" onClick={onOpen}>查看完整报告</button>
  </div>;
  const label = outcomeLabel(parsed.outcome);
  const tone = outcomeTone(parsed.outcome);
  const verdictText = parsed.verdict === 'not_comparable' ? '不可比较' : parsed.verdict;
  const statusText = reportStatusText(parsed.status);
  const all = Object.entries(parsed.evaluation.metrics);
  const metrics = all.slice(0, 6);
  return <div className="auto-brief">
    <div className="auto-brief-head">
      <div><span>结局</span><b className={label && tone ? `tone-${tone}` : undefined}>{label ?? '无结局'}</b></div>
      <div><span>比较结论</span><b className={parsed.verdict === 'not_comparable' ? 'tone-warn' : undefined}>{verdictText}</b></div>
      <div><span>报告状态</span><b>{statusText}</b></div>
    </div>
    {parsed.evaluation.reasons.length > 0 && <ul className="auto-brief-reasons">
      {parsed.evaluation.reasons.slice(0, 2).map((reason, i) => <li key={i} title={reason}>{reason}</li>)}
    </ul>}
    {metrics.length > 0 && <div className="wb-stat-grid auto-metrics">
      {metrics.map(([name, value]) => <div className="wb-stat" key={name}><span className="ui-mono">{name}</span><b>{formatNumber(value)}</b></div>)}
    </div>}
    <p className="auto-brief-note">
      <span>判定由程序给出，不来自模型自述{all.length > metrics.length ? `；另有 ${all.length - metrics.length} 项指标在完整报告里` : ''}。</span>
      <button type="button" className="btn btn-secondary" onClick={onOpen}>查看完整报告</button>
    </p>
  </div>;
}

/** 证据完备性（程序判定），放在「候选与复核」的抽屉里。 */
export function ReportEvidence({ report }: { report: unknown }) {
  const parsed = parseReport(report);
  return parsed ? <EvidenceCard report={parsed} /> : null;
}

/** 过程性内容：完成门槛、逐条执行观测、模型叙述与局限。 */
export function ReportDetails({ report, onOpenEvent }: { report: unknown; onOpenEvent?: (sequence: number) => void }) {
  const parsed = parseReport(report);
  if (!parsed) return null;
  return <div className="auto-report">
    <GateCard report={parsed} />
    <ObservationsCard report={parsed} onOpenEvent={onOpenEvent} />
    <Card>
      <CardHeader icon="doc" title="模型叙述" subtitle="模型自述的实验过程，不是执行证据；结论以上方程序判定为准。" />
      <div className="ui-card-section">
        <Disclosure variant="sub" title="模型叙述（不是执行证据）">
          <pre className="ui-code" aria-label="模型叙述（只读）">{parsed.modelNarrative || '（空）'}</pre>
        </Disclosure>
      </div>
    </Card>
    <Card>
      <CardHeader icon="info" title="局限" />
      <div className="ui-card-section">
        <ul className="auto-limitations">{parsed.limitations.map((item, i) => <li key={i}>{item}</li>)}</ul>
        <div className="auto-report-hash">
          <span className="ui-mono" title={parsed.sha256}>报告 SHA256：{parsed.sha256.slice(0, 16)}…</span>
          <CopyButton value={parsed.sha256} label="复制报告 SHA256" />
        </div>
      </div>
    </Card>
  </div>;
}

// 结局：程序按候选复核结果判定，与下方的比较结论（verdict）分开。
function OutcomeCard({ report }: { report: ExperimentReport }) {
  const label = outcomeLabel(report.outcome);
  const tone = outcomeTone(report.outcome);
  const reason = outcomeReasonLabel(report.outcomeReason);
  const sha = report.outcomeSnapshotSha256;
  const caption = report.outcome === 'verified' ? '复核通过不等于复现成立；可比性见下方结论。' : '结局由程序根据冻结候选与独立复核判定，不来自模型自述。';
  return <Card>
    <CardHeader icon="shield" title="结局" status={label && tone ? { text: label, tone } : { text: '无结局', tone: 'idle' }}
      subtitle={label ? caption : '该报告没有程序判定的结局：旧版任务，或任务结束时没有可复核的候选结果。'} />
    {label && <div className="ui-card-section">
      <KVList>
        <KV label="原因" value={reason ?? '—'} mono={reason === report.outcomeReason && reason !== null} />
        <KV label="判定所依据的候选快照">
          {sha
            ? <><span className="ui-mono" title={sha}>{sha.slice(0, 16)}…</span><CopyButton value={sha} label="复制候选快照 SHA256" /></>
            : <span className="ui-mono">—</span>}
        </KV>
      </KVList>
    </div>}
  </Card>;
}

function ConclusionCard({ report }: { report: ExperimentReport }) {
  const statusText = reportStatusText(report.status);
  const verdictText = report.verdict === 'not_comparable' ? '不可比较' : report.verdict;
  return <Card>
    <CardHeader icon="flask" title="结论" status={{ text: verdictText, tone: report.verdict === 'not_comparable' ? 'warn' : 'idle' }}
      subtitle="判定由程序给出，不来自模型自述。任务完成不代表复现成立。" />
    <div className="ui-card-section auto-conclusion">
      <div className="auto-conclusion-grid">
        <div><span>报告状态</span><b>{statusText}</b></div>
        <div><span>比较结论</span><b className={report.verdict === 'not_comparable' ? 'tone-warn' : undefined}>{verdictText}</b></div>
      </div>
      {report.evaluation.reasons.length > 0 && <div>
        <h3 className="ui-subhead">原因</h3>
        <ul className="auto-limitations">{report.evaluation.reasons.map((reason, i) => <li key={i}>{reason}</li>)}</ul>
      </div>}
    </div>
  </Card>;
}

function GateCard({ report }: { report: ExperimentReport }) {
  const gate = report.completionGate;
  return <Card>
    <CardHeader icon="shield" title="完成门槛"
      status={{ text: report.successfulExecutionObserved ? '已观测到成功执行' : '未观测到成功执行', tone: report.successfulExecutionObserved ? 'ok' : 'bad' }} />
    <div className="ui-card-section">
      <dl className="ui-kv">
        <div className="ui-kv-row"><dt>入口文件</dt><dd><span className="ui-mono">{gate.entrypoint ?? '—'}</span></dd></div>
        <div className="ui-kv-row"><dt>冻结代码哈希</dt><dd>
          {gate.frozen_code_sha256
            ? <><span className="ui-mono" title={gate.frozen_code_sha256}>{gate.frozen_code_sha256.slice(0, 16)}…</span><CopyButton value={gate.frozen_code_sha256} label="复制冻结代码哈希" /></>
            : <span className="ui-mono">—</span>}
        </dd></div>
      </dl>
      <p className="ui-help">{gate.rule}</p>
    </div>
  </Card>;
}

const EVIDENCE_KINDS: Array<{ key: string; label: string }> = [
  { key: 'code', label: '代码' }, { key: 'environment', label: '环境' }, { key: 'data', label: '数据' }, { key: 'result', label: '结果' },
];

function EvidenceCard({ report }: { report: ExperimentReport }) {
  const missing = new Set(report.missingEvidenceKinds);
  return <Card>
    <CardHeader icon="layers" title="证据完备性"
      status={report.missingEvidenceKinds.length === 0 && report.missingArtifacts.length === 0
        ? { text: '四类证据齐全', tone: 'ok' }
        : { text: `缺 ${report.missingEvidenceKinds.length + report.missingArtifacts.length} 项`, tone: 'bad' }} />
    <div className="ui-card-section">
      <ul className="ui-list">
        {EVIDENCE_KINDS.map(kind => <li key={kind.key} className="ui-row">
          <Icon name={missing.has(kind.key) ? 'x' : 'check'} size={14} />
          <span className="ui-row-main"><span className="ui-row-title">{kind.label}证据</span></span>
          <StatusWord tone={missing.has(kind.key) ? 'bad' : 'ok'}>{missing.has(kind.key) ? '缺失' : '已冻结'}</StatusWord>
        </li>)}
      </ul>
      {report.missingArtifacts.length > 0 && <div>
        <h3 className="ui-subhead">缺失的声明产物</h3>
        <ul className="auto-limitations">{report.missingArtifacts.map(name => <li key={name}><code className="ui-mono">{name}</code></li>)}</ul>
      </div>}
      {report.artifacts.length > 0 && <Disclosure variant="sub" title="已冻结产物" count={report.artifacts.length}>
        <ul className="ui-list">
          {report.artifacts.map(artifact => <li key={`${artifact.protocolSha256 ?? 'unversioned'}/${artifact.name}`} className="ui-row">
            <span className="ui-row-main">
              <span className="ui-row-title ui-mono">{artifact.name}</span>
              <span className="ui-row-sub">{artifact.kind}{artifact.bytes !== null ? ` · ${artifact.bytes} B` : ''}</span>
            </span>
            <span className="ui-row-end"><span className="ui-mono" title={artifact.sha256}>{artifact.sha256.slice(0, 12)}…</span><CopyButton value={artifact.sha256} label={`复制 ${artifact.name} 哈希`} /></span>
          </li>)}
        </ul>
      </Disclosure>}
    </div>
  </Card>;
}

function ObservationsCard({ report, onOpenEvent }: { report: ExperimentReport; onOpenEvent?: (sequence: number) => void }) {
  const rows = report.executionObservations;
  return <Card>
    <CardHeader icon="terminal" title="执行观测" subtitle="程序记录的每次命令执行；「匹配入口」指命令与已批准协议入口一致。" />
    <div className="ui-card-section">
      {rows.length === 0 && <p className="ui-help">尚无执行观测。</p>}
      {rows.length > 0 && <div className="auto-obs-wrap">
        <table className="wb-table auto-obs">
          <thead><tr><th>事件</th><th>工具</th><th>命令</th><th>exit</th><th>匹配入口</th></tr></thead>
          <tbody>{rows.map(row => <tr key={row.sequence}>
            <td>{onOpenEvent
              ? <button type="button" className="btn btn-ghost ui-mono" onClick={() => onOpenEvent(row.sequence)} title="在事件日志中查看">#{row.sequence}</button>
              : <span className="ui-mono">#{row.sequence}</span>}</td>
            <td className="ui-mono">{row.tool}</td>
            <td className="ui-mono auto-obs-cmd" title={row.command}>{row.command || '—'}</td>
            <td className="ui-mono">{row.exitCode ?? '—'}</td>
            <td>{row.matchesEntrypoint ? <Badge tone="ok">匹配</Badge> : <Badge>不匹配</Badge>}</td>
          </tr>)}</tbody>
        </table>
      </div>}
    </div>
  </Card>;
}

function MetricsCard({ report }: { report: ExperimentReport }) {
  const metrics = Object.entries(report.evaluation.metrics);
  const comparisons = report.verification?.comparisons ?? [];
  return <Card>
    <CardHeader icon="chart" title="指标" subtitle="由独立评测器重算；参考值标注来源范围（scope）。" />
    <div className="ui-card-section">
      {metrics.length === 0 && <p className="ui-help">没有可展示的指标。</p>}
      {metrics.length > 0 && <div className="wb-stat-grid auto-metrics">
        {metrics.map(([name, value]) => {
          const ci = report.evaluation.confidenceIntervals[`${name}_95`];
          return <div className="wb-stat" key={name}>
            <span className="ui-mono">{name}</span>
            <b>{formatNumber(value)}</b>
            {ci && <small className="ui-mono auto-ci">95% CI [{formatNumber(ci.lower)}, {formatNumber(ci.upper)}]</small>}
          </div>;
        })}
      </div>}
      {report.evaluation.withinTolerance !== null && <p className="ui-help">
        容差判定（仅数值对比，不代表复现成立）：{report.evaluation.withinTolerance ? '在容差内' : '超出容差'}
      </p>}
      {comparisons.length > 0 && <div>
        <h3 className="ui-subhead">与参考值对比（scope：<code className="ui-mono">{report.verification?.scope}</code>）</h3>
        <div className="auto-obs-wrap">
          <table className="wb-table auto-obs">
            <thead><tr><th>指标</th><th>参考值</th><th>观测值</th><th>绝对差</th><th>判定</th></tr></thead>
            <tbody>{comparisons.map((row, i) => <tr key={`${row.metric}-${i}`}>
              <td className="ui-mono">{row.metric}</td>
              <td className="ui-mono">{row.referenceValue !== null ? formatNumber(row.referenceValue) : '—'}</td>
              <td className="ui-mono">{row.observedValue !== null ? formatNumber(row.observedValue) : '—'}</td>
              <td className="ui-mono">{row.absoluteDifference !== null ? formatNumber(row.absoluteDifference) : '—'}</td>
              <td>{row.verdict === 'matched'
                ? <Badge tone="ok">容差内</Badge>
                : row.verdict === 'outside_tolerance'
                  ? <Badge tone="bad">超容差</Badge>
                  : <Badge tone="warn">不可比较</Badge>}</td>
            </tr>)}</tbody>
          </table>
        </div>
      </div>}
      {report.verification && report.verification.limitations.length > 0 && <Disclosure variant="sub" title="对比的局限" count={report.verification.limitations.length}>
        <ul className="auto-limitations">{report.verification.limitations.map((item, i) => <li key={i}>{item}</li>)}</ul>
      </Disclosure>}
    </div>
  </Card>;
}

function formatNumber(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toPrecision(6).replace(/\.?0+$/, '');
}

// 报告状态用中文显示；未知的值原样显示，不猜。
const REPORT_STATUS_TEXT: Record<string, string> = { incomplete: '流程未完成', not_comparable: '不可比较' };
function reportStatusText(status: string): string {
  return Object.prototype.hasOwnProperty.call(REPORT_STATUS_TEXT, status) ? REPORT_STATUS_TEXT[status] : status;
}
