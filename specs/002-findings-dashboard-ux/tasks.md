# Tasks: Findings Dashboard — Presentation & UX v2

**Input**: Design documents from `/specs/002-findings-dashboard-ux/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/ (ui.md, interaction.md, charts.md, cli.md), quickstart.md

**Tests**: MANDATORY — constitution VII (test-first) requires every story phase to start with failing tests. All test tasks below are written FIRST and must FAIL before their implementation task runs.

**Organization**: Tasks are grouped by user story (US1–US5 from spec.md) so each story is independently implementable and testable.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (US1–US5)
- Exact file paths in every description

## Path Conventions

Single Python project at repository root: `dashboard/` (package), `tests/` (unittest), `findings/`, `results/`, `docs/`, `site/` (generated). Run tests from the repo root: `python3 -m unittest discover -s tests -v`.

**Module map** (plan.md): `dashboard/findings.py`, `dashboard/sources.py`, `dashboard/generate.py` are **UNCHANGED**. `dashboard/charts.py` and `dashboard/assets.py` are NEW. `dashboard/render.py` is REWRITTEN (page assembly). `assets.py` owns `COLORS` (palette dict), `STYLE` (CSS string), `SCRIPTS` (JS string) as static module-level constants.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Establish the green v1 baseline and the importable module stubs.

- [X] T001 Run the v1 test suite from the repo root (`python3 -m unittest discover -s tests -v`) and record the passing count — every v1 test must pass before any v2 change; this is the non-regression baseline (FR-001)
- [X] T002 [P] Create `dashboard/assets.py` with a module docstring and stub constants `COLORS: dict[str, str] = {}`, `STYLE: str = ""`, `SCRIPTS: str = ""` (v2 tests import these; behaviour lands in T006/T007/T010/T022/T026)
- [X] T003 [P] Create `dashboard/charts.py` with a module docstring and stub `bar_chart_svg(name: str, unit: str, direction: str, entries: list[tuple[str, float, str]]) -> str` that raises `NotImplementedError` — `entries` is a list of `(model, value, tooltip)` where `tooltip` is the full `data-tooltip` attribute value of the corresponding table row (contracts/charts.md)

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The v2 page frame every story hangs on — design system (assets.py) and the rewritten page skeleton (render.py) with v1 content intact.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

### Tests for the foundation (write FIRST, must FAIL)

- [X] T004 [P] Write `tests/test_assets.py`: (a) `COLORS` has at least keys `bg`, `text`, `secondary`, `link` plus one text/bg pair per v1 status badge; (b) compute WCAG relative-luminance contrast in the test and assert `text`/`bg` ≥ 4.5, `secondary`/`bg` ≥ 4.5, `link`/`bg` ≥ 4.5, and each badge text/bg pair ≥ 4.5 (contracts/ui.md palette; v1's `#777` secondary must be gone); (c) `STYLE` and `SCRIPTS` are non-empty; (d) no external references in either string — no `http://`, `https://`, `src="`, `@import`, `url(` (FR-011); (e) `STYLE` contains `:focus-visible` and `@media (prefers-reduced-motion: reduce)` (FR-014); (f) `SCRIPTS` contains none of `fetch(`, `XMLHttpRequest`, `WebSocket`, `import(`, `localStorage`, `sessionStorage`, `indexedDB`, `document.cookie`, `history.pushState`, `history.replaceState`, `new Date(` (contracts/interaction.md "must NOT do")
- [X] T005 [P] Write `tests/test_render_a11y.py` (structure part; use the `FixturesMixin` fixture pattern from `tests/test_generate.py`, with one shipped finding carrying a metric, one finding with no metrics, one on-trial finding): (a) `<html lang="en">`; (b) exactly one `<h1>`; (c) skip link `<a class="skip" href="#main">`; (d) `<main id="main">`; (e) each of `<section id="lineup">`, `<section id="board">`, `<section id="dead-ends">` has `aria-labelledby` equal to the `id` of its `<h2>`, and each section `<h2>` carries `tabindex="-1"` (FR-014); (f) heading levels in document order never skip (h1→h2→h3→h4); (g) every `.badge` span contains its status word as visible text; (h) the no-source marker contains the text "no measurement" (FR-004 — never colour alone)

### Implementation for the foundation

