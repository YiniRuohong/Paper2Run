# Task 5 report

Implemented: `railHead` prop and `.wb.is-app` layout (grid areas "rail head head" / "rail viewer files", wb-grid uses display: contents; darwin 82px traffic-light padding + drag region; responsive 1440/1240/860 variants, rail first on narrow), `wb-group` separators (stage.group change), expandable items with children (default collapsed, `aria-expanded={childOpen}`, children rendered via same `wb-sub` renderer incl. openPath). Without `railHead` layout/DOM is unchanged apart from Fragment wrapper.
errorDetail moved to `src/features/autonomous/errors.ts`; TaskPage imports and re-exports it; NewExperimentPage imports from './errors'; helper re-exports directly.

TDD: RED - 2 new tests failed (no railHead, errors.ts ENOENT). GREEN - `pnpm --dir apps/web test` 165 pass / 0 fail; typecheck and build pass.
Note: tests/autonomous-isolation.test.mjs VM harness needed a `./errors` module stub (added) since TaskPage now requires it.

Files: Workbench.tsx, workbench.css, errors.ts (new), TaskPage.tsx, NewExperimentPage.tsx, tests/helpers/error-detail.mjs, tests/workbench.test.mjs, tests/autonomous-isolation.test.mjs.
Concerns: app-layout CSS is untested visually (no caller passes railHead yet).

## Fix round 1

Changes (`apps/web/src/ui/workbench/workbench.css`, `apps/web/tests/workbench.test.mjs`; no markup change):
- Removed `grid-row: auto` from the `.wb.is-app .wb-rail` rule in the `max-width: 1240px` block, so the rail keeps spanning all three rows through `grid-area: rail`.
- Added `.wb.is-app .wb-rail-head { position: sticky; top: 0; z-index: 2; background: <rail background> }` so the brand and back link stay pinned while the stage list scrolls. Scoped to `.wb.is-app`; the plain layout is unchanged.
- New test: `app layout keeps the rail spanning every row and its head pinned` (1240 app-rail block has no `grid-row`; app rail head is sticky, top 0, with a background; plain `.wb-rail-head` not sticky).

Commands and results:
- RED: `cd apps/web && node --test tests/workbench.test.mjs` -> new test failed: "the rail must keep spanning rows through grid-area" (actual `.wb.is-app .wb-rail { grid-row: auto; ... }`).
- GREEN: same command -> tests 11, pass 11, fail 0.
- `pnpm --dir apps/web typecheck` -> clean; `pnpm --dir apps/web build` -> built in 413ms.
