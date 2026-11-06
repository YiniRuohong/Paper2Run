import { useEffect, useState } from "react";
import type { ToolKind } from "./DrawingLayer";

export const HIGHLIGHTER_COLORS = ["#FFE066", "#A5E8A2", "#9AD0FF"];
export const PEN_COLORS = ["#1D1D1F", "#E23A2E", "#2E6BE2", "#12A150"];
const PEN_WIDTHS = [2, 4, 7];
const ERASER_MIN = 6;
const ERASER_MAX = 30;

const IconHighlighter = () => (
  <svg
    width="16"
    height="16"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.8"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    <path d="m9 11-6 6v3h9l6-6" />
    <path d="m22 12-8.5 8.5a2.12 2.12 0 0 1-3 3L3 16a2.12 2.12 0 0 1 3-3l8.5 8.5" />
  </svg>
);

const IconPen = () => (
  <svg
    width="16"
    height="16"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.8"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    <path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z" />
    <path d="m15 5 4 4" />
  </svg>
);

const IconEraser = () => (
  <svg
    width="16"
    height="16"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.8"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    <path d="m7 21-4.3-4.3c-1-1-1-2.5 0-3.4l9.6-9.6c1-1 2.5-1 3.4 0l5.6 5.6c1 1 1 2.5 0 3.4L13 21" />
    <path d="M22 21H7" />
    <path d="m5 11 9 9" />
  </svg>
);

const IconRuler = () => (
  <svg
    width="16"
    height="16"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.8"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    <path d="M21.3 15.3a2.4 2.4 0 0 1 0 3.4l-2.6 2.6a2.4 2.4 0 0 1-3.4 0L2.7 8.7a2.41 2.41 0 0 1 0-3.4l2.6-2.6a2.41 2.41 0 0 1 3.4 0Z" />
    <path d="m14.5 12.5 2-2" />
    <path d="m11.5 9.5 2-2" />
    <path d="m8.5 6.5 2-2" />
    <path d="m17.5 15.5 2-2" />
  </svg>
);

interface Props {
  tool: ToolKind | null;
  color: string;
  thickness: number;
  zoom: number;
  canUndo: boolean;
  canRedo: boolean;
  onToolChange: (tool: ToolKind | null) => void;
  onColorChange: (color: string) => void;
  onThicknessChange: (width: number) => void;
  onZoomIn: () => void;
  onZoomOut: () => void;
  onZoomReset: () => void;
  onUndo: () => void;
  onRedo: () => void;
}

const IconUndo = () => (
  <svg
    width="16"
    height="16"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.8"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    <path d="M3 7v6h6" />
    <path d="M21 17a9 9 0 0 0-9-9 9 9 0 0 0-6 2.3L3 13" />
  </svg>
);

const IconRedo = () => (
  <svg
    width="16"
    height="16"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.8"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    <path d="M21 7v6h-6" />
    <path d="M3 17a9 9 0 0 1 9-9 9 9 0 0 1 6 2.3L21 13" />
  </svg>
);

const IconGrab = () => (
  <svg
    width="16"
    height="16"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.8"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    <path d="M18 11V6a2 2 0 0 0-4 0v5" />
    <path d="M14 10V4a2 2 0 0 0-4 0v6" />
    <path d="M10 10.5V6a2 2 0 0 0-4 0v8" />
    <path d="M18 8a2 2 0 0 1 4 0v6a8 8 0 0 1-8 8h-2c-2.8 0-4.5-.86-5.99-2.34l-3.42-3.41c-.8-.79-.8-2.07 0-2.86a2.06 2.06 0 0 1 2.88 0L10 16" />
  </svg>
);

