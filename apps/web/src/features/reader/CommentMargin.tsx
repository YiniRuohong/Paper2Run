import { type ReactNode, useEffect, useRef, useState } from "react";
import type { SearchableHighlight } from "./SearchDialog";

interface Props {
  highlights: Array<SearchableHighlight>;
  activeId: string | null;
  onHover: (id: string | null) => void;
  onEdit: (highlight: SearchableHighlight) => void;
}

const BUBBLE_H = 34;
const SIDE_GAP = 16;
const QUEUE_GAP_Y = 8;

const truncate = (text: string, max = 14) =>
  text.length > max ? `${text.slice(0, max)}…` : text;

/**
 * 评论气泡：渲染在 PDF 页面右侧空白区，右边缘统一对齐；
 * 每帧从页面 DOM 实时换算位置——缩放/滚动/翻页时与页面绝对贴合。
 */
export function CommentMargin({
  highlights,
  activeId,
  onHover,
  onEdit,
}: Props) {
  const contentRef = useRef<HTMLDivElement | null>(null);
  const [, setResizeTick] = useState(0);

  useEffect(() => {
    const onResize = () => setResizeTick((t) => t + 1);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  /* hover 走原生事件委托 */
  useEffect(() => {
    const el = contentRef.current;
    if (!el) {
      return;
    }
    const onOver = (event: MouseEvent) => {
      const bubble = (event.target as HTMLElement).closest?.(
        ".comment-bubble",
      ) as HTMLElement | null;
      if (bubble?.dataset.id) {
        onHover(bubble.dataset.id);
      }
    };
    const onOut = (event: MouseEvent) => {
      const to = event.relatedTarget as HTMLElement | null;
      if (!to || !to.closest?.(".comment-bubble")) {
        onHover(null);
      }
    };
    el.addEventListener("mouseover", onOver);
    el.addEventListener("mouseout", onOut);
    return () => {
      el.removeEventListener("mouseover", onOver);
      el.removeEventListener("mouseout", onOut);
    };
  }, [onHover]);

  /* hover 时被评论文字的变色高亮框 */
  const hoverOverlay: ReactNode = (() => {
    if (!activeId) {
      return null;
    }
    const h = highlights.find((item) => item.id === activeId);
    if (!h) {
      return null;
    }
    const position = h.position;
    const boxes = (
      position.rects?.length ? position.rects : [position.boundingRect]
    ).map((scaled) => ({
      page: scaled.pageNumber ?? position.pageNumber,
      leftRatio: scaled.x1 / (scaled.width || 1),
      topRatio: scaled.y1 / (scaled.height || 1),
      widthRatio: (scaled.x2 - scaled.x1) / (scaled.width || 1),
      heightRatio: (scaled.y2 - scaled.y1) / (scaled.height || 1),
    }));
    return boxes.map((box) => (
      <div
        key={`hover-${box.leftRatio.toFixed(3)}-${box.topRatio.toFixed(3)}`}
        className="comment-hover-mark"
        data-hl-index={0}
        data-page={box.page}
        data-lr={box.leftRatio}
        data-tr={box.topRatio}
        data-wr={box.widthRatio}
        data-hr={box.heightRatio}
        style={{ left: 0, top: 0, width: 0, height: 0 }}
      />
    ));
  })();

  const bubbles: ReactNode = highlights.map((h) => {
    const b = h.position.boundingRect;
    const firstRect = h.position.rects?.[0];
    const yRatio = firstRect
      ? firstRect.y1 / (firstRect.height || 1)
      : b.y1 / (b.height || 1);
    const text = h.comment.text.trim();
    const label = text
      ? truncate(text)
      : h.content.image
        ? "[图]"
        : "（未填写评论）";
    return (
      <button
        key={h.id}
        type="button"
        className={`comment-bubble${activeId === h.id ? " on" : ""}`}
        style={{ left: 0, top: 0 }}
        data-id={h.id}
        data-page={h.position.pageNumber}
        data-yr={yRatio}
        onClick={() => onEdit(h)}
        title="点击编辑评论"
      >
        {h.comment.emoji ? `${h.comment.emoji} ` : ""}
        {label}
      </button>
    );
  });

  /* rAF 循环：每帧按页面 DOM 实际位置摆放气泡与高亮框 */
  useEffect(() => {
    let raf = 0;
    const frame = () => {
      const content = contentRef.current;
      const stage = content?.closest(".pdf-stage");
      if (!stage || !content) {
        raf = requestAnimationFrame(frame);
        return;
      }
      const stageRect = stage.getBoundingClientRect();

      const pageRect = (pageNumber: number) => {
        const page = stage.querySelector(
          `div[data-page-number="${pageNumber}"]`,
        );
        if (!page) {
          return null;
        }
        const pr = page.getBoundingClientRect();
        return {
          left: pr.left - stageRect.left,
          top: pr.top - stageRect.top,
          width: pr.width,
          height: pr.height,
          right: pr.right - stageRect.left,
        };
      };

      // 1) 气泡：右缘统一对齐页面右侧 + 间距；y 重叠向下排队
      const bubbleEls = [
        ...content.querySelectorAll<HTMLElement>(".comment-bubble"),
      ];
      const placed: Array<{ top: number; bottom: number }> = [];
      const rows = bubbleEls.map((el) => ({
        el,
        yRatio: Number(el.dataset.yr ?? 0),
        page: Number(el.dataset.page ?? 1),
      }));
      rows.sort((a, b) => {
        const pa = pageRect(a.page);
        const pb = pageRect(b.page);
        const ya = pa ? pa.top + a.yRatio * pa.height : 0;
        const yb = pb ? pb.top + b.yRatio * pb.height : 0;
        return ya - yb;
      });
      for (const row of rows) {
        const pr = pageRect(row.page);
        if (!pr) {
          row.el.style.visibility = "hidden";
          continue;
        }
        const anchorY = pr.top + row.yRatio * pr.height;
        let top = anchorY - BUBBLE_H / 2;
        for (const p of placed) {
          if (
            top < p.bottom + QUEUE_GAP_Y &&
            p.top < top + BUBBLE_H + QUEUE_GAP_Y
          ) {
            top = p.bottom + QUEUE_GAP_Y;
          }
        }
        placed.push({ top, bottom: top + BUBBLE_H });
        // 嵌在 Paper2Run 外壳里时展示区较窄：页面右侧放不下就贴着展示区右缘，
        // 宁可压住页边距，也不要被裁掉。
        const maxLeft = stageRect.width - row.el.offsetWidth - SIDE_GAP;
        row.el.style.left = `${Math.max(0, Math.min(pr.right + SIDE_GAP, maxLeft))}px`;
        row.el.style.top = `${top}px`;
        row.el.style.visibility = "visible";
      }

      // 2) hover 高亮框
      for (const mark of content.querySelectorAll<HTMLElement>(
        ".comment-hover-mark",
      )) {
        const page = pageRect(Number(mark.dataset.page ?? 1));
        if (!page) {
          mark.style.visibility = "hidden";
          continue;
        }
        mark.style.left = `${page.left + Number(mark.dataset.lr) * page.width}px`;
        mark.style.top = `${page.top + Number(mark.dataset.tr) * page.height}px`;
        mark.style.width = `${Number(mark.dataset.wr) * page.width}px`;
        mark.style.height = `${Number(mark.dataset.hr) * page.height}px`;
        mark.style.visibility = "visible";
      }

      raf = requestAnimationFrame(frame);
    };
    raf = requestAnimationFrame(frame);
    // biome-ignore lint/correctness/useExhaustiveDependencies: 循环每帧读 DOM，气泡/高亮由 React 渲染
    return () => cancelAnimationFrame(raf);
  }, []);

  return (
    <div className="comment-margin-layer">
      <div ref={contentRef} className="comment-margin-content">
        {hoverOverlay}
        {bubbles}
      </div>
    </div>
  );
}
