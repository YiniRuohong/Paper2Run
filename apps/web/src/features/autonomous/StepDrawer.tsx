import type { ReactNode } from 'react';
import { Badge } from '../../ui/Card';
import { Disclosure } from '../../ui/Disclosure';
import { evidenceKindLabel } from './config';
import { OUTPUT_LIMIT, type ItemDetail } from './stepDetail';

// 抽屉里一条子步骤的过程：命令与输出、工具参数与结果、自报进度、专家调用、冻结证据或事件。
// 模型自述与专家回复照旧标注为模型叙述，不当作执行证据。

const pretty = (value: unknown) => JSON.stringify(value, null, 2);

export function itemTitle(detail: ItemDetail): string {
  switch (detail.kind) {
    case 'command': return detail.command;
    case 'tool': return detail.title;
    case 'note': return detail.summary || detail.phase;
    case 'delegation': return `科研专家 · ${detail.call.specialistId ?? '未知专家'}`;
    case 'artifact': return detail.name;
    case 'event': return detail.label;
  }
}

function Block({ label, children }: { label: string; children: string }) {
  return <section className="auto-drawer-block">
    <span className="auto-drawer-label">{label}</span>
    <pre className="ui-code is-wrap">{children}</pre>
  </section>;
}

function Meta({ rows }: { rows: Array<[string, ReactNode]> }) {
  const shown = rows.filter(([, value]) => value !== null && value !== undefined && value !== '');
  if (shown.length === 0) return null;
  return <dl className="flow-summary">{shown.map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}</dl>;
}

export function ItemDrawerBody({ detail }: { detail: ItemDetail }) {
  switch (detail.kind) {
    case 'command':
      return <>
        <Block label="命令">{detail.command}</Block>
        {detail.error && <p className="ui-callout tone-bad" role="alert"><span>{detail.error}</span></p>}
        {detail.running
          ? <p className="ui-help">运行中，输出完成后显示。</p>
          : <>
            {detail.stdout !== null && <Block label="输出">{detail.stdout || '（无输出）'}</Block>}
            {detail.stderr && <Block label="错误输出">{detail.stderr}</Block>}
            {detail.stdout === null && !detail.stderr && !detail.error && <p className="ui-help">这条调用没有返回输出。</p>}
            {detail.truncated && <p className="ui-help">输出过长，只显示末尾 {Math.round(OUTPUT_LIMIT / 1000)}K 字符；完整内容在导出的 ZIP 里。</p>}
          </>}
        <Meta rows={[['退出码', detail.exitCode !== null ? <code className="ui-mono">{detail.exitCode}</code> : null], ['状态', detail.state], ['工具', <code className="ui-mono">{detail.name}</code>], ['时间', detail.time]]} />
      </>;
    case 'tool':
      return <>
        {detail.error && <p className="ui-callout tone-bad" role="alert"><span>{detail.error}</span></p>}
        <Block label="参数">{pretty(detail.args)}</Block>
        {detail.running ? <p className="ui-help">运行中，结果返回后显示。</p> : !detail.error && <Block label="结果">{pretty(detail.result)}</Block>}
        <Meta rows={[['工具', <code className="ui-mono">{detail.name}</code>], ['时间', detail.time]]} />
      </>;
    case 'note':
      return <>
        <p className="auto-drawer-note"><Badge tone="idle">模型叙述（不是执行证据）</Badge></p>
        <Meta rows={[['阶段', detail.phase], ['时间', detail.time]]} />
        <p className="auto-drawer-text">{detail.summary || '（空）'}</p>
      </>;
    case 'delegation': {
      const call = detail.call;
      return <>
        <p className="auto-drawer-note"><Badge tone="idle">建议，不是执行证据</Badge></p>
        <Meta rows={[['专家', <span className="ui-mono">{call.specialistId ?? '未知专家'}</span>], ['任务类型', call.taskTypeLabel], ['模型', <code className="ui-mono">{call.model ?? '未知'}</code>], ['理由', call.reason ?? '未提供'], ['时间', call.time]]} />
        {call.text ? <Block label="专家回复（模型叙述）">{call.text}</Block> : <p className="ui-help">{call.status === 'running' ? '专家还在回复。' : '没有返回内容。'}</p>}
      </>;
    }
    case 'artifact':
      return <Meta rows={[
        ['类型', detail.evidenceKind ? evidenceKindLabel(detail.evidenceKind) : null],
        ['轮次', detail.round !== null ? `第 ${detail.round} 轮` : null],
        ['来源', detail.sourcePath ? <code className="ui-mono">{detail.sourcePath}</code> : null],
        ['大小', detail.bytes !== null ? `${detail.bytes} B` : null],
        ['SHA256', detail.sha256 ? <code className="ui-mono auto-drawer-sha">{detail.sha256}</code> : null],
        ['时间', detail.time],
      ]} />;
    case 'event':
      return <>
        {detail.detail && <p className="auto-drawer-text">{detail.detail}</p>}
        <Meta rows={[['事件', <code className="ui-mono">{detail.type}</code>], ['时间', detail.time]]} />
        <Disclosure variant="sub" title="原始载荷"><pre className="ui-code">{pretty(detail.payload)}</pre></Disclosure>
      </>;
  }
}
