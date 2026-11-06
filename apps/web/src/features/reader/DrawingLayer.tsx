import { type CSSProperties, useEffect, useRef, useState } from "react";

export type ToolKind = "highlighter" | "pen" | "eraser" | "ruler" | "grab";

export interface StrokeRect {
  page: number;
  xr: number; // normalized 0..1 relative to the page element
  yr: number;
  wr: number;
  hr: number;
}

export interface Stroke {
  id: string;
  kind: "highlighter" | "pen" | "ruler";
  color: string;
  width: number;
  page: number; // anchor page (pen/ruler)
  points?: Array<{ xr: number; yr: number }>; // pen / ruler (page-normalized)
  rects?: Array<StrokeRect>; // highlighter text-line marks
}

interface Props {
  tool: ToolKind | null;
  color: string;
  thickness: number;
  strokes: Array<Stroke>;
  onAdd: (stroke: Stroke) => void;
  onErase: (id: string) => void;
  onEraseRect: (id: string, rectIndex: number) => void;
}

/* Custom tool cursors as data-URI SVGs (hotspot at the tool tip). */
function cursorFor(
  tool: ToolKind,
  color: string,
  thickness: number,
): CSSProperties {
  const uri = (svg: string, hx: number, hy: number) =>
    `url("data:image/svg+xml,${encodeURIComponent(svg)}") ${hx} ${hy}, crosshair`;
  switch (tool) {
    case "pen":
      return {
        cursor: uri(
          `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24"><circle cx="12" cy="12" r="4" fill="${color}" stroke="white" stroke-width="1.2"/></svg>`,
          12,
          12,
        ),
      };
    case "highlighter":
      return {
        cursor: uri(
          `<svg xmlns="http://www.w3.org/2000/svg" width="30" height="34"><rect x="11" y="3" width="8" height="15" rx="2" fill="${color}" opacity="0.9"/><path d="M11 18 L19 18 L15.5 30 Z" fill="${color}" opacity="0.75"/></svg>`,
          15,
          30,
        ),
      };
    case "eraser": {
      const r = Math.max(6, Math.min(thickness, 30));
      const size = r * 2 + 6;
      return {
        cursor: uri(
          `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}"><circle cx="${size / 2}" cy="${size / 2}" r="${r}" fill="rgba(0,0,0,0.05)" stroke="#8a8a90" stroke-width="1.5"/></svg>`,
          size / 2,
          size / 2,
        ),
      };
    }
    case "grab":
      return { cursor: "grab" };
    case "ruler":
      return { cursor: "crosshair" };
    default:
      return {};
  }
}

/** 点到线段的最短距离 */
function distToSegment(
  px: number,
  py: number,
  x1: number,
  y1: number,
  x2: number,
  y2: number,
): number {
  const dx = x2 - x1;
  const dy = y2 - y1;
  const lenSq = dx * dx + dy * dy;
  let t = lenSq === 0 ? 0 : ((px - x1) * dx + (py - y1) * dy) / lenSq;
  t = Math.max(0, Math.min(1, t));
  return Math.hypot(px - (x1 + t * dx), py - (y1 + t * dy));
}