- [X] T006 Implement `COLORS` and `STYLE` in `dashboard/assets.py` per contracts/ui.md: palette `bg #ffffff`, `text #1a1a1a`, `secondary #595959`, `link #0b5ed7`, v1 badge pairs (all ≥ 4.5:1); max-width page layout; sticky bar `.bar { position: sticky; top: 0 }`; global `:focus-visible { outline: 2px solid #0b5ed7; outline-offset: 2px }`; `@media (prefers-reduced-motion: reduce)` setting `scroll-behavior: auto` and disabling transitions/animations; badge/marker styles with a 1 px border in the badge's own text colour; table typography; **no truncation anywhere** — no `text-overflow: ellipsis` and no `overflow: hidden` on title/model cells, long text wraps (FR-004)
- [X] T007 Rewrite page assembly in `dashboard/render.py`: `<html lang="en">`; `<head>` with `<style>` from `assets.STYLE`; skip link; `<header>` with `<h1>` carrying the generated-at stamp in the v1 format (determinism test in `tests/test_generate.py` must keep passing); sticky `<div class="bar">` present but empty (US1 adds the nav, US4 adds the controls); `<main id="main">` with the three `<section id="lineup|board|dead-ends" aria-labelledby="…-h">` each headed by `<h2 id="…-h" tabindex="-1">`; keep **all** v1 content rendering exactly (lineup tier groups with h3, board table, dead-ends list, empty states, `data-tooltip` triggers, "no measurement" markers, FINDINGS.md + raw-results links); move the v1 tooltip JavaScript from `render.py` into `assets.SCRIPTS` with unchanged behaviour (`tests/test_render_lineup.py::test_tooltip_triggers_are_focusable_and_dismissible` must keep passing — it asserts `Escape`, `focusin`, `mouseover` in the HTML); page ends with `<script>` from `assets.SCRIPTS`; keep the `render_page(findings, sections, resolved, stale_count, stamp)` signature; after this task the FULL v1 suite plus T004/T005 must be green

**Checkpoint**: Foundation ready — the page is a designed, accessible skeleton with all v1 content; every v1 test and both new test files pass. User stories can now begin.

---

## Phase 3: User Story 1 - One-action navigation between views (Priority: P1) 🎯 MVP

**Goal**: From anywhere on the page, jump to any of the three views in one action — pointer, keyboard, or with JavaScript disabled.

**Independent Test**: `python3 dashboard/generate.py`, open `site/index.html`: the sticky bar shows three links; each resolves to its view; with JS on, activation moves focus to the view's heading; with JS off, they are plain in-page anchors. `tests/test_render_nav.py` green; quickstart.md scenario 3.

### Tests for User Story 1 (write FIRST, must FAIL)

- [X] T008 [P] [US1] Write `tests/test_render_nav.py` (fixture pattern from `tests/test_generate.py`): (a) `<nav aria-label="Views">` exists inside the sticky `.bar`; (b) exactly three links — `href="#lineup"` labelled "Live lineup", `href="#board"` labelled "Candidate status board", `href="#dead-ends"` labelled "Settled dead ends" (data-model.md §Navigation target); (c) `STYLE` gives `.bar` `position: sticky` and `top: 0`; (d) each target section's `<h2>` carries `tabindex="-1"` (the focus target); (e) the nav renders for an **empty** findings layer too (edge case, US1 scenario 4); (f) `SCRIPTS` contains a nav click handler that scrolls the target into view and calls `focus()` on its heading

### Implementation for User Story 1

- [X] T009 [US1] Implement the nav in `dashboard/render.py`: `<nav aria-label="Views">` with the three plain anchors from T008(b) inside the sticky bar, always rendered including the empty state
- [X] T010 [US1] Implement the nav-focus enhancement in `dashboard/assets.py` `SCRIPTS` (append to the existing tooltip JS): on nav-link activation, scroll the target section into view (smooth; instant under `prefers-reduced-motion` via the CSS from T006) and move focus to its `tabindex="-1"` heading (contracts/interaction.md §Navigation enhancement)

**Checkpoint**: US1 fully functional and independently testable — navigation works by pointer, keyboard, and with JS disabled.

---

## Phase 4: User Story 2 - A designed, readable page (Priority: P1)

**Goal**: The page reads as designed: status-grouped board, badges that convey meaning by text and shape, AA contrast, no truncation, and 100% of the content readable with JavaScript disabled.

**Independent Test**: quickstart.md scenarios 2, 6, 7, 10 — generate and open; board grouped by status; badges carry text + border; long names wrap intact; strip the `<script>` blocks and every value, tooltip text, link, and rationale is still present; two runs differ only in the stamp.

