import type { AutonomousEvent } from '../../api-client/autonomous';
import { Badge, Card, CardHeader, StatusWord, type Tone } from '../../ui/Card';
import { Disclosure } from '../../ui/Disclosure';
import { Icon } from '../../ui/Icon';
import { progressNotes, specialistCalls, type SpecialistCall } from './stages';

// Pi 的自述与专家建议：都是模型叙述，与程序判定（候选复核、报告）分区展示。

export function ProgressNotes({ events }: { events: AutonomousEvent[] }) {
  const notes = progressNotes(events);
  return <Card className="auto-notes" aria-label="Pi 自报进度">
    <CardHeader icon="list" title="Pi 自报进度（模型叙述，不是程序判定）"
      subtitle="Pi 通过 note_progress 记录的阶段与摘要，不影响证据与结局。" />
    <div className="ui-card-section">
      {notes.length === 0
        ? <p className="ui-help auto-quiet">Pi 还没有记录进度。它调用 note_progress 时，阶段与摘要会按时间倒序出现在这里。</p>
        : <ul className="ui-list auto-notes-list">
          {notes.map(note => <li key={note.sequence} className="ui-row">
            <span className="ui-row-main">
              <span className="ui-row-title">{note.phase ?? '未命名阶段'}</span>
              <span className="ui-row-sub">{note.summary ?? '（无摘要）'}</span>
            </span>
            <span className="ui-row-end">{note.time ? <time>{note.time}</time> : <span>时间未知</span>}</span>
          </li>)}
        </ul>}
    </div>
  </Card>;
}

const CALL_STATUS: Record<SpecialistCall['status'], { text: string; tone: Tone }> = {
  running: { text: '进行中', tone: 'run' },
  done: { text: '已返回', tone: 'idle' },
  failed: { text: '调用失败', tone: 'bad' },
};

export function SpecialistCalls({ events, configured }: { events: AutonomousEvent[]; configured: number }) {
  const calls = specialistCalls(events);
  return <Disclosure variant="card" title="科研专家调用" count={calls.length} subtitle="建议，不是执行证据">
    {calls.length === 0
      ? <p className="ui-help auto-quiet">{configured > 0
        ? `本任务配置了 ${configured} 位科研专家，主模型还没有咨询。咨询时，专家、任务类型、模型与理由会出现在这里。`
        : '本任务没有配置科研专家，主模型单独执行。'}</p>
      : <ul className="ui-list">
        {calls.map(call => <li key={call.sequence} className="auto-call">
          <div className="auto-call-head">
            <span className="ui-mono">{call.specialistId ?? '未知专家'}</span>
            <Badge>{call.taskTypeLabel}</Badge>
            <StatusWord tone={CALL_STATUS[call.status].tone}>{CALL_STATUS[call.status].text}</StatusWord>
            {call.time && <time>{call.time}</time>}
          </div>
          <p className="auto-call-meta">模型 <code className="ui-mono">{call.model ?? '未知'}</code></p>
          <p className="auto-call-meta">理由：{call.reason ?? '未提供'}</p>
          {call.status !== 'running' && <Disclosure variant="sub" title="专家回复（模型叙述，不是执行证据）">
            {call.text ? <pre className="ui-code">{call.text}</pre> : <p className="ui-help">没有返回内容。</p>}
          </Disclosure>}
        </li>)}
      </ul>}
    <p className="ui-help auto-call-note"><Icon name="info" size={13} /> 专家输出只作建议；代码执行与可信评估仍由主模型的受控工具和程序完成。</p>
  </Disclosure>;
}
