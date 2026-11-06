import { useEffect, useState } from 'react';
import type { AutonomousFile } from '../../api-client/autonomous';
import { Card, CardHeader } from '../../ui/Card';
import { baseName, ext, pickFigures } from './figures';

// 图表：实验产出的图片直接显示在中间栏；挑哪些图见 figures.ts。
export { pickFigures } from './figures';

type Loaded = { key: string; url: string } | { key: string; error: true };
const keyOf = (f: AutonomousFile) => `${f.source}:${f.path}:${f.sha256 ?? f.bytes}`;

export function FiguresView({ items, load, compact, onOpen }: {
  items: AutonomousFile[];
  load: (file: AutonomousFile, signal: AbortSignal) => Promise<Blob>;
  /** 中间栏一屏布局：不套卡片，图片按可用高度缩放，点击放大。 */
  compact?: boolean;
  onOpen?: (file: AutonomousFile, url: string) => void;
}) {
  const figures = pickFigures(items);
  const signature = figures.map(keyOf).join('|');
  const [loaded, setLoaded] = useState<Loaded[]>([]);

  useEffect(() => {
    if (figures.length === 0) { setLoaded([]); return; }
    const controller = new AbortController();
    const urls: string[] = [];
    Promise.all(figures.map(async (file): Promise<Loaded> => {
      try {
        const blob = await load(file, controller.signal);
        const typed = ext(file.path) === 'svg' ? new Blob([blob], { type: 'image/svg+xml' }) : blob;
        const url = URL.createObjectURL(typed);
        urls.push(url);
        return { key: keyOf(file), url };
      } catch {
        return { key: keyOf(file), error: true };
      }
    })).then(next => { if (!controller.signal.aborted) setLoaded(next); });
    return () => { controller.abort(); urls.forEach(url => URL.revokeObjectURL(url)); };
  }, [signature]); // the signature captures the figure list; load is stable per task

  if (figures.length === 0) return null;
  if (compact) {
    // 1–2 张竖排，3 张及以上两列；行高平分可用高度，图片 object-fit: contain。
    const cols = figures.length <= 2 ? 1 : 2;
    return <section className="auto-figures-fill" aria-label="图表" style={{ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))` }}>
      {figures.map(file => {
        const entry = loaded.find(l => l.key === keyOf(file));
        const url = entry && 'url' in entry ? entry.url : null;
        const name = baseName(file.path);
        return <button type="button" key={keyOf(file)} className="auto-figure-tile" disabled={!url || !onOpen} title={`点击放大 ${name}`} onClick={() => { if (url && onOpen) onOpen(file, url); }}>
          <span className="auto-figure-img">
            {!entry && <span className="ui-help">读取中…</span>}
            {entry && 'error' in entry && <span className="ui-help">读取失败，可在右侧文件树打开</span>}
            {url && <img src={url} alt={name} />}
          </span>
          <span className="auto-figure-cap"><span className="ui-mono">{name}</span><small>{file.source === 'artifact' ? '证据' : '工作区'}</small></span>
        </button>;
      })}
    </section>;
  }
  return <Card>
    <CardHeader icon="image" title="图表" subtitle="实验产出的图片，原样显示；文件名即图注。是否支持结论以上方程序判定为准。" />
    <div className="ui-card-section">
      <div className="auto-figures">
        {figures.map(file => {
          const entry = loaded.find(l => l.key === keyOf(file));
          return <figure key={keyOf(file)} className="auto-figure">
            <div className="auto-figure-img">
              {!entry && <span className="ui-help">读取中…</span>}
              {entry && 'error' in entry && <span className="ui-help">读取失败，可在右侧文件树打开</span>}
              {entry && 'url' in entry && <img src={entry.url} alt={baseName(file.path)} loading="lazy" />}
            </div>
            <figcaption><span className="ui-mono" title={file.path}>{baseName(file.path)}</span>{file.source === 'artifact' ? <small>证据</small> : <small>工作区</small>}</figcaption>
          </figure>;
        })}
      </div>
    </div>
  </Card>;
}