export function DrawingLayer({
  tool,
  color,
  thickness,
  strokes,
  onAdd,
  onErase,
  onEraseRect,
}: Props) {
  const svgRef = useRef<SVGSVGElement | null>(null);
  const strokesRef = useRef<Array<Stroke>>(strokes);
  const draftRef = useRef<Stroke | null>(null);
  const draftRectsRef = useRef<Array<StrokeRect>>([]);
  const rulerRef = useRef<{
    start: { page: number; xr: number; yr: number };
    preview: { page: number; xr: number; yr: number } | null;
  } | null>(null);
  const grabRef = useRef<{
    clientX: number;
    clientY: number;
    scrollTop: number;
    scrollLeft: number;
  } | null>(null);
  const hlCharsRef = useRef<Map<string, StrokeRect>>(new Map());
  const lastSampleRef = useRef<{ x: number; y: number } | null>(null);
  const [grabbing, setGrabbing] = useState(false);

  useEffect(() => {
    strokesRef.current = strokes;
  }, [strokes]);

  /* ---- page/coordinate helpers ---- */

  const scrollEl = () =>
    svgRef.current
      ?.closest(".pdf-stage")
      ?.querySelector('[class*="_container"]') as HTMLElement | null;

  const pageEl = (pageNumber: number): HTMLElement | null => {
    const stage = svgRef.current?.closest(".pdf-stage");
    return stage?.querySelector(
      `div[data-page-number="${pageNumber}"]`,
    ) as HTMLElement | null;
  };

  const pageRect = (pageNumber: number, stageRect: DOMRect) => {
    const el = pageEl(pageNumber);
    if (!el) {
      return null;
    }
    const pr = el.getBoundingClientRect();
    return {
      left: pr.left - stageRect.left,
      top: pr.top - stageRect.top,
      width: pr.width,
      height: pr.height,
    };
  };

  /* which page element sits under the screen point (temporarily
     click-transparenting ourselves and annotation overlays) */
  const pageAt = (clientX: number, clientY: number) => {
    const overlays = document.querySelectorAll<HTMLElement>(
      '.pdf-stage [class*="highlight-layer"], .pdf-stage [class*="highlightLayer"]',
    );
    const restore: Array<() => void> = [];
    const self = svgRef.current;
    if (self) {
      const prev = self.style.pointerEvents;
      self.style.pointerEvents = "none";
      restore.push(() => {
        self.style.pointerEvents = prev;
      });
    }
    for (const el of overlays) {
      const prev = el.style.pointerEvents;
      el.style.pointerEvents = "none";
      restore.push(() => {
        el.style.pointerEvents = prev;
      });
    }
    const hit = document
      .elementFromPoint(clientX, clientY)
      ?.closest("[data-page-number]");
    for (const fn of restore) {
      fn();
    }
    return (hit as HTMLElement | null) ?? null;
  };

  const pageNumberOf = (clientX: number, clientY: number): number | null => {
    const num = pageAt(clientX, clientY)?.getAttribute("data-page-number");
    return num ? Number(num) : null;
  };

  const normOf = (clientX: number, clientY: number, pageNumber: number) => {
    const svg = svgRef.current;
    if (!svg) {
      return { xr: 0, yr: 0 };
    }
    const stageRect = svg.getBoundingClientRect();
    const pr = pageRect(pageNumber, stageRect);
    if (!pr) {
      return { xr: 0, yr: 0 };
    }
    // clamp 到页面内：拖出 PDF 边界时吸附到页面边缘
    const clamp01 = (v: number) => Math.min(1, Math.max(0, v));
    return {
      xr: clamp01((clientX - stageRect.left - pr.left) / pr.width),
      yr: clamp01((clientY - stageRect.top - pr.top) / pr.height),
    };
  };

  /* highlighter: 字符粒度采样——每个命中的字母/字符/单字生成一个矩形。
     绘图层 svg 与批注高亮层位于文字上方，采样前临时透明化再探测 caret。 */
  const sampleTextLine = (clientX: number, clientY: number) => {
    const overlays = document.querySelectorAll<HTMLElement>(
      '.pdf-stage [class*="highlight-layer"], .pdf-stage [class*="highlightLayer"]',
    );
    const restore: Array<() => void> = [];
    const self = svgRef.current;
    const targets: Array<HTMLElement | SVGSVGElement> = [
      ...overlays,
      ...(self ? [self] : []),
    ];
    for (const el of targets) {
      const prev = el.style.pointerEvents;
      el.style.pointerEvents = "none";
      restore.push(() => {
        el.style.pointerEvents = prev;
      });
    }
    try {
      const caret = document.caretRangeFromPoint?.(clientX, clientY);
      if (!caret || caret.startContainer.nodeType !== Node.TEXT_NODE) {
        return;
      }
      const el = caret.startContainer.parentElement?.closest(
        "[data-page-number]",
      ) as HTMLElement | null;
      if (!el) {
        return;
      }
      const pageNumber = Number(el.getAttribute("data-page-number"));
      const textNode = caret.startContainer as Text;
      const charRange = document.createRange();
      const start = caret.startOffset;
      charRange.setStart(textNode, start);
      charRange.setEnd(textNode, Math.min(start + 1, textNode.length));
      const charRect = charRange.getBoundingClientRect();
      if (!charRect || charRect.width === 0 || charRect.height === 0) {
        return;
      }
      const svg = svgRef.current;
      if (!svg) {
        return;
      }
      const stageRect = svg.getBoundingClientRect();
      const pr = pageRect(pageNumber, stageRect);
      if (!pr) {
        return;
      }
      // charRect 是 client 绝对坐标，先转到相对 stage（与 pr 同基准）
      const charLeft = charRect.left - stageRect.left;
      const charTop = charRect.top - stageRect.top;
      const char: StrokeRect = {
        page: pageNumber,
        xr: (charLeft - pr.left) / pr.width,
        yr: (charTop - pr.top) / pr.height,
        wr: charRect.width / pr.width,
        hr: charRect.height / pr.height,
      };
      // 同一字符去重：以左上角归一化坐标为键
      const key = `${char.page}:${char.xr.toFixed(3)}:${char.yr.toFixed(3)}`;
      if (!hlCharsRef.current.has(key)) {
        hlCharsRef.current.set(key, char);
      }
      draftRectsRef.current = [...hlCharsRef.current.values()];
    } finally {
      for (const fn of restore) {
        fn();
      }
    }
  };

  const commitHighlighter = () => {
    const rects = [...hlCharsRef.current.values()].filter((r) => r.wr > 0.002);
    hlCharsRef.current.clear();
    lastSampleRef.current = null;
    draftRectsRef.current = [];
    if (rects.length === 0) {
      return;
    }
    const height = rects.reduce((sum, r) => sum + r.hr, 0) / rects.length;
    onAdd({
      id: String(Math.random()).slice(2),
      kind: "highlighter",
      color,
      width: height,
      page: rects[0].page,
      rects,
    });
  };

  /* eraser: hit-test in screen space by re-measuring pages every call */
  const eraseAt = (clientX: number, clientY: number) => {
    try {
      const svg = svgRef.current;
      if (!svg) {
        return;
      }
      // 命中测试使用相对 stage 的坐标（与笔画渲染基准一致）
      const stageRect = svg.getBoundingClientRect();
      const hitX = clientX - stageRect.left;
      const hitY = clientY - stageRect.top;
      let bestId: string | null = null;
      let bestRectIndex = -1;
      let bestDist = Number.POSITIVE_INFINITY;
      for (const stroke of strokesRef.current) {
        const pr = pageRect(stroke.page, stageRect);
        if (!pr) {
          continue;
        }
        if (stroke.rects) {
          for (let i = 0; i < stroke.rects.length; i++) {
            const r = stroke.rects[i];
            const rPr =
              r.page === stroke.page ? pr : pageRect(r.page, stageRect);
            if (!rPr) {
              continue;
            }
            const rx = rPr.left + r.xr * rPr.width;
            const ry = rPr.top + r.yr * rPr.height;
            const rw = r.wr * rPr.width;
            const rh = r.hr * rPr.height;
            const pad = 4;
            if (
              hitX >= rx - pad &&
              hitX <= rx + rw + pad &&
              hitY >= ry - pad &&
              hitY <= ry + rh + pad
            ) {
              const dist = Math.hypot(
                hitX - (rx + rw / 2),
                hitY - (ry + rh / 2),
              );
              if (dist < bestDist) {
                bestDist = dist;
                bestId = stroke.id;
                bestRectIndex = i;
              }
            }
          }
        } else {
          const reach = stroke.width / 2 + 8;
          let prevX: number | null = null;
          let prevY: number | null = null;
          for (const p of stroke.points ?? []) {
            const px = pr.left + p.xr * pr.width;
            const py = pr.top + p.yr * pr.height;
            if (prevX !== null && prevY !== null) {
              const dist = distToSegment(hitX, hitY, prevX, prevY, px, py);
              if (dist < reach && dist < bestDist) {
                bestDist = dist;
                bestId = stroke.id;
                bestRectIndex = -1;
              }
            }
            prevX = px;
            prevY = py;
          }
        }
      }
      if (!bestId) {
        return;
      }
      if (bestRectIndex >= 0) {
        onEraseRect(bestId, bestRectIndex);
      } else {
        onErase(bestId);
      }
    } catch (eraseError) {
      console.error("[eraseAt ERROR]", eraseError);
    }
  };

  /* ---- pointer handlers ---- */

  const onPointerDown = (event: React.PointerEvent<SVGSVGElement>) => {
    if (!tool) {
      return;
    }
    event.currentTarget.setPointerCapture(event.pointerId);

    if (tool === "grab") {
      const scroll = scrollEl();
      grabRef.current = {
        clientX: event.clientX,
        clientY: event.clientY,
        scrollTop: scroll?.scrollTop ?? 0,
        scrollLeft: scroll?.scrollLeft ?? 0,
      };
      setGrabbing(true);
      return;
    }
    if (tool === "eraser") {
      eraseAt(event.clientX, event.clientY);
      return;
    }
    if (tool === "ruler") {
      const page = pageNumberOf(event.clientX, event.clientY);
      if (page === null) {
        return;
      }
      const { xr, yr } = normOf(event.clientX, event.clientY, page);
      const current = rulerRef.current;
      if (!current) {
        rulerRef.current = { start: { page, xr, yr }, preview: null };
      } else {
        onAdd({
          id: String(Math.random()).slice(2),
          kind: "ruler",
          color: "#1d1d1f",
          width: 2.5,
          page: current.start.page,
          points: [
            { xr: current.start.xr, yr: current.start.yr },
            { xr, yr },
          ],
        });
        rulerRef.current = null;
      }
      return;
    }
    if (tool === "highlighter") {
      hlCharsRef.current.clear();
      lastSampleRef.current = { x: event.clientX, y: event.clientY };
      draftRectsRef.current = [];
      return;
    }
    // pen
    const page = pageNumberOf(event.clientX, event.clientY);
    if (page === null) {
      return;
    }
    const { xr, yr } = normOf(event.clientX, event.clientY, page);
    draftRef.current = {
      id: String(Math.random()).slice(2),
      kind: tool,
      color,
      width: thickness,
      page,
      points: [{ xr, yr }],
    };
  };

  const onPointerMove = (event: React.PointerEvent<SVGSVGElement>) => {
    if (!tool) {
      return;
    }

    if (tool === "grab") {
      const grab = grabRef.current;
      if (grab && event.buttons & 1) {
        const scroll = scrollEl();
        scroll?.scrollTo({
          top: grab.scrollTop - (event.clientY - grab.clientY),
          left: grab.scrollLeft - (event.clientX - grab.clientX),
        });
      }
      return;
    }
    if (tool === "eraser") {
      if (event.buttons & 1) {
        eraseAt(event.clientX, event.clientY);
      }
      return;
    }
    if (tool === "ruler") {
      const current = rulerRef.current;
      if (current) {
        const page = pageNumberOf(event.clientX, event.clientY);
        if (page !== null) {
          const { xr, yr } = normOf(event.clientX, event.clientY, page);
          current.preview = { page, xr, yr };
        }
      }
      return;
    }
    if (tool === "highlighter") {
      const last = lastSampleRef.current;
      if (last) {
        const moved = Math.hypot(
          event.clientX - last.x,
          event.clientY - last.y,
        );
        if (moved < 7) {
          return;
        }
      }
      lastSampleRef.current = { x: event.clientX, y: event.clientY };
      sampleTextLine(event.clientX, event.clientY);
      return;
    }
    const current = draftRef.current;
    if (!current) {
      return;
    }
    const { xr, yr } = normOf(event.clientX, event.clientY, current.page);
    const points = current.points ?? [];
    current.points = points;
    const lastPoint = points[points.length - 1];
    if (!lastPoint) {
      points.push({ xr, yr });
      return;
    }
    const svg = svgRef.current;
    if (!svg) {
      return;
    }
    const pr = pageRect(current.page, svg.getBoundingClientRect());
    if (!pr) {
      return;
    }
    if (
      Math.hypot(
        (xr - lastPoint.xr) * pr.width,
        (yr - lastPoint.yr) * pr.height,
      ) < 4
    ) {
      return; // throttle tiny moves
    }
    points.push({ xr, yr });
  };

  const onPointerUp = () => {
    if (tool === "grab") {
      grabRef.current = null;
      setGrabbing(false);
      return;
    }
    if (tool === "pen" && draftRef.current) {
      const current = draftRef.current;
      const points = [...(current.points ?? [])];
      if (points.length === 1) {
        points.push({ xr: points[0].xr + 0.004, yr: points[0].yr + 0.004 });
      }
      onAdd({ ...current, points });
      draftRef.current = null;
      return;
    }
    if (tool === "highlighter") {
      commitHighlighter();
    }
  };

  /* ---- rAF render loop: glue every stroke/draft to its page, every frame ---- */

  const strokesGRef = useRef<SVGGElement | null>(null);

  useEffect(() => {
    const svg = svgRef.current;
    const g = strokesGRef.current;
    if (!svg || !g) {
      return;
    }
    let raf = 0;
    const frame = () => {
      const stageRect = svg.getBoundingClientRect();
      const strokesNow = strokesRef.current;

      type Item = {
        key: string;
        tag: "rect" | "polyline" | "circle" | "line";
        attrs: Record<string, string>;
      };
      const wanted: Array<Item> = [];
      const pageRectCache = new Map<number, ReturnType<typeof pageRect>>();
      const rectOf = (page: number) => {
        if (!pageRectCache.has(page)) {
          pageRectCache.set(page, pageRect(page, stageRect));
        }
        return pageRectCache.get(page);
      };

      for (const stroke of strokesNow) {
        const pr = rectOf(stroke.page);
        if (!pr) {
          continue;
        }
        if (stroke.rects) {
          for (let i = 0; i < stroke.rects.length; i++) {
            const r = stroke.rects[i];
            const rPr = r.page === stroke.page ? pr : rectOf(r.page);
            if (!rPr) {
              continue;
            }
            wanted.push({
              key: `${stroke.id}:${i}`,
              tag: "rect",
              attrs: {
                x: String(rPr.left + r.xr * rPr.width),
                y: String(rPr.top + r.yr * rPr.height),
                width: String(r.wr * rPr.width),
                height: String(r.hr * rPr.height),
                rx: "4",
                fill: stroke.color,
                "fill-opacity": "0.35",
              },
            });
          }
        } else if (stroke.points && stroke.points.length > 0) {
          const pts = stroke.points
            .map((p) => {
              const r = rectOf(stroke.page);
              return r
                ? `${(r.left + p.xr * r.width).toFixed(1)},${(r.top + p.yr * r.height).toFixed(1)}`
                : "";
            })
            .filter(Boolean)
            .join(" ");
          wanted.push({
            key: stroke.id,
            tag: "polyline",
            attrs: {
              points: pts,
              fill: "none",
              stroke: stroke.color,
              "stroke-width": String(stroke.width),
              "stroke-opacity": "1",
              "stroke-linecap": "round",
              "stroke-linejoin": "round",
            },
          });
        }
      }

      const draft = draftRef.current;
      if (draft && (draft.points?.length ?? 0) > 0) {
        const pr = rectOf(draft.page);
        if (pr) {
          const pts = (draft.points ?? [])
            .map(
              (p) =>
                `${(pr.left + p.xr * pr.width).toFixed(1)},${(pr.top + p.yr * pr.height).toFixed(1)}`,
            )
            .join(" ");
          wanted.push({
            key: "__draft",
            tag: "polyline",
            attrs: {
              points: pts,
              fill: "none",
              stroke: draft.color,
              "stroke-width": String(draft.width),
              "stroke-opacity": "1",
              "stroke-linecap": "round",
              "stroke-linejoin": "round",
            },
          });
        }
      }

      for (const r of draftRectsRef.current) {
        const pr = rectOf(r.page);
        if (!pr) {
          continue;
        }
        wanted.push({
          key: `__hldraft-${r.xr.toFixed(3)}-${r.yr.toFixed(3)}`,
          tag: "rect",
          attrs: {
            x: String(pr.left + r.xr * pr.width),
            y: String(pr.top + r.yr * pr.height),
            width: String(r.wr * pr.width),
            height: String(r.hr * pr.height),
            rx: "4",
            fill: color,
            "fill-opacity": "0.25",
          },
        });
      }

      const ruler = rulerRef.current;
      if (ruler) {
        const srPr = rectOf(ruler.start.page);
        if (srPr) {
          wanted.push({
            key: "__ruler-dot",
            tag: "circle",
            attrs: {
              cx: String(srPr.left + ruler.start.xr * srPr.width),
              cy: String(srPr.top + ruler.start.yr * srPr.height),
              r: "4",
              fill: "#1d1d1f",
            },
          });
          const pv = ruler.preview;
          if (pv) {
            const pvPr = rectOf(pv.page);
            if (pvPr) {
              wanted.push({
                key: "__ruler-line",
                tag: "line",
                attrs: {
                  x1: String(srPr.left + ruler.start.xr * srPr.width),
                  y1: String(srPr.top + ruler.start.yr * srPr.height),
                  x2: String(pvPr.left + pv.xr * pvPr.width),
                  y2: String(pvPr.top + pv.yr * pvPr.height),
                  stroke: "#1d1d1f",
                  "stroke-width": "2.5",
                  "stroke-dasharray": "8 6",
                },
              });
            }
          }
        }
      }

      // diff against existing children
      const existing = new Map<string, Element>();
      for (const child of Array.from(g.children)) {
        const k = child.getAttribute("data-key");
        if (k) {
          existing.set(k, child);
        }
      }
      for (const item of wanted) {
        let el = existing.get(item.key);
        if (!el || el.tagName.toLowerCase() !== item.tag) {
          el?.remove();
          el = document.createElementNS("http://www.w3.org/2000/svg", item.tag);
          el.setAttribute("data-key", item.key);
          g.appendChild(el);
        }
        for (const [name, value] of Object.entries(item.attrs)) {
          el.setAttribute(name, value);
        }
        existing.delete(item.key);
      }
      for (const el of existing.values()) {
        el.remove();
      }

      raf = requestAnimationFrame(frame);
    };
    raf = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(raf);
  });

  const svgStyle: CSSProperties | undefined = tool
    ? cursorFor(tool, color, thickness)
    : grabbing
      ? { cursor: "grabbing" }
      : undefined;

  return (
    <svg
      ref={svgRef}
      aria-hidden="true"
      className={`drawing-layer${tool ? " active" : ""}`}
      style={svgStyle}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerLeave={onPointerUp}
    >
      <g ref={strokesGRef} />
    </svg>
  );
}
