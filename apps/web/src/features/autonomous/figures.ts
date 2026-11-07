import type { AutonomousFile } from '../../api-client/autonomous.ts';

// 图表：实验在冻结证据或工作区里产出的图片，直接显示在中间栏。
// 只读真实文件（与文件树同一来源），不生成或改画任何图；文件名就是图注。
// 只显示关键图表：已冻结为证据的图；还没有时用工作区里依赖目录之外的图。最多 MAX_FIGURES 张，其余在文件树里看。
const IMAGE_EXT = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg']);
const MAX_FIGURES = 6;
const MAX_FIGURE_BYTES = 8 * 1024 * 1024;

export const ext = (path: string) => path.slice(path.lastIndexOf('.') + 1).toLowerCase();
export const baseName = (path: string) => path.slice(path.lastIndexOf('/') + 1);

// 依赖、第三方源码、缓存里的图片（文档截图、库自带图标）不是实验图表。
const DEPENDENCY_DIRS = new Set(['vendor', 'third_party', 'thirdparty', 'external', 'node_modules', 'site-packages',
  'dist-packages', 'venv', 'env', '__pycache__', 'build', 'dist', 'lib', 'lib64', 'include', 'share', 'docs', 'doc', 'website']);
const fromDependency = (path: string) =>
  path.split('/').slice(0, -1).some(part => part.startsWith('.') || DEPENDENCY_DIRS.has(part.toLowerCase()));

/** The key figures: the images the agent froze as evidence. Before any is frozen, workspace
 *  images outside dependency and hidden folders stand in. By path, at most MAX_FIGURES; the
 *  same image is shown once. */
export function pickFigures(items: AutonomousFile[]): AutonomousFile[] {
  const seen = new Set<string>();
  const images = items.filter(f => IMAGE_EXT.has(ext(f.path)) && f.bytes > 0 && f.bytes <= MAX_FIGURE_BYTES);
  const frozen = images.filter(f => f.source === 'artifact');
  return (frozen.length ? frozen : images.filter(f => f.source === 'workspace' && !fromDependency(f.path)))
    .sort((a, b) => a.path.localeCompare(b.path))
    .filter(f => {
      // Workspace files carry no SHA256, so the same image is recognised by name and size.
      const key = `${baseName(f.path)}:${f.bytes}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    })
    .slice(0, MAX_FIGURES);
}

