import { type ReactNode, useEffect } from "react";

interface Props {
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  /** "start" places footer buttons bottom-left (confirm dialogs), default right. */
  footerAlign?: "start" | "end";
  noHeadClose?: boolean;
}

/** Gray-out overlay + centered dialog window (magpie style). */
export function Modal({
  title,
  onClose,
  children,
  footer,
  footerAlign = "end",
  noHeadClose = false,
}: Props) {
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
      }
    };
    window.addEventListener("keydown", onKeyDown, false);
    return () => {
      window.removeEventListener("keydown", onKeyDown, false);
    };
  }, [onClose]);

  return (
    <div className="overlay" onClick={onClose}>
      <div
        className="dialog"
        aria-modal="true"
        aria-label={title}
        onClick={(event) => event.stopPropagation()}
      >
        <div className="dialog-head">
          <h2 className="dialog-title">{title}</h2>
          {noHeadClose ? null : (
            <button
              type="button"
              className="dialog-close"
              onClick={onClose}
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
          )}
        </div>
        <div className="dialog-body">{children}</div>
        {footer ? (
          <div className={`dialog-foot dialog-foot-${footerAlign}`}>
            {footer}
          </div>
        ) : null}
      </div>
    </div>
  );
}
