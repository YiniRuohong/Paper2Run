import { Modal } from "./Modal";

export type ColorKey = "indigo" | "amber" | "mint" | "coral";

export const COLOR_PRESETS: Record<
  ColorKey,
  { label: string; vars: Record<string, string>; dot: string }
> = {
  indigo: {
    label: "靛蓝",
    dot: "#7C79FF",
    vars: {
      "--hl-soft": "rgba(124, 121, 255, 0.28)",
      "--hl-strong": "rgba(91, 91, 214, 0.55)",
      "--hl-border": "rgba(91, 91, 214, 0.9)",
      "--hl-fill": "rgba(124, 121, 255, 0.14)",
      "--hl-area": "rgba(124, 121, 255, 0.18)",
    },
  },
  amber: {
    label: "琥珀",
    dot: "#E8A33D",
    vars: {
      "--hl-soft": "rgba(255, 205, 105, 0.45)",
      "--hl-strong": "rgba(214, 148, 20, 0.62)",
      "--hl-border": "rgba(214, 148, 20, 0.9)",
      "--hl-fill": "rgba(255, 205, 105, 0.16)",
      "--hl-area": "rgba(255, 205, 105, 0.22)",
    },
  },
  mint: {
    label: "薄荷",
    dot: "#37B98C",
    vars: {
      "--hl-soft": "rgba(52, 199, 138, 0.28)",
      "--hl-strong": "rgba(16, 152, 100, 0.55)",
      "--hl-border": "rgba(16, 152, 100, 0.9)",
      "--hl-fill": "rgba(52, 199, 138, 0.14)",
      "--hl-area": "rgba(52, 199, 138, 0.18)",
    },
  },
  coral: {
    label: "珊瑚",
    dot: "#F0705F",
    vars: {
      "--hl-soft": "rgba(255, 112, 95, 0.28)",
      "--hl-strong": "rgba(214, 60, 45, 0.55)",
      "--hl-border": "rgba(214, 60, 45, 0.9)",
      "--hl-fill": "rgba(255, 112, 95, 0.14)",
      "--hl-area": "rgba(255, 112, 95, 0.18)",
    },
  },
};

interface Props {
  colorKey: ColorKey;
  highlightCount: number;
  strokeCount: number;
  hiddenCount: number;
  docName: string;
  onClose: () => void;
  onColorChange: (color: ColorKey) => void;
  onClearAnnotations: () => void;
  onRestoreHidden: () => void;
}

export function SettingsDialog({
  colorKey,
  highlightCount,
  strokeCount,
  hiddenCount,
  docName,
  onClose,
  onColorChange,
  onClearAnnotations,
  onRestoreHidden,
}: Props) {
  return (
    <Modal title="阅读器设置" onClose={onClose}>
      <section className="settings-section">
        <h3>批注 · 高亮颜色</h3>
        <div className="settings-row">
          <span className="grow">
            PDF 中的高亮、框选与区域批注颜色，立即生效
            <span className="desc">界面浅色 / 深色跟随 Paper2Run 设置</span>
          </span>
          <div className="swatches">
            {(Object.keys(COLOR_PRESETS) as Array<ColorKey>).map((key) => (
              <button
                key={key}
                type="button"
                title={COLOR_PRESETS[key].label}
                aria-label={COLOR_PRESETS[key].label}
                aria-pressed={colorKey === key}
                className={`swatch${colorKey === key ? " on" : ""}`}
                style={{ background: COLOR_PRESETS[key].dot }}
                onClick={() => onColorChange(key)}
              />
            ))}
          </div>
        </div>
      </section>

      <section className="settings-section">
        <h3>批注数据</h3>
        <div className="settings-row">
          <span className="grow">
            当前论文：{docName || "未打开"}
            <span className="desc">
              {highlightCount} 条批注 · {strokeCount} 处笔画 · 只保存在本机浏览器
            </span>
          </span>
        </div>
        <div className="settings-actions">
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            disabled={!docName || (highlightCount === 0 && strokeCount === 0)}
            onClick={onClearAnnotations}
          >
            清空当前论文的批注与笔画
          </button>
        </div>
      </section>

      <section className="settings-section">
        <h3>书架</h3>
        <div className="settings-row">
          <span className="grow">
            书架列出所有项目里上传的 PDF 论文
            <span className="desc">已移出书架 {hiddenCount} 篇</span>
          </span>
        </div>
        <div className="settings-actions">
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            disabled={hiddenCount === 0}
            onClick={onRestoreHidden}
          >
            恢复移出书架的论文
          </button>
        </div>
      </section>

      <section className="settings-section">
        <h3>关于</h3>
        <p className="settings-about">
          PTR 批注器，基于 react-pdf-highlighter（MIT）定制 · 划词评论 · Alt
          拖框区域批注 · 绘图工具
        </p>
      </section>
    </Modal>
  );
}