### Tests for User Story 2 (write FIRST, must FAIL)

- [X] T011 [P] [US2] Extend `tests/test_render_a11y.py`: (a) board rows render in status-grouped order `shipped` → `on-trial` → `retired` → `dead-end` → `open`, then title case-insensitive (fixture with one finding per status; data-model.md §Default table order, FR-003); (b) no truncation — `STYLE` contains neither `text-overflow: ellipsis` nor `overflow: hidden`, and a 60-character model name renders verbatim in the HTML; (c) every `.badge` and marker style in `STYLE` carries a `border` (shape boundary, FR-004); (d) tooltip text visible without JS — for a metric carrying `tooltip_what`/`tooltip_how`/`tooltip_evidence`, all three strings appear in the page as visible text, not only inside the `data-tooltip` attribute (FR-010)
- [X] T012 [P] [US2] Extend `tests/test_generate.py` with a JS-disabled completeness test (CLI level, FR-010): generate the site, strip every `<script>…</script>` block from `site/index.html`, and assert that every resolved metric value, every tooltip what/how/evidence string, every `docs/FINDINGS.md#…` and `../results/…` link, and every rationale is still present in the remaining static HTML

### Implementation for User Story 2

- [X] T013 [US2] Implement the always-visible tooltip "details" line in `dashboard/render.py`: directly under each resolved value in lineup tables and in dead-end entries, render the tooltip content as visible text — what / how / evidence plus the same links, in the v1 tooltip order (contracts/ui.md §Progressive-enhancement contract, research.md R7)
- [X] T014 [US2] Implement the board status grouping in `dashboard/render.py`: order board rows by status `shipped` → `on-trial` → `retired` → `dead-end` → `open`, then title case-insensitive (data-model.md §Default table order, FR-003)

**Checkpoint**: US1 + US2 both work independently — a navigable, designed page whose full content survives with JavaScript disabled.

---

## Phase 5: User Story 3 - Comparison charts that are correct and comparable (Priority: P1)

**Goal**: Replace the v1 clipped vertical bars with horizontal content-measured charts: a labelled value axis, gridlines, zero label clipping, multi-metric capability, and tooltip parity with the tables.

**Independent Test**: quickstart.md scenarios 2 and 9 — the generated page shows one chart per headline metric with ≥ 2 models, axis ticks at 0/¼/½/¾/nice_max, no clipped or overlapping labels; a scratch layer with two headline metrics yields two charts; a single-model headline metric yields no chart. `tests/test_charts.py` and the rewritten `tests/test_render_charts.py` green.

### Tests for User Story 3 (write FIRST, must FAIL)

- [X] T015 [P] [US3] Write `tests/test_charts.py` (pure function tests of `dashboard/charts.py`, no page rendering) per contracts/charts.md: (a) output is `<svg role="img" aria-label="…">` naming the metric; (b) bottom axis line with exactly five tick labels 0, ¼, ½, ¾, `nice_max` and a vertical gridline at each tick; (c) `nice_max` is the smallest of {1, 2, 2.5, 5} × 10^k ≥ max value (assert for 34.0 → 50, 99.9 → 100, 120 → 200); (d) **no clipping** — every `<text>` bounding box estimated with `char_w = 6.6` lies inside the viewport and no two text boxes overlap; (e) value labels sit at bar end + 4 px inside the reserved value column (x ≥ pad + label_w + plot_w); (f) bar width = `plot_w · v / nice_max` with `plot_w = 360`, so the longest bar never reaches the plot edge; (g) every bar `<rect>` has `class="bar"`, `tabindex="0"`, `data-tooltip`, and `aria-label="<model>: <value> <unit>"`; (h) a 40-character model name grows `label_w` and still passes (d); (i) two calls with identical inputs return byte-identical strings (FR-013)
- [X] T016 [P] [US3] Rewrite `tests/test_render_charts.py` for the v2 chart contract (replacing the v1 vertical-bar geometry assertions, e.g. the `y < 150` value-label heuristic): (a) a lineup with ≥ 2 models on a headline metric renders an inline `<svg>` chart under an `<h4>` heading naming the metric and unit; (b) a headline metric with exactly one comparable model renders **no** chart (US3 scenario 4); (c) keep the v1 invariant — every charted value label is present in a table cell (charted values ⊆ table values); (d) every bar's `data-tooltip` is byte-identical to the corresponding table row's `data-tooltip`

