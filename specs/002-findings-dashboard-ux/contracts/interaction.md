# Contract: Browser-side Interaction (filter / search / sort)

**Feature**: 002-findings-dashboard-ux
The inline JavaScript's observable behaviour. All state is in browser
memory; nothing is written to the URL, storage, or disk (FR-012); no
network requests (FR-011). The generated document is the single source of
truth and the reset state.

## Shared rules

- **Finding entries**: the JS operates only on elements with
  `class="finding"` (data-model.md §DOM data contract) — one per finding in
  each of the three views. Visibility is toggled with the `hidden`
  attribute; entries are never removed from the DOM.
- **Re-evaluation**: on every `input`/`change` of any control, all entries
  in all three views are re-evaluated; the match count and per-view
  no-match states update in the same turn (no debounce, no reload, no
  network — SC-004).
- **Determinism of the document**: the JS mutates only the live DOM of the
  open page; the bytes of `site/index.html` are never affected.

## Filter / search

- **Match predicate** (all active fields must hold):
  - model: the selection is among the entry's `data-model` ids;
  - tier: `data-tier` equals the selection (entries without a tier match
    only when the tier filter is `all`);
  - status: `data-status` equals the selection;
  - text: the query (lower-cased) is a substring of
    `data-title + " " + data-rationale` (lower-cased).
- **Count**: `#f-count` shows "N of M findings" — N visible, M total —
  updated live; the element is `role="status"` (announced, focus not
  stolen).
- **No match** (US4 scenario 3, edge case): when a filter/query yields zero
  visible entries, each affected view shows its own "no findings match the
  current filters" paragraph with a "Clear filters" button wired to the
  same clear handler — never a blank page. Views whose entries are all
  hidden but that had no active filter effect keep their normal empty state
  ("no findings yet") only when the layer itself is empty.
- **Clear** (FR-007, SC-004): one action (the bar's button or a no-match
  button) resets model/tier/status to `all` and empties the search box; the
  full unfiltered page is restored.
- **Reload** (FR-012): filter/search state does not persist; a reload shows
  the generated document as-is.

## Sort

- **Scope**: per table; tables with ≤ 1 row have no sort controls (edge
  case). Sortable columns: every column of the board table (title,
  model(s), status, rationale) and every column of lineup finding tables
  (metric, value, unit).
- **Controls**: `<th scope="col" aria-sort="…"><button>Label</button></th>`;
  the button's text carries a visible arrow (↑ ascending / ↓ descending)
  when its column is active; `aria-sort` is `none` / `ascending` /
  `descending` on the `th`.
- **Direction** (FR-008, US5):
  - first activation of a value column: **best-first** per the column's
    `data-sort` (`higher` ⇒ descending, `lower` ⇒ ascending);
  - first activation of a text column: ascending;
  - repeat activation of the active column: toggle;
  - activating another column: switch to it with its initial direction.
- **Numeric columns** sort by the cell's `data-value` (a number), never by
  the display string (SC-005).
- **Stale / "no measurement" rows** (no `data-value`): always last, in
  stable original order, regardless of column and direction (FR-008;
  data-model.md §Sort state).
- **Keyboard** (FR-009, US5 scenario 3): sort buttons are real `<button>`s
  — Tab + Enter/Space activates, with the global visible focus indicator.
- **Reset**: a reload restores the documented default order
  (data-model.md §Default table order); sort state is per-table and does
  not affect other tables.

## Navigation enhancement

- On nav-link activation: scroll the target section into view, then move
  focus to its heading (`tabindex="-1"`). With `prefers-reduced-motion`,
  the jump is instant (CSS `scroll-behavior: auto`); otherwise smooth.
- With JavaScript disabled the same links behave as plain in-page anchors
  (FR-010).

## What the JS must NOT do

- no `fetch`/`XMLHttpRequest`/`WebSocket`/`import()` (no network — FR-011);
- no `localStorage`/`sessionStorage`/`indexedDB`/cookie writes (FR-012);
- no `history.pushState`/`replaceState` or hash rewrites (FR-012);
- no `Date`/randomness (FR-013 — the document's bytes are static; the JS
  only reads them).