export function Toolbar({
  tool,
  color,
  thickness,
  zoom,
  canUndo,
  canRedo,
  onToolChange,
  onColorChange,
  onThicknessChange,
  onZoomIn,
  onZoomOut,
  onZoomReset,
  onUndo,
  onRedo,
}: Props) {
  const toggle = (next: ToolKind) => onToolChange(tool === next ? null : next);

  /* dropdown panel collapses when clicking anywhere outside the toolbar
     and outside the pdf stage */
  const [panelOpen, setPanelOpen] = useState(false);

  useEffect(() => {
    setPanelOpen(tool === "highlighter" || tool === "pen" || tool === "eraser");
  }, [tool]);

  /* 点击面板以外的任何地方（包括 PDF 区域）都收起；
     在面板内部操作（选颜色/拖滑块）不收起。 */
  useEffect(() => {
    if (!panelOpen) {
      return;
    }
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as HTMLElement;
      if (!target.closest(".tool-panel")) {
        setPanelOpen(false);
      }
    };
    document.addEventListener("pointerdown", onPointerDown, true);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown, true);
    };
  }, [panelOpen]);

  const showPanel =
    panelOpen &&
    (tool === "highlighter" || tool === "pen" || tool === "eraser");

  return (
    <div className="toolbar-wrap">
      <div className="history-group">
        <button
          type="button"
          className="tool-btn"
          onClick={onUndo}
          disabled={!canUndo}
          aria-label="上一步"
          title="上一步（撤回绘图，最多 5 步）"
        >
          <IconUndo />
        </button>
        <button
          type="button"
          className="tool-btn"
          onClick={onRedo}
          disabled={!canRedo}
          aria-label="下一步"
          title="下一步（恢复绘图，最多 5 步）"
        >
          <IconRedo />
        </button>
      </div>

      <span className="toolbar-divider" aria-hidden="true" />

      <div className="zoom-group">
        <button
          type="button"
          className="tool-btn"
          onClick={onZoomOut}
          aria-label="缩小"
          title="缩小"
        >
          <svg
            width="15"
            height="15"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinecap="round"
            aria-hidden="true"
          >
            <path d="M5 12h14" />
          </svg>
        </button>
        <button
          type="button"
          className="zoom-label"
          onClick={onZoomReset}
          aria-label="重置缩放"
          title="重置缩放"
        >
          {Math.round(zoom * 100)}%
        </button>
        <button
          type="button"
          className="tool-btn"
          onClick={onZoomIn}
          aria-label="放大"
          title="放大"
        >
          <svg
            width="15"
            height="15"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinecap="round"
            aria-hidden="true"
          >
            <path d="M12 5v14M5 12h14" />
          </svg>
        </button>
      </div>

      <span className="toolbar-divider" aria-hidden="true" />

      <button
        type="button"
        className={`tool-btn${tool === "grab" ? " on" : ""}`}
        onClick={() => toggle("grab")}
        aria-label="抓手"
        title="抓手（拖拽平移页面）"
      >
        <IconGrab />
      </button>
      <button
        type="button"
        className={`tool-btn${tool === "highlighter" ? " on" : ""}`}
        onClick={() => toggle("highlighter")}
        aria-label="荧光笔"
        title="荧光笔"
      >
        <IconHighlighter />
      </button>
      <button
        type="button"
        className={`tool-btn${tool === "pen" ? " on" : ""}`}
        onClick={() => toggle("pen")}
        aria-label="圆珠笔"
        title="圆珠笔"
      >
        <IconPen />
      </button>
      <button
        type="button"
        className={`tool-btn${tool === "eraser" ? " on" : ""}`}
        onClick={() => toggle("eraser")}
        aria-label="橡皮擦"
        title="橡皮擦"
      >
        <IconEraser />
      </button>
      <button
        type="button"
        className={`tool-btn${tool === "ruler" ? " on" : ""}`}
        onClick={() => toggle("ruler")}
        aria-label="直尺"
        title="直尺（点击起点，再点击终点划线）"
      >
        <IconRuler />
      </button>

      {showPanel ? (
        <div className="tool-panel">
          {tool === "highlighter"
            ? HIGHLIGHTER_COLORS.map((c) => (
                <button
                  key={c}
                  type="button"
                  className={`color-dot${color === c ? " on" : ""}`}
                  style={{ background: c }}
                  onClick={() => onColorChange(c)}
                  aria-label={`颜色 ${c}`}
                  title="荧光笔颜色"
                />
              ))
            : null}
          {tool === "pen" ? (
            <>
              {PEN_COLORS.map((c) => (
                <button
                  key={c}
                  type="button"
                  className={`color-dot${color === c ? " on" : ""}`}
                  style={{ background: c }}
                  onClick={() => onColorChange(c)}
                  aria-label={`颜色 ${c}`}
                  title="笔颜色"
                />
              ))}
              <span className="panel-divider" aria-hidden="true" />
              {PEN_WIDTHS.map((w) => (
                <button
                  key={w}
                  type="button"
                  className={`width-opt${thickness === w ? " on" : ""}`}
                  onClick={() => onThicknessChange(w)}
                  aria-label={`粗细 ${w}`}
                  title={`粗细 ${w}`}
                >
                  <span className="bar" style={{ height: w * 1.6 }} />
                </button>
              ))}
            </>
          ) : null}
          {tool === "eraser" ? (
            <div className="eraser-size-row">
              <span className="eraser-size-label">大小</span>
              <label
                className="eraser-slider"
                aria-label={`橡皮擦大小 ${thickness}px`}
              >
                <input
                  type="range"
                  min={ERASER_MIN}
                  max={ERASER_MAX}
                  step={1}
                  value={thickness}
                  onChange={(event) =>
                    onThicknessChange(Number(event.target.value))
                  }
                />
              </label>
              <span className="eraser-size-value">{thickness}px</span>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
