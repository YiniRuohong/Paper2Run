import { useEffect, useState } from 'react';
import type { AutonomousFile } from '../../api-client/autonomous';
import { Card, CardHeader } from '../../ui/Card';

// 图表：实验在冻结证据或工作区里产出的图片，直接显示在中间栏。
// 只读真实文件（与文件树同一来源），不生成或改画任何图；文件名就是图注。
// 冻结证据排在工作区文件之前；最多显示 MAX_FIGURES 张，其余在文件树里看。
const IMAGE_EXT = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg']);
const MAX_FIGURES = 6;
const MAX_FIGURE_BYTES = 8 * 1024 * 1024;

const ext = (path: string) => path.slice(path.lastIndexOf('.') + 1).toLowerCase();
const baseName = (path: string) => path.slice(path.lastIndexOf('/') + 1);

/** The figures to show: images only, frozen evidence first, then by path, at most MAX_FIGURES. */
export function pickFigures(items: AutonomousFile[]): AutonomousFile[] {
  return items
    .filter(f => IMAGE_EXT.has(ext(f.path)) && f.bytes > 0 && f.bytes <= MAX_FIGURE_BYTES)
    .sort((a, b) => (a.source === b.source ? a.path.localeCompare(b.path) : a.source === 'artifact' ? -1 : 1))
    .slice(0, MAX_FIGURES);
}

type Loaded = { key: string; url: string } | { key: string; error: true };
const keyOf = (f: AutonomousFile) => `${f.source}:${f.path}:${f.sha256 ?? f.bytes}`;

export function FiguresView({ items, load }: {
  items: AutonomousFile[];
  load: (file: AutonomousFile, signal: AbortSignal) => Promise<Blob>;
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
