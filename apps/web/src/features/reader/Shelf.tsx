import { useEffect, useMemo, useRef, useState } from "react";
import { Modal } from "./Modal";
import type { ShelfDoc } from "./storage";

interface Props {
  docs: Array<ShelfDoc>;
  currentId: string;
  onSelectDoc: (id: string) => void;
  onNewPdf: () => void;
  onOpenSearch: () => void;
  onOpenSettings: () => void;
  onCollapse: () => void;
  /** 收起时仍保持挂载以便播放收起动画；inert 让它退出 Tab 顺序与无障碍树。 */
  open?: boolean;
  loading?: boolean;
  error?: string;
  onRenameDoc: (id: string, name: string) => void;
  onTogglePin: (id: string) => void;
  onDeleteDocs: (ids: Array<string>) => void;
  onReproduce: (doc: ShelfDoc) => void;
}

/* ---------- tiny icons ---------- */

const IconSearch = () => (
  <svg
    width="16"
    height="16"
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
);

const IconSettings = () => (
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
    <circle cx="12" cy="12" r="3" />
    <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
  </svg>
);

const IconPin = () => (
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
    <path d="M12 17v5" />
    <path d="M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V7a1 1 0 0 1 1-1 2 2 0 0 0 0-4H8a2 2 0 0 0 0 4 1 1 0 0 1 1 1z" />
  </svg>
);

const IconPencil = () => (
  <svg
    width="13"
    height="13"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.8"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    <path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z" />
  </svg>
);

const IconMulti = () => (
  <svg
    width="13"
    height="13"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.8"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    <path d="m3 17 2 2 4-4" />
    <path d="m3 7 2 2 4-4" />
    <path d="M13 6h8" />
    <path d="M13 12h8" />
    <path d="M13 18h8" />
  </svg>
);

const IconTrash = () => (
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
    <path d="M3 6h18" />
    <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6" />
    <path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
  </svg>
);

/* ---------- time grouping ---------- */

const DAY = 86_400_000;

const bucketOf = (createdAt: number): string => {
  const now = new Date();
  const startOfToday = new Date(
    now.getFullYear(),
    now.getMonth(),
    now.getDate(),
  ).getTime();
  if (createdAt >= startOfToday) return "today";
  if (createdAt >= startOfToday - DAY) return "yesterday";
  if (createdAt >= startOfToday - 7 * DAY) return "week";
  if (createdAt >= startOfToday - 30 * DAY) return "month";
  return "older";
};

const GROUP_LABELS: Record<string, string> = {
  pin: "置顶",
  today: "今天",
  yesterday: "昨天",
  week: "七天内",
  month: "三十天内",
  older: "更早",
};
const GROUP_ORDER = ["pin", "today", "yesterday", "week", "month", "older"];

