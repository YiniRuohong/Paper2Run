import { type CSSProperties, useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { PaperInfo, PapersApi, ProjectInfo } from "../../api-client/papers";
import { draftHref } from "../../shell/routes";
import { CommentDialog } from "./CommentDialog";
import { CommentMargin } from "./CommentMargin";
import { DrawingLayer, type Stroke, type ToolKind } from "./DrawingLayer";
import { SearchDialog, type SearchableHighlight } from "./SearchDialog";
import { COLOR_PRESETS, type ColorKey, SettingsDialog } from "./SettingsDialog";
import { Shelf } from "./Shelf";
import { Spinner } from "./Spinner";
import { Toolbar } from "./Toolbar";
import { UploadPdfDialog } from "./UploadPdfDialog";
import {
  AreaHighlight,
  Highlight,
  PdfHighlighter,
  PdfLoader,
} from "./pdf-highlighter";
import type {
  Content,
  IHighlight,
  NewHighlight,
  ScaledPosition,
} from "./pdf-highlighter";
import {
  type ShelfDoc,
  type ShelfPrefs,
  buildShelf,
  loadAnnotations,
  loadColorKey,
  loadPosition,
  loadShelfPrefs,
  saveAnnotations,
  saveColorKey,
  savePosition,
  saveShelfPrefs,
} from "./storage";

import "./reader.css";

// 论文阅读器（#/reader、#/reader/<paperId>）：PTR 批注器嵌入 Paper2Run。
// 书架 = 所有项目里上传的 PDF 论文；PDF 字节从 /papers/{id}/content 读取。
// 批注、笔画、阅读位置与书架调整（别名 / 置顶 / 移出）只存本机 localStorage。

type AppHighlight = IHighlight & { createdAt?: number };

interface Notes {
  id: string;
  highlights: Array<AppHighlight>;
  strokes: Array<Stroke>;
  /** 用户改过，需要写回本机存储（刚读出来的不重复写）。 */
  dirty: boolean;
}

const HISTORY_CAP = 5;
const COLOR_KEYS = Object.keys(COLOR_PRESETS) as Array<ColorKey>;

const getNextId = () => crypto.randomUUID();
const readerHref = (paperId: string) =>
  `#/reader/${encodeURIComponent(paperId)}`;

const notesFor = (id: string): Notes => {
  const stored = id
    ? loadAnnotations<AppHighlight, Stroke>(id)
    : { highlights: [], strokes: [] };
  return { id, ...stored, dirty: false };
};

const errorText = (e: unknown) => (e instanceof Error ? e.message : String(e));

function LoadError({ error }: { error?: Error }) {
  return (
    <div className="ptr-load-error" role="alert">
      <b>PDF 加载失败</b>
      <span>{error?.message || "论文内容暂时无法读取，请刷新后重试。"}</span>
    </div>
  );
}

export default function ReaderPage({
  papers,
  paperId,
}: {
  papers: PapersApi;
  paperId: string;
}) {
  /* ---------- 书架：后端论文列表 + 本地偏好 ---------- */
  const [projects, setProjects] = useState<Array<ProjectInfo>>([]);
  const [paperRows, setPaperRows] = useState<Array<PaperInfo>>([]);
  const [shelfLoading, setShelfLoading] = useState(true);
  const [shelfError, setShelfError] = useState("");
  const [prefs, setPrefs] = useState<ShelfPrefs>(() => loadShelfPrefs());
  const [shelfTick, setShelfTick] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setShelfLoading(true);
    setShelfError("");
    (async () => {
      const list = await papers.listProjects(controller.signal);
      const rows = await Promise.all(
        list.map((p) =>
          papers.listPapers(p.id, controller.signal).catch(() => []),
        ),
      );
      if (controller.signal.aborted) return;
      setProjects(list);
      setPaperRows(rows.flat());
    })()
      .catch((e) => {
        if (!controller.signal.aborted)
          setShelfError(`读取论文失败：${errorText(e)}`);
      })
      .finally(() => {
        if (!controller.signal.aborted) setShelfLoading(false);
      });
    return () => controller.abort();
  }, [papers, shelfTick]);

  const shelf = useMemo(
    () => buildShelf(paperRows, projects, prefs),
    [paperRows, projects, prefs],
  );

  const updatePrefs = (change: (old: ShelfPrefs) => ShelfPrefs) => {
    setPrefs((old) => {
      const next = change(old);
      if (!saveShelfPrefs(next)) showToast("书架设置未能保存到本机");
      return next;
    });
  };

  // 没有指定论文时打开书架上的第一篇（置顶优先，其次最新）。
  useEffect(() => {
    if (paperId || shelfLoading || shelf.length === 0) return;
    const first =
      shelf.find((d) => d.pinned) ??
      [...shelf].sort((a, b) => b.createdAt - a.createdAt)[0];
    window.location.replace(readerHref(first.id));
  }, [paperId, shelf, shelfLoading]);

  const currentDoc = shelf.find((d) => d.id === paperId);
  const currentRow = paperRows.find((p) => p.paper_id === paperId);
  const currentDocName =
    currentDoc?.name ??
    prefs.alias[paperId] ??
    currentRow?.title ??
    (paperId ? paperId : "");

  /* ---------- 批注与笔画（随论文切换整体换入） ---------- */
  const [notes, setNotes] = useState<Notes>(() => notesFor(paperId));
  const [drawingPast, setDrawingPast] = useState<Array<Array<Stroke>>>([]);
  const [drawingFuture, setDrawingFuture] = useState<Array<Array<Stroke>>>([]);
  if (notes.id !== paperId) {
    // 渲染期按 prop 重置状态：旧论文的批注不会写到新论文名下。
    setNotes(notesFor(paperId));
    setDrawingPast([]);
    setDrawingFuture([]);
  }
  const { highlights, strokes } = notes;

  useEffect(() => {
    if (!notes.dirty || !notes.id) return;
    if (
      !saveAnnotations(notes.id, {
        highlights: notes.highlights,
        strokes: notes.strokes,
      })
    ) {
      showToast("批注未能保存到本机（存储空间可能已满）");
    }
  }, [notes]);

  const setHighlights = (
    change: (old: Array<AppHighlight>) => Array<AppHighlight>,
  ) =>
    setNotes((n) => ({ ...n, highlights: change(n.highlights), dirty: true }));

  const commitStrokes = (change: (old: Array<Stroke>) => Array<Stroke>) => {
    setDrawingPast((prev) => [...prev, strokes].slice(-HISTORY_CAP));
    setDrawingFuture([]);
    setNotes((n) => ({ ...n, strokes: change(n.strokes), dirty: true }));
  };

  const undoDrawing = () => {
    const previous = drawingPast[drawingPast.length - 1];
    if (!previous) return;
    setDrawingPast((prev) => prev.slice(0, -1));
    setDrawingFuture((fut) => [...fut, strokes].slice(-HISTORY_CAP));
    setNotes((n) => ({ ...n, strokes: previous, dirty: true }));
  };

  const redoDrawing = () => {
    const next = drawingFuture[drawingFuture.length - 1];
    if (!next) return;
    setDrawingFuture((fut) => fut.slice(0, -1));
    setDrawingPast((prev) => [...prev, strokes].slice(-HISTORY_CAP));
    setNotes((n) => ({ ...n, strokes: next, dirty: true }));
  };

  const addStroke = (stroke: Stroke) => commitStrokes((s) => [...s, stroke]);
  const eraseStroke = (id: string) =>
    commitStrokes((s) => s.filter((x) => x.id !== id));
  const eraseStrokeRect = (id: string, rectIndex: number) =>
    commitStrokes((list) => {
      const next: Array<Stroke> = [];
      for (const s of list) {
        if (s.id === id && s.rects) {
          const rects = s.rects.filter((_, i) => i !== rectIndex);
          if (rects.length > 0) next.push({ ...s, rects });
        } else {
          next.push(s);
        }
      }
      return next;
    });

  const addHighlight = (highlight: NewHighlight) =>
    setHighlights((prev) => [
      { ...highlight, id: getNextId(), createdAt: Date.now() },
      ...prev,
    ]);

  const updateHighlight = (
    highlightId: string,
    position: Partial<ScaledPosition>,
    content: Partial<Content>,
  ) =>
    setHighlights((prev) =>
      prev.map((h) =>
        h.id === highlightId
          ? {
              ...h,
              position: { ...h.position, ...position },
              content: { ...h.content, ...content },
            }
          : h,
      ),
    );

  /* ---------- 界面状态 ---------- */
  const [searchOpen, setSearchOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const toastTimer = useRef(0);
  const showToast = useCallback((text: string) => {
    setToast(text);
    window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(null), 2600);
  }, []);
  useEffect(() => () => window.clearTimeout(toastTimer.current), []);

  const [colorKey, setColorKey] = useState<ColorKey>(() =>
    loadColorKey(COLOR_KEYS, "indigo"),
  );
  useEffect(() => {
    saveColorKey(colorKey);
  }, [colorKey]);

  const stageRef = useRef<HTMLDivElement | null>(null);
  const scrollContainer = () =>
    stageRef.current?.querySelector<HTMLElement>('[class*="_container"]') ??
    null;

  /* ---------- 缩放（仅 PDF 展示区）----------
     笔画/评论均为页面归一化坐标，由渲染循环每帧贴位，缩放无需手动同步 */
  const [zoom, setZoom] = useState(1);
  const zoomRef = useRef(1);
  const zoomHideTimer = useRef(0);
  const applyZoom = useCallback((next: number) => {
    const clamped = Math.min(3, Math.max(0.6, +next.toFixed(3)));
    // 缩放进行中隐藏库高亮层，避免 pdf.js 重排期间高亮位置滞后产生的抽搐；
    // 停止缩放 400ms 后恢复。
    const stage = stageRef.current;
    stage?.classList.add("zooming");
    window.clearTimeout(zoomHideTimer.current);
    zoomHideTimer.current = window.setTimeout(() => {
      stage?.classList.remove("zooming");
    }, 400);
    zoomRef.current = clamped;
    setZoom(clamped);
  }, []);
  useEffect(() => () => window.clearTimeout(zoomHideTimer.current), []);

  /* ---------- 绘图工具 ---------- */
  const [tool, setTool] = useState<ToolKind | null>(null);
  const [toolColor, setToolColor] = useState("#FFE066");
  const [toolThickness, setToolThickness] = useState(4);

  /* ---------- 每篇论文的阅读位置 ---------- */
  useEffect(() => {
    const stage = stageRef.current;
    if (!stage || !paperId) return;
    let timer = 0;
    // scroll 不冒泡，用捕获阶段在 stage 上统一接管滚动容器的位置
    const onScroll = (event: Event) => {
      const target = event.target as HTMLElement | null;
      if (!target || target === stage) return;
      window.clearTimeout(timer);
      timer = window.setTimeout(
        () => savePosition(paperId, target.scrollTop),
        250,
      );
    };
    stage.addEventListener("scroll", onScroll, { capture: true, passive: true });
    return () => {
      window.clearTimeout(timer);
      stage.removeEventListener("scroll", onScroll, { capture: true });
    };
  }, [paperId]);

  /* ---------- 触控板（仅 PDF 展示区域生效） ----------
     捏合（Ctrl/Meta+wheel）= 缩放；普通双指 = 平移；
     工具激活时绘图层覆盖页面，改为手动接管滚动，避免"锁死"。 */
  useEffect(() => {
    const stage = stageRef.current;
    if (!stage) return;
    let scheduled = 0;
    const onWheel = (event: WheelEvent) => {
      if (event.ctrlKey || event.metaKey) {
        event.preventDefault();
        if (scheduled) return;
        scheduled = window.requestAnimationFrame(() => {
          scheduled = 0;
          const factor = Math.exp(-(event.deltaY || 0) * 0.0016);
          applyZoom(zoomRef.current * factor);
        });
        return;
      }
      if (tool) {
        const scrollEl = scrollContainer();
        if (scrollEl) {
          event.preventDefault();
          scrollEl.scrollTop += event.deltaY;
          scrollEl.scrollLeft += event.deltaX;
        }
      }
    };
    stage.addEventListener("wheel", onWheel, { passive: false });
    return () => {
      if (scheduled) window.cancelAnimationFrame(scheduled);
      stage.removeEventListener("wheel", onWheel);
    };
  }, [applyZoom, tool]);

  /* ---------- 选区评论 ---------- */
  const [commentDraft, setCommentDraft] = useState<{
    position: ScaledPosition;
    content: Content;
  } | null>(null);
  const [editing, setEditing] = useState<{ id: string; text: string } | null>(
    null,
  );
  const [activeCommentId, setActiveCommentId] = useState<string | null>(null);

  /* ---------- 定位到批注（不改 URL hash：hash 是应用路由） ---------- */
  const scrollViewerTo = useRef<(highlight: AppHighlight) => void>(() => {});
  const jumpToHighlight = (highlight: SearchableHighlight) => {
    const target = highlights.find((h) => h.id === highlight.id);
    if (target) scrollViewerTo.current(target);
  };

  /* ---------- 书架操作 ---------- */
  const selectDoc = (id: string) => {
    if (id !== paperId) window.location.hash = readerHref(id);
  };
  const renameDoc = (id: string, name: string) =>
    updatePrefs((p) => ({ ...p, alias: { ...p.alias, [id]: name } }));
  const togglePin = (id: string) =>
    updatePrefs((p) => ({
      ...p,
      pinned: p.pinned.includes(id)
        ? p.pinned.filter((x) => x !== id)
        : [...p.pinned, id],
    }));
  const hideDocs = (ids: Array<string>) => {
    updatePrefs((p) => ({
      ...p,
      hidden: [...new Set([...p.hidden, ...ids])],
      pinned: p.pinned.filter((x) => !ids.includes(x)),
    }));
    if (ids.includes(paperId)) {
      const next = shelf.find((d) => !ids.includes(d.id));
      window.location.replace(next ? readerHref(next.id) : "#/reader");
    }
  };
  // 复现：打开这篇论文所在项目的草稿工作台（草稿以项目为单位，论文已挂在项目上）。
  const reproduceDoc = (doc: ShelfDoc) => {
    window.location.hash = draftHref(doc.projectId);
  };

  const content = paperId ? papers.contentRequest(paperId) : null;
  const hlVars = COLOR_PRESETS[colorKey].vars as CSSProperties;

  return (
    <div className={`ptr${collapsed ? " collapsed" : ""}`} style={hlVars}>
      {!collapsed ? (
        <Shelf
          docs={shelf}
          currentId={paperId}
          loading={shelfLoading}
          error={shelfError}
          onSelectDoc={selectDoc}
          onNewPdf={() => setUploadOpen(true)}
          onOpenSearch={() => setSearchOpen(true)}
          onOpenSettings={() => setSettingsOpen(true)}
          onCollapse={() => setCollapsed(true)}
          onRenameDoc={renameDoc}
          onTogglePin={togglePin}
          onDeleteDocs={hideDocs}
          onReproduce={reproduceDoc}
        />
      ) : null}
      <section className="ptr-main" aria-label="PDF 阅读区">
        <header className={`ptr-head${collapsed ? " collapsed-head" : ""}`}>
          {collapsed ? (
            <span className="head-frame">
              <button
                type="button"
                className="head-icon-btn"
                onClick={() => setCollapsed(false)}
                aria-label="打开书架"
                title="打开书架"
              >
                <svg
                  width="15"
                  height="15"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.8"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  aria-hidden="true"
                >
                  <rect x="3" y="4" width="18" height="16" rx="3" />
                  <path d="M9 4v16" />
                </svg>
              </button>
              <button
                type="button"
                className="head-icon-btn"
                onClick={() => setSearchOpen(true)}
                aria-label="搜索批注"
                title="搜索批注"
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
                  <circle cx="11" cy="11" r="7" />
                  <path d="m20 20-3.5-3.5" />
                </svg>
              </button>
              <button
                type="button"
                className="head-icon-btn"
                onClick={() => setUploadOpen(true)}
                aria-label="上传 PDF"
                title="上传 PDF"
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
            </span>
          ) : null}
          <b className="head-title" title={currentDocName}>
            {currentDocName || "论文阅读"}
          </b>
          {paperId ? (
            <Toolbar
              tool={tool}
              color={toolColor}
              thickness={toolThickness}
              zoom={zoom}
              canUndo={drawingPast.length > 0}
              canRedo={drawingFuture.length > 0}
              onToolChange={setTool}
              onColorChange={setToolColor}
              onThicknessChange={setToolThickness}
              onZoomIn={() => applyZoom(zoom + 0.1)}
              onZoomOut={() => applyZoom(zoom - 0.1)}
              onZoomReset={() => applyZoom(1)}
              onUndo={undoDrawing}
              onRedo={redoDrawing}
            />
          ) : null}
        </header>
        <div className="pdf-stage" ref={stageRef}>
          {content ? (
            <>
              <PdfLoader
                key={paperId}
                url={content.url}
                httpHeaders={content.headers}
                beforeLoad={<Spinner />}
                errorMessage={<LoadError />}
              >
                {(pdfDocument) => (
                  <PdfHighlighter
                    pdfDocument={pdfDocument}
                    pdfScaleValue={String(zoom)}
                    enableAreaSelection={(event) => event.altKey}
                    onScrollChange={() => {}}
                    scrollRef={(scrollTo) => {
                      scrollViewerTo.current = scrollTo;
                      // 恢复这篇论文上次的阅读位置
                      const saved = loadPosition(paperId);
                      if (saved > 0) {
                        window.setTimeout(() => {
                          const scrollEl = scrollContainer();
                          if (scrollEl) scrollEl.scrollTop = saved;
                        }, 250);
                      }
                    }}
                    onSelectionFinished={(
                      position,
                      selected,
                      hideTipAndSelection,
                    ) => (
                      <button
                        type="button"
                        className="sel-comment-btn"
                        title="添加评论"
                        aria-label="添加评论"
                        onClick={() => {
                          setCommentDraft({ position, content: selected });
                          hideTipAndSelection();
                        }}
                      >
                        <svg
                          width="14"
                          height="14"
                          viewBox="0 0 24 24"
                          fill="none"
                          stroke="currentColor"
                          strokeWidth="1.8"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                          aria-hidden="true"
                        >
                          <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
                        </svg>
                      </button>
                    )}
                    highlightTransform={(
                      highlight,
                      index,
                      _setTip,
                      _hideTip,
                      viewportToScaled,
                      screenshot,
                      isScrolledTo,
                    ) =>
                      !highlight.content?.image ? (
                        <Highlight
                          key={index}
                          isScrolledTo={isScrolledTo}
                          position={highlight.position}
                          comment={highlight.comment}
                        />
                      ) : (
                        <AreaHighlight
                          key={index}
                          isScrolledTo={isScrolledTo}
                          highlight={highlight}
                          onChange={(boundingRect) => {
                            updateHighlight(
                              highlight.id,
                              { boundingRect: viewportToScaled(boundingRect) },
                              { image: screenshot(boundingRect) },
                            );
                          }}
                        />
                      )
                    }
                    highlights={highlights}
                  />
                )}
              </PdfLoader>
              <CommentMargin
                highlights={highlights}
                activeId={activeCommentId}
                onHover={setActiveCommentId}
                onEdit={(highlight) =>
                  setEditing({ id: highlight.id, text: highlight.comment.text })
                }
              />
              <DrawingLayer
                tool={tool}
                color={toolColor}
                thickness={toolThickness}
                strokes={strokes}
                onAdd={addStroke}
                onErase={eraseStroke}
                onEraseRect={eraseStrokeRect}
              />
            </>
          ) : (
            <div className="ptr-empty">
              {shelfLoading ? (
                <Spinner />
              ) : (
                <>
                  <b>还没有可阅读的 PDF</b>
                  <span>上传一篇论文，或在新建实验里上传后回到这里阅读。</span>
                  <button
                    type="button"
                    className="btn btn-primary"
                    onClick={() => setUploadOpen(true)}
                  >
                    上传 PDF
                  </button>
                </>
              )}
            </div>
          )}
        </div>
      </section>

      {searchOpen ? (
        <SearchDialog
          docName={currentDocName}
          highlights={highlights}
          onClose={() => setSearchOpen(false)}
          onJump={jumpToHighlight}
        />
      ) : null}

      {settingsOpen ? (
        <SettingsDialog
          colorKey={colorKey}
          highlightCount={highlights.length}
          strokeCount={strokes.length}
          hiddenCount={prefs.hidden.length}
          docName={paperId ? currentDocName : ""}
          onClose={() => setSettingsOpen(false)}
          onColorChange={setColorKey}
          onClearAnnotations={() => {
            setNotes((n) => ({ ...n, highlights: [], strokes: [], dirty: true }));
            setDrawingPast([]);
            setDrawingFuture([]);
          }}
          onRestoreHidden={() => updatePrefs((p) => ({ ...p, hidden: [] }))}
        />
      ) : null}

      {uploadOpen ? (
        <UploadPdfDialog
          papers={papers}
          projects={projects}
          onClose={() => setUploadOpen(false)}
          onUploaded={(id) => {
            setShelfTick((t) => t + 1);
            window.location.hash = readerHref(id);
          }}
        />
      ) : null}

      {commentDraft ? (
        <CommentDialog
          onCancel={() => setCommentDraft(null)}
          onSubmit={(text) => {
            addHighlight({
              content: commentDraft.content,
              position: commentDraft.position,
              comment: { text, emoji: "" },
            });
            setCommentDraft(null);
          }}
        />
      ) : null}

      {editing ? (
        <CommentDialog
          mode="edit"
          initialText={editing.text}
          onCancel={() => setEditing(null)}
          onSubmit={(text) => {
            const targetId = editing.id;
            setHighlights((prev) =>
              prev.map((h) =>
                h.id === targetId ? { ...h, comment: { ...h.comment, text } } : h,
              ),
            );
            setEditing(null);
          }}
        />
      ) : null}

      {toast ? (
        <div className="ptr-toast" role="status">
          {toast}
        </div>
      ) : null}
    </div>
  );
}
