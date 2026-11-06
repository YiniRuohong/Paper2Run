import { useEffect, useRef, useState } from "react";

interface Props {
  mode?: "create" | "edit";
  initialText?: string;
  onCancel: () => void;
  onSubmit: (text: string) => void;
}

/** Centered, draggable comment dialog — no dimming behind (transparent overlay). */
export function CommentDialog({
  mode = "create",
  initialText = "",
  onCancel,
  onSubmit,
}: Props) {
  const [text, setText] = useState(initialText);
  const [offset, setOffset] = useState({ x: 0, y: 0 });
  const drag = useRef<{
    pointerX: number;
    pointerY: number;
    offsetX: number;
    offsetY: number;
  } | null>(null);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onCancel();
      }
    };
    window.addEventListener("keydown", onKeyDown, false);
    return () => {
      window.removeEventListener("keydown", onKeyDown, false);
    };
  }, [onCancel]);

  const onDragStart = (event: React.PointerEvent<HTMLDivElement>) => {
    // 从按钮（如关闭 ×）起手时不进入拖拽，否则 pointer capture 会让 click 落空
    if ((event.target as HTMLElement).closest("button")) {
      return;
    }
    drag.current = {
      pointerX: event.clientX,
      pointerY: event.clientY,
      offsetX: offset.x,
      offsetY: offset.y,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
  };

  const onDragMove = (event: React.PointerEvent<HTMLDivElement>) => {
    const current = drag.current;
    if (!current) {
      return;
    }
    setOffset({
      x: current.offsetX + (event.clientX - current.pointerX),
      y: current.offsetY + (event.clientY - current.pointerY),
    });
  };

  const onDragEnd = () => {
    drag.current = null;
  };

  return (
    <div className="comment-overlay">
      <section
        className="comment-dialog"
        aria-label={mode === "edit" ? "编辑评论" : "添加评论"}
        style={{
          transform: `translate(calc(-50% + ${offset.x}px), calc(-50% + ${offset.y}px))`,
        }}
      >
        <div
          className="comment-head"
          onPointerDown={onDragStart}
          onPointerMove={onDragMove}
          onPointerUp={onDragEnd}
        >
          <span className="comment-grip" aria-hidden="true">
            <svg
              width="12"
              height="12"
              viewBox="0 0 12 12"
              fill="currentColor"
              aria-hidden="true"
            >
              <circle cx="3" cy="2.5" r="1" />
              <circle cx="9" cy="2.5" r="1" />
              <circle cx="3" cy="6" r="1" />
              <circle cx="9" cy="6" r="1" />
              <circle cx="3" cy="9.5" r="1" />
              <circle cx="9" cy="9.5" r="1" />
            </svg>
          </span>
          <h2 className="comment-title">
            {mode === "edit" ? "编辑评论" : "添加评论"}
          </h2>
          <button
            type="button"
            className="dialog-close"
            onClick={onCancel}
            aria-label="关闭"
          >
            <svg
              width="12"
              height="12"
              viewBox="0 0 12 12"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.6"
              strokeLinecap="round"
              aria-hidden="true"
            >
              <path d="M2 2l8 8M10 2l-8 8" />
            </svg>
          </button>
        </div>
        <div className="comment-body">
          <textarea
            className="comment-textarea"
            // biome-ignore lint/a11y/noAutofocus: This is an example app
            autoFocus
            placeholder="写下你的评论…"
            value={text}
            onChange={(event) => setText(event.target.value)}
          />
        </div>
        <div className="comment-foot">
          <button
            type="button"
            className="btn btn-secondary"
            onClick={onCancel}
          >
            取消
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={!text.trim()}
            onClick={() => onSubmit(text.trim())}
          >
            保存
          </button>
        </div>
      </section>
    </div>
  );
}