export function Shelf({
  docs,
  currentId,
  onSelectDoc,
  onNewPdf,
  onOpenSearch,
  onOpenSettings,
  onCollapse,
  open = true,
  onRenameDoc,
  onTogglePin,
  onDeleteDocs,
  onReproduce,
  loading = false,
  error = "",
}: Props) {
  /* 置顶独立成组，其余按时间倒序后落入时间段 */
  const groups = useMemo(() => {
    const byTime = [...docs].sort((a, b) => b.createdAt - a.createdAt);
    const buckets: Record<string, Array<ShelfDoc>> = {};
    for (const doc of byTime) {
      const key = doc.pinned ? "pin" : bucketOf(doc.createdAt);
      if (!buckets[key]) {
        buckets[key] = [];
      }
      buckets[key].push(doc);
    }
    return GROUP_ORDER.filter((key) => buckets[key]?.length).map((key) => ({
      key,
      label: GROUP_LABELS[key],
      items: buckets[key],
    }));
  }, [docs]);

  const [menuId, setMenuId] = useState<string | null>(null);
  const [multi, setMulti] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [confirmIds, setConfirmIds] = useState<Array<string> | null>(null);
  const [renameId, setRenameId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");

  const menuRootRef = useRef<HTMLSpanElement | null>(null);

  /* 收起时清掉临时状态（菜单、多选、弹窗），与以前收起即卸载的行为一致 */
  useEffect(() => {
    if (open) {
      return;
    }
    setMenuId(null);
    setMulti(false);
    setSelected(new Set());
    setConfirmIds(null);
    setRenameId(null);
  }, [open]);

  /* 点击菜单外部 / Esc 时关闭 ⋯ 菜单 */
  useEffect(() => {
    if (!menuId) {
      return;
    }
    const onPointerDown = (event: PointerEvent) => {
      if (!menuRootRef.current?.contains(event.target as Node)) {
        setMenuId(null);
      }
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setMenuId(null);
      }
    };
    document.addEventListener("pointerdown", onPointerDown, false);
    window.addEventListener("keydown", onKeyDown, false);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown, false);
      window.removeEventListener("keydown", onKeyDown, false);
    };
  }, [menuId]);

  const exitMulti = () => {
    setMulti(false);
    setSelected(new Set());
  };

  const toggleSelected = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  };

  const openRename = (id: string, name: string) => {
    setRenameId(id);
    setRenameValue(name);
    setMenuId(null);
  };

  const confirmDoc = confirmIds
    ? docs.find((d) => d.id === confirmIds[0])
    : null;
  const isMultiDelete = Boolean(confirmIds && confirmIds.length > 1);
  const deleteTitle = isMultiDelete
    ? `将 ${confirmIds?.length ?? 0} 篇论文移出书架`
    : "将该论文移出书架";
  const deleteBody = isMultiDelete
    ? "论文仍保留在所属项目中，批注留在本机；可在阅读器设置里恢复。"
    : `「${confirmDoc?.name ?? ""}」仍保留在所属项目中，批注留在本机；可在阅读器设置里恢复。`;

  const hasSelection = selected.size > 0;

  return (
    <aside
      className="ptr-side"
      aria-label="论文书架"
      {...(open ? {} : { inert: true, "aria-hidden": true })}
    >
      <div className="ptr-side-top">
        <div className="brand-row">
          <b className="brand-name">论文书架</b>
          <span className="brand-actions">
            <button
              type="button"
              className="head-icon-btn"
              onClick={onOpenSearch}
              aria-label="搜索"
              title="搜索"
            >
              <IconSearch />
            </button>
            <button
              type="button"
              className="head-icon-btn"
              onClick={onOpenSettings}
              aria-label="设置"
              title="设置"
            >
              <IconSettings />
            </button>
            <button
              type="button"
              className="head-icon-btn"
              onClick={onCollapse}
              aria-label="收起侧边栏"
              title="收起侧边栏"
            >
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
                <rect x="3" y="4" width="18" height="16" rx="3" />
                <path d="M15 4v16" />
              </svg>
            </button>
          </span>
        </div>

        {multi ? (
          <div className="multi-head">
            <span className="multi-title">选择文件</span>
            <button
              type="button"
              className="multi-exit"
              onClick={exitMulti}
              aria-label="退出多选"
              title="退出多选"
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
        ) : (
          <button type="button" className="start-pill" onClick={onNewPdf}>
            <span className="start-pill-plus" aria-hidden="true">
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
                <path d="M6 2v8M2 6h8" />
              </svg>
            </span>
            上传 PDF
          </button>
        )}
      </div>

      <nav className="ptr-side-nav">
        {error ? (
          <p className="shelf-empty" role="alert">{error}</p>
        ) : loading && docs.length === 0 ? (
          <p className="shelf-empty" role="status">读取论文…</p>
        ) : docs.length === 0 ? (
          <p className="shelf-empty">书架还是空的，上传一篇 PDF 开始阅读</p>
        ) : (
          groups.map((group) => (
            <section key={group.key} className="shelf-group">
              <div className="group-label">{group.label}</div>
              <ul className="shelf-list">
                {group.items.map((doc) => {
                  const isSelected = selected.has(doc.id);
                  return (
                    <li key={doc.id}>
                      <div
                        className={`shelf-item${doc.id === currentId && !multi ? " on" : ""}`}
                      >
                        <button
                          type="button"
                          className="shelf-main"
                          onClick={() => {
                            if (multi) {
                              toggleSelected(doc.id);
                            } else {
                              onSelectDoc(doc.id);
                            }
                          }}
                          title={`${doc.name} · ${doc.projectName}`}
                        >
                          <span
                            className={`checkbox${isSelected ? " on" : ""}${multi ? " show" : ""}`}
                            aria-hidden="true"
                          >
                            {isSelected ? (
                              <svg
                                width="11"
                                height="11"
                                viewBox="0 0 12 12"
                                fill="none"
                                stroke="#fff"
                                strokeWidth="2"
                                strokeLinecap="round"
                                strokeLinejoin="round"
                                aria-hidden="true"
                              >
                                <path d="m2 6 3 3 5-6" />
                              </svg>
                            ) : null}
                          </span>
                          <span className="shelf-name">{doc.name}</span>
                          {doc.pinned && !multi ? (
                            <span className="pin-mark" title="已置顶">
                              <IconPin />
                            </span>
                          ) : null}
                        </button>
                        {!multi ? (
                          <span
                            className="kebab-wrap"
                            ref={menuId === doc.id ? menuRootRef : undefined}
                          >
                            <button
                              type="button"
                              className="kebab"
                              aria-label="更多操作"
                              onClick={() => {
                                setMenuId(menuId === doc.id ? null : doc.id);
                              }}
                            >
                              <svg
                                width="15"
                                height="15"
                                viewBox="0 0 16 16"
                                fill="currentColor"
                                aria-hidden="true"
                              >
                                <circle cx="8" cy="3" r="1.4" />
                                <circle cx="8" cy="8" r="1.4" />
                                <circle cx="8" cy="13" r="1.4" />
                              </svg>
                            </button>
                            {menuId === doc.id ? (
                              <div className="row-menu">
                                <button
                                  type="button"
                                  onClick={() => openRename(doc.id, doc.name)}
                                >
                                  <IconPencil />
                                  重命名
                                </button>
                                <button
                                  type="button"
                                  onClick={() => {
                                    onTogglePin(doc.id);
                                    setMenuId(null);
                                  }}
                                >
                                  <IconPin />
                                  {doc.pinned ? "取消置顶" : "置顶"}
                                </button>
                                <button
                                  type="button"
                                  onClick={() => {
                                    setMulti(true);
                                    setSelected(new Set([doc.id]));
                                    setMenuId(null);
                                  }}
                                >
                                  <IconMulti />
                                  多选
                                </button>
                                <button
                                  type="button"
                                  onClick={() => {
                                    onReproduce(doc);
                                    setMenuId(null);
                                  }}
                                >
                                  <svg
                                    width="13"
                                    height="13"
                                    viewBox="0 0 24 24"
                                    fill="none"
                                    stroke="currentColor"
                                    strokeWidth="1.8"
                                    strokeLinecap="round"
                                    strokeLinejoin="round"
                                    aria-hidden="true"
                                  >
                                    <path d="M5 3l14 9-14 9z" />
                                  </svg>
                                  复现
                                </button>
                                <button
                                  type="button"
                                  className="danger"
                                  onClick={() => {
                                    setConfirmIds([doc.id]);
                                    setMenuId(null);
                                  }}
                                >
                                  <IconTrash />
                                  移出书架
                                </button>
                              </div>
                            ) : null}
                          </span>
                        ) : null}
                      </div>
                    </li>
                  );
                })}
              </ul>
            </section>
          ))
        )}
      </nav>

      {multi ? (
        <div className="bulk-bar">
          <button
            type="button"
            className={`bulk-btn${hasSelection ? " active" : ""}`}
            disabled={!hasSelection}
            onClick={() => {
              for (const id of selected) {
                onTogglePin(id);
              }
              exitMulti();
            }}
          >
            <IconPin />
            置顶
          </button>
          <span className="bulk-divider" aria-hidden="true" />
          <button
            type="button"
            className={`bulk-btn danger${hasSelection ? " active" : ""}`}
            disabled={!hasSelection}
            onClick={() => setConfirmIds([...selected])}
          >
            <IconTrash />
            移出书架
          </button>
        </div>
      ) : null}

      {/* 移出书架确认弹窗（按钮左对齐，无右上角关闭） */}
      {confirmIds ? (
        <Modal
          title={deleteTitle}
          onClose={() => setConfirmIds(null)}
          noHeadClose
        >
          <p className="confirm-body">{deleteBody}</p>
          <div className="confirm-btns">
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => setConfirmIds(null)}
            >
              取消
            </button>
            <button
              type="button"
              className="btn btn-danger"
              onClick={() => {
                onDeleteDocs(confirmIds);
                setConfirmIds(null);
                exitMulti();
              }}
            >
              移出书架
            </button>
          </div>
        </Modal>
      ) : null}

      {/* 重命名弹窗 */}
      {renameId ? (
        <Modal title="重命名" onClose={() => setRenameId(null)}>
          <label className="field-label" htmlFor="rename-input">
            文档名称
          </label>
          <input
            id="rename-input"
            className="field-input"
            value={renameValue}
            // biome-ignore lint/a11y/noAutofocus: This is an example app
            autoFocus
            onChange={(event) => setRenameValue(event.target.value)}
          />
          <div className="confirm-btns end">
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => setRenameId(null)}
            >
              取消
            </button>
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => {
                if (renameValue.trim()) {
                  onRenameDoc(renameId, renameValue.trim());
                }
                setRenameId(null);
              }}
            >
              保存
            </button>
          </div>
        </Modal>
      ) : null}
    </aside>
  );
}
