import type { AutonomousFile } from '../../api-client/autonomous.ts';

// 图表：实验在冻结证据或工作区里产出的图片，直接显示在中间栏。
// 只读真实文件（与文件树同一来源），不生成或改画任何图；文件名就是图注。
// 冻结证据排在工作区文件之前；最多显示 MAX_FIGURES 张，其余在文件树里看。
const IMAGE_EXT = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg']);
const MAX_FIGURES = 6;
const MAX_FIGURE_BYTES = 8 * 1024 * 1024;

export const ext = (path: string) => path.slice(path.lastIndexOf('.') + 1).toLowerCase();
export const baseName = (path: string) => path.slice(path.lastIndexOf('/') + 1);

/** The figures to show: images only, frozen evidence first, then by path, at most MAX_FIGURES.
 *  The same image frozen as evidence and still in the workspace is shown once (the evidence copy). */
export function pickFigures(items: AutonomousFile[]): AutonomousFile[] {
  const seen = new Set<string>();
  return items
    .filter(f => IMAGE_EXT.has(ext(f.path)) && f.bytes > 0 && f.bytes <= MAX_FIGURE_BYTES)
    .sort((a, b) => (a.source === b.source ? a.path.localeCompare(b.path) : a.source === 'artifact' ? -1 : 1))
    .filter(f => {
      // Workspace files carry no SHA256, so the same image is recognised by name and size.
      const key = `${baseName(f.path)}:${f.bytes}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    })
    .slice(0, MAX_FIGURES);
}