### Implementation for User Story 3

- [X] T017 [US3] Implement `bar_chart_svg` in `dashboard/charts.py` per contracts/charts.md §Geometry: `row_h = 30`, `pad = 12`, `plot_w = 360`, `char_w = 6.6`, `axis_h = 26`, `label_w = ceil(max(len(model)) * char_w) + 12`, `value_w = ceil(max(len(fmt(v))) * char_w) + 12`, `width = pad + label_w + plot_w + value_w + pad`, `height = pad + n * row_h + pad + axis_h`; horizontal bars (height `row_h − 10`, vertically centred), bottom axis + five ticks + gridlines, value labels at bar end + 4 px, right-aligned one-line model labels; `fmt` matches the table's value formatting
- [X] T018 [US3] Integrate charts in `dashboard/render.py`: for each headline metric with ≥ 2 lineup models whose outcome is `value`, emit `charts.bar_chart_svg(…)` under an `<h4>` naming the metric and unit, passing each bar the byte-identical tooltip of its table row; delete v1's `_bar_chart_svg` from `render.py` (FR-006, SC-003)

**Checkpoint**: All P1 stories (US1–US3) independently functional — navigation, designed page, and correct charts.

---

## Phase 6: User Story 4 - Filter and search to the finding you want (Priority: P2)

**Goal**: Filter by model / tier / status and search by text with a live match count, per-view no-match states, and one-action clear — zero network, nothing persisted.

**Independent Test**: quickstart.md scenario 4 — generate and open; each select's options match the findings layer; combining filters narrows all three views at once; the count reads "N of M findings"; a zero-match query shows a per-view no-match paragraph with a working clear button; reload restores the unfiltered page.

### Tests for User Story 4 (write FIRST, must FAIL)

- [X] T019 [P] [US4] Write `tests/test_render_filter.py` (fixture pattern from `tests/test_generate.py`, multiple findings across statuses/tiers/models): (a) **DOM data contract** (data-model.md §DOM data contract) — every finding entry in all three views carries `data-model` (space-joined ids, empty string when none), `data-tier` (lineup entries only), `data-status`, `data-title`, `data-rationale`, with values equal to the fixture TOML; lineup entry is `<div class="finding">`, board entry `<tr class="finding">`, dead-end entry `<p class="finding">`; (b) controls exist, native and labelled — `<select id="f-model">` with options `all` + the layer's model ids sorted, `<select id="f-tier">` with `all`/`quality`/`fast`/`additive`, `<select id="f-status">` with `all` + the five v1 statuses, `<input type="search" id="f-text">`, `<button id="f-clear" type="button">`, `<p id="f-count" role="status">` (FR-009); (c) initial count text is "N of M findings" with M = total entry count; (d) each view contains a no-match paragraph with its own clear button, hidden by default (US4 scenario 3); (e) `SCRIPTS` re-evaluates on `input`/`change`, toggles the `hidden` attribute (assert it uses `hidden`, never `.remove(`), and updates `#f-count` (contracts/interaction.md)

### Implementation for User Story 4

- [X] T020 [US4] Implement the data attributes in `dashboard/render.py`: wrap each lineup finding (heading + markers + table) in `<div class="finding" data-model="…" data-tier="…" data-status="shipped" data-title="…" data-rationale="…">`; give each board row `<tr class="finding" …>` and each dead-end entry `<p class="finding" …>` the same five attributes per data-model.md §DOM data contract
- [X] T021 [US4] Update the v1 render tests whose element regexes assumed attribute-free tags to match the T020 markup, keeping every assertion identical in strength: `tests/test_render_lineup.py` (row regex `<tr>.*?</tr>` at line 114 → `<tr[^>]*>.*?</tr>`), `tests/test_render_board.py` (`<tr><td>…` at lines 125, 139, 151 → `<tr[^>]*><td>…`), `tests/test_render_deadends.py` (`<p><b>…` at lines 73, 84 → `<p[^>]*><b>…`)
- [X] T022 [US4] Implement the control bar in `dashboard/render.py`: `<form class="controls" role="search">` inside the sticky bar with the six controls from T019(b); options generated from the findings layer at generation time (model ids sorted; tier/status fixed sets — never invented, never scraped at view time); initial "N of M findings" count; one hidden no-match paragraph per view with a clear button (contracts/ui.md §Controls)
- [X] T023 [US4] Implement the filter/search JavaScript in `dashboard/assets.py` `SCRIPTS` per contracts/interaction.md §Filter / search: match predicate — model ∈ `data-model` ids; `data-tier` equals selection (entries without a tier match only when the tier filter is `all`); `data-status` equals selection; lower-cased query is a substring of `data-title + " " + data-rationale`; on every `input`/`change` re-evaluate all entries in all three views, toggle `hidden`, update `#f-count` ("N of M findings"), show/hide the per-view no-match paragraphs; the clear handler (bar button **and** no-match buttons) resets model/tier/status to `all` and empties the search box; no debounce, no reload, no network, no persistence (FR-007, FR-012, SC-004)

