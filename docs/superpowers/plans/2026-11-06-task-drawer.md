# Task page drawer Implementation Plan

> **For agentic workers:** executed natively in one session (user asked for fastest completion).

**Goal:** centre column shows only live/outcome, metrics, figures and actionable cards; clicking a rail sub-step, step 08/09, or 任务详情 opens a right-side drawer with that part's process.

**Architecture:** `Workbench` gains an optional drawer slot and rail callbacks; the task page maps a rail item id (`t<seq>` / `e<seq>` / `n<seq>`, already produced by `stages.ts`) to a detail view-model built from events, and renders it in the drawer.

**Tech Stack:** React + TypeScript, node:test.

**Spec:** `docs/superpowers/specs/2026-11-06-task-drawer-design.md`

## Global Constraints

- Rail look and colours unchanged; setup pages (no `onOpenItem`) behave exactly as before.
- Model narrative stays labelled 「模型叙述（不是执行证据）」; outcome and conclusion stay in the centre column.

## Review Focus

- A command still running (no `tool_completed`) — drawer shows the command and 「运行中，输出完成后显示」.
- A failed tool (`is_error`) — one-sentence error, not an empty output block.
- Very long stdout — only the tail is shown, with a truncation note.
- Drawer open while polling adds events — content refreshes for the same item.
- Item id not backed by an event (setup items) — no drawer, nothing thrown.

### Task 1: Detail view-model (`features/autonomous/stepDetail.ts`)

- `itemSequence(id: string): number | null` — `/^[ten](\d+)$/`.
- `itemDetail(events, itemId, task): ItemDetail | null` with kinds `command | tool | note | delegation | artifact | event`; `OUTPUT_LIMIT = 64_000` chars kept from the tail.
- Tests in `tests/autonomous-drawer.test.mjs`: command with stdout/stderr/exit; running command; failed tool; note from tool and from `progress_noted`; artifact path; long output truncated; unknown id → null.

### Task 2: Workbench drawer slot

- Props: `drawer?: { label: string; title: string; body: ReactNode; onClose(): void; openPath?: string } | null`, `onOpenItem?(stageId, item)`, `onOpenStage?(stageId)`, `selectedItemId?: string | null`.
- New `ui/workbench/Drawer.tsx`: scrim + aside, Esc closes, focus in/out; CSS in `workbench.css`.
- Test (source-level, `tests/workbench.test.mjs`): sub-step rows call `onOpenItem` when given; stage head calls `onOpenStage` and keeps toggling.

### Task 3: Task page

- Centre: actionable cards, dashed live/outcome card, `ReportSummary` (outcome, conclusion, metrics), figures, 追加指令.
- Drawer: item detail; step `review` → evidence + `CandidateCard`; step `report` → `ReportDetails`; 任务详情 → summary, usage, specialists, skills, config, raw events.
- Update `autonomous-isolation` and `workbench` tests; tsc + full web tests; browser check.
