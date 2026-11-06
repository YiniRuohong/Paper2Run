import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { Modal } from "./Modal";
import type { ScaledPosition } from "./pdf-highlighter";
import { firstMatchIndex, fuzzyMatch, snippetAround } from "./search-utils";

export interface SearchableHighlight {
  id: string;
  position: ScaledPosition;
  content: { text?: string; image?: string };
  comment: { text: string; emoji: string };
  createdAt?: number;
}

type FilterKey = "all" | "highlight" | "comment";

const FILTERS: Array<{ key: FilterKey; label: string }> = [
  { key: "all", label: "全部" },
  { key: "highlight", label: "高亮" },
  { key: "comment", label: "评论" },
];

interface Props {
  docName: string;
  highlights: Array<SearchableHighlight>;
  onClose: () => void;
  onJump: (highlight: SearchableHighlight) => void;
}

/** Highlight the first matched keyword inside a snippet. */
function markSnippet(text: string, query: string): ReactNode {
  const normalized = text.replace(/\s+/g, " ").trim();
  const idx = firstMatchIndex(text, query);
  if (idx < 0) {
    return normalized;
  }
  const kw = query
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean)
    .find((k) => normalized.toLowerCase().includes(k));
  if (!kw) {
    return normalized;
  }
  const at = normalized.toLowerCase().indexOf(kw);
  return (
    <>
      {normalized.slice(0, at)}
      <span className="hl-mark">{normalized.slice(at, at + kw.length)}</span>
      {normalized.slice(at + kw.length)}
    </>
  );
}

export function SearchDialog({ highlights, onClose, onJump }: Props) {
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<FilterKey>("all");
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRootRef = useRef<HTMLDivElement | null>(null);

  /* 点击菜单外部 / Esc 时关闭筛选菜单 */
  useEffect(() => {
    if (!menuOpen) {
      return;
    }
    const onPointerDown = (event: PointerEvent) => {
      if (!menuRootRef.current?.contains(event.target as Node)) {
        setMenuOpen(false);
      }
    };
    document.addEventListener("pointerdown", onPointerDown, false);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown, false);
    };
  }, [menuOpen]);

  const results = useMemo(() => {
    const pool = highlights.map((h, index) => ({
      highlight: h,
      // stable fallback so imported demo highlights keep their order
      createdAt: h.createdAt ?? index,
    }));

    return pool
      .filter(({ highlight: h }) => {
        const hasComment = Boolean(h.comment.text.trim());
        const quotedText = h.content.text ?? "";
        const commentText = h.comment.text;
        if (filter === "comment") {
          return hasComment && fuzzyMatch(commentText, query) >= 0;
        }
        if (filter === "highlight") {
          return fuzzyMatch(quotedText, query) >= 0;
        }
        return (
          fuzzyMatch(quotedText, query) >= 0 ||
          fuzzyMatch(commentText, query) >= 0
        );
      })
      .sort((a, b) => b.createdAt - a.createdAt); // 时间排序（最新在前）
  }, [highlights, query, filter]);

  return (
    <Modal title="搜索批注" onClose={onClose}>
      <div className="search-row">
        <div className="search-box">
          <svg
            width="13"
            height="13"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            aria-hidden="true"
          >
            <circle cx="11" cy="11" r="7" />
            <path d="m20 20-3.5-3.5" />
          </svg>
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
          <div className="search-filter-wrap" ref={menuRootRef}>
            <button
              type="button"
              className={`search-filter-btn${filter !== "all" ? " active" : ""}`}
              onClick={() => setMenuOpen(!menuOpen)}
              aria-label="筛选"
              title="筛选"
            >
              <svg
                width="14"
                height="14"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.8"
                strokeLinecap="round"
                aria-hidden="true"
              >
                <path d="M4 6h16" />
                <path d="M7 12h10" />
                <path d="M10 18h4" />
              </svg>
            </button>
            {menuOpen ? (
              <div className="search-menu">
                {FILTERS.map((item) => (
                  <button
                    key={item.key}
                    type="button"
                    className={filter === item.key ? "on" : ""}
                    onClick={() => {
                      setFilter(item.key);
                      setMenuOpen(false);
                    }}
                  >
                    {item.label}
                    {filter === item.key ? (
                      <svg
                        width="12"
                        height="12"
                        viewBox="0 0 12 12"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth="2"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        aria-hidden="true"
                      >
                        <path d="m2 6 3 3 5-6" />
                      </svg>
                    ) : null}
                  </button>
                ))}
              </div>
            ) : null}
          </div>
        </div>
      </div>

      {results.length === 0 ? (
        <p className="search-empty">
          {query.trim() ? "没有匹配的批注" : "当前文档还没有批注"}
        </p>
      ) : (
        <ul className="search-results">
          {results.map(({ highlight: h }) => {
            const mainText =
              h.comment.text.trim() || h.content.text || "（区域批注）";
            const subText = h.content.text
              ? snippetAround(
                  h.content.text,
                  Math.max(firstMatchIndex(h.content.text, query), 10),
                )
              : "区域批注（截图）";
            return (
              <li key={h.id}>
                <button
                  type="button"
                  className="result-item"
                  onClick={() => {
                    onJump(h);
                    onClose();
                  }}
                >
                  <span className="result-main">
                    {h.comment.emoji ? `${h.comment.emoji} ` : ""}
                    {markSnippet(mainText, query)}
                  </span>
                  <span className="result-sub">{subText}</span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </Modal>
  );
}