**Checkpoint**: US4 independently functional — filter/search narrows all views live with correct count and no-match states; v1 suite still green after T021.

---

## Phase 7: User Story 5 - Sort tables on any column (Priority: P2)

**Goal**: Every column of every multi-row table sorts on click: best-first initial direction for value columns, numeric sorting by value not display string, stale/no-measurement rows always last, keyboard operable, per-table state.

**Independent Test**: quickstart.md scenario 5 — generate and open; clicking a value column header sorts best-first (honouring the metric's direction); clicking again toggles; a column with stale rows keeps them last in original order; single-row tables have no sort controls; reload restores the documented default order.

### Tests for User Story 5 (write FIRST, must FAIL)

- [X] T024 [P] [US5] Write `tests/test_render_sort.py` (fixture pattern from `tests/test_generate.py`, board with ≥ 2 rows and a lineup finding with ≥ 2 metrics): (a) every column of a multi-row table renders `<th scope="col" aria-sort="none"><button>Label</button></th>` — board columns title/model(s)/status/rationale, lineup finding table columns metric/value/unit (contracts/interaction.md §Sort); (b) a table with ≤ 1 row has **no** sort buttons (edge case); (c) value-column `<th>` carries `data-sort="higher"` or `data-sort="lower"` equal to the metric's `direction`; (d) every numeric cell carries `data-value="<number>"` equal to the resolved value; stale / "no measurement" cells carry **no** `data-value`; (e) `SCRIPTS` sorts numeric columns by `data-value`, sets `aria-sort` to `ascending`/`descending` on the active `th` and renders a visible arrow (↑/↓) in the active button's text

### Implementation for User Story 5

- [X] T025 [US5] Implement the sortable headers in `dashboard/render.py`: `<th scope="col" aria-sort="none"><button>…</button></th>` for every column of the board table and of every lineup finding table with ≥ 2 rows; `data-sort` on value columns; `data-value` on numeric cells (stale/no-measurement cells get none); single-row tables keep plain `<th>` (data-model.md §DOM data contract)
- [X] T026 [US5] Implement the sort JavaScript in `dashboard/assets.py` `SCRIPTS` per contracts/interaction.md §Sort: per-table state (never shared across tables); first activation of a value column is best-first per `data-sort` (`higher` ⇒ descending, `lower` ⇒ ascending), first activation of a text column is ascending; repeat activation of the active column toggles; activating another column switches with its initial direction; numeric columns compare `data-value` as numbers, never the display string (SC-005); rows without `data-value` (stale / "no measurement") always sort **last in stable original order** regardless of column and direction (FR-008); update `aria-sort` and the visible arrow; buttons are real `<button>`s — Tab + Enter/Space with the global focus indicator (FR-009)

**Checkpoint**: All five user stories independently functional.

---

## Phase 8: Polish & Cross-Cutting Concerns

**Purpose**: Prove the whole feature end-to-end against the real findings layer and the v1 guarantees.

- [X] T027 Regenerate the real site from the repo root: `python3 dashboard/generate.py` — exit code 0, stdout summary line, `site/index.html` now reflects v2 (the committed site is the generated artifact, v1 FR-015)
- [X] T028 Run the full suite from the repo root: `python3 -m unittest discover -s tests -v` — every v1 test (unchanged in strength) and every v2 test passes
- [X] T029 Run the v2 quickstart end-to-end: all 10 scenarios in `specs/002-findings-dashboard-ux/quickstart.md` (test suite, generate + open, navigation, filter/search, sort, JS-disabled completeness, determinism, v1 non-regression, multi-metric chart proof, accessibility spot-check)
- [X] T030 [P] Verify the v1 contract kept verbatim against the generated `site/index.html` (walk the v1 spec's FR list): tooltip content order what → how → evidence → links, triggered by hover and keyboard focus and dismissible; stale marker names the file/path and shows no value; no-source marker says "no measurement" with the documentation link; `docs/FINDINGS.md#<github-slug>` and `../results/<file>.json` links are relative and resolve from `file://`; charted values link exactly like their table rows
- [X] T031 [US-all][SC-008] Add a generation-time performance assertion in `tests/test_generate.py` (write FIRST, must FAIL if generation regresses): time a full `render_page` + write over the real (or a representative fixture) findings layer and assert wall-time < 5 s (spec SC-008 non-regression); keep the assertion deterministic (single timed run, generous margin — it guards against gross regression, not micro-benchmarks)

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — start immediately
- **Foundational (Phase 2)**: Depends on Setup — **BLOCKS all user stories**
- **User Stories (Phases 3–7)**: All depend on Foundational. US1/US2/US3 are independent of each other in *scope*, but their implementation tasks share `dashboard/render.py` and `dashboard/assets.py`, so they run **sequentially** in priority order (US1 → US2 → US3 → US4 → US5), not in parallel
- **Polish (Phase 8)**: Depends on all user stories being complete; T031 (SC-008 generation-time assertion) is written test-first like any other test but exercises the finished generator, so it lands in Phase 8 alongside the end-to-end verification

### User Story Dependencies

- **US1 (P1)**: After Phase 2 only
- **US2 (P1)**: After Phase 2 only (shares render.py with US1 — sequence after it)
- **US3 (P1)**: After Phase 2 only (new `charts.py` is independent; the render.py integration sequences after US2's render.py changes)
- **US4 (P2)**: After US1–US3 (its data attributes and control bar build on the current render.py; T021 must follow T020 so the v1 suite stays green)
- **US5 (P2)**: After US4 (sortable `<th>` markup builds on the rows US4 tagged)

### Within Each User Story

- Tests are written FIRST and must FAIL (constitution VII) — run them and confirm red before the implementation task
- Data contract / markup before JavaScript (the JS only consumes what the HTML emits)
- Story complete (its tests + v1 suite green) before starting the next story

### Parallel Opportunities

- T002 / T003 (independent stub files)
- T004 / T005 (independent new test files)
- T011 / T012 (independent test files)
- T015 / T016 (independent test files)
- T030 with T028/T029 (read-only verification of the generated page)
- No implementation tasks are parallel-safe: `render.py` and `assets.py` are shared across stories

---

## Parallel Example: User Story 3

```bash
# Launch both chart test files together (both must FAIL before T017/T018):
Task: "Write tests/test_charts.py (T015)"
Task: "Rewrite tests/test_render_charts.py (T016)"

# Then sequentially (shared render.py):
Task: "Implement bar_chart_svg in dashboard/charts.py (T017)"
Task: "Integrate charts in dashboard/render.py (T018)"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup
2. Complete Phase 2: Foundational (CRITICAL — blocks all stories)
3. Complete Phase 3: User Story 1
4. **STOP and VALIDATE**: quickstart scenario 3 — nav by pointer, keyboard, and JS-disabled
5. Demo-ready: a navigable, designed skeleton with all v1 content

### Incremental Delivery

1. Setup + Foundational → foundation ready (v1 suite + T004/T005 green)
2. Add US1 → validate independently → demo (MVP)
3. Add US2 → validate independently (quickstart 2/6/7/10)
4. Add US3 → validate independently (quickstart 2/9)
5. Add US4 → validate independently (quickstart 4)
6. Add US5 → validate independently (quickstart 5)
7. Phase 8 polish → full quickstart + real site regeneration
8. Each story adds value without breaking any previous story or v1 guarantee

### Parallel Team Strategy

Not applicable — a single self-contained page means `render.py`/`assets.py` are shared across all stories. Parallelism exists only at the test-writing level (marked [P] above) and in Phase 8 verification.

---

## Notes

- [P] tasks = different files, no dependencies on incomplete tasks
- [Story] label maps each task to its user story for traceability
- Constitution VII: confirm each test file FAILS before its implementation task; run `python3 -m unittest discover -s tests -v` after every implementation task — the v1 suite must never go red
- `dashboard/findings.py`, `dashboard/sources.py`, `dashboard/generate.py` must remain byte-identical (verify with `git diff` at the end of each story)
- Determinism (FR-013): the only variable byte in `site/index.html` is the generated-at stamp — `tests/test_generate.py::DeterminismTests` guards this
- Commit after each completed story (only when the user asks)
