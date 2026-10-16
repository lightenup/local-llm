# Contract: Dashboard UI v2 (site/index.html)

**Feature**: 002-findings-dashboard-ux
Supersedes the v1 UI contract for layout and accessibility; the v1 tooltip,
marker, and link contracts are **kept verbatim** (FR-001) and restated at
the bottom.

Single self-contained HTML file, no network, no server, opens from
`file://` (FR-011). All CSS and JS inline; no external `src`/`href`, no
`<link>`, no fonts.

## Page structure

```text
<a class="skip" href="#main">Skip to content</a>          (visually hidden until focused)
<header>
  <h1>Findings — local-LLM stack [generated at: <stamp>]</h1>
  <div class="bar">                                        (position: sticky; top: 0)
    <nav aria-label="Views">
      <a href="#lineup">Live lineup</a>
      <a href="#board">Candidate status board</a>
      <a href="#dead-ends">Settled dead ends</a>
    </nav>
    <form class="controls" role="search">                  (no action; JS-only)
      <label>Model <select id="f-model">…</select></label>
      <label>Tier    <select id="f-tier">…</select></label>
      <label>Status  <select id="f-status">…</select></label>
      <label>Search  <input type="search" id="f-text"></label>
      <button id="f-clear" type="button">Clear filters</button>
      <p id="f-count" role="status">N of M findings</p>
    </form>
  </div>
</header>
<main id="main">
  <section id="lineup" aria-labelledby="lineup-h"> <h2 id="lineup-h" tabindex="-1">…</h2> … </section>
  <section id="board"  aria-labelledby="board-h">  <h2 id="board-h"  tabindex="-1">…</h2> … </section>
  <section id="dead-ends" aria-labelledby="deadends-h"> <h2 id="deadends-h" tabindex="-1">…</h2> … </section>
</main>
<script>…tooltip…</script>
<script>…nav focus + filter/search/sort…</script>
```

- **Landmarks** (FR-014): `header`, `nav`, `main`, three `section`s with
  `aria-labelledby`; heading order strictly h1 → h2 (views) → h3 (tiers) →
  h4 (findings), no skipped levels.
- **Sticky bar** (FR-002, SC-001): nav + controls stay visible at any scroll
  position; each nav link reaches its view in one action from anywhere.
- **Nav links are plain anchors** — with JavaScript disabled they resolve as
  in-page anchors (FR-010).
- **View regions** (FR-003): visually separated (spacing + rules), each
  headed; lineup grouped and labelled by tier (h3 per tier); board rows
  grouped/ordered by status (data-model.md §Default table order).
- **Empty state** (v1, kept): an empty findings layer renders the three
  headed regions with "no findings yet" messages, and the nav still renders
  (US1 scenario 4).

## Controls

| Control | Type | Options / behaviour |
|---|---|---|
| Model | `<select>` | `all` + every model id in the findings layer, sorted |
| Tier | `<select>` | `all`, `quality`, `fast`, `additive` |
| Status | `<select>` | `all` + the five v1 statuses |
| Search | `<input type="search">` | free text; case-insensitive substring over finding titles and rationales |
| Clear filters | `<button>` | resets all controls to default in one action (FR-007) |
| Match count | `<p role="status">` | "N of M findings", updated live, announced to screen readers |

Every control is a native, labelled element (FR-009). Options are generated
from the findings layer at generation time — never invented, never scraped
at view time (constitution IV).

## Badge & marker contract (FR-004)

- Every status badge and the stale / "no measurement" markers convey their
  meaning by **text and shape, never colour alone**: the status word is
  always rendered inside the badge; markers keep their "stale" /
  "no measurement" text.
- Each badge/marker has a 1 px border in its own text colour (shape
  boundary meets the 3:1 non-text contrast bar).
- Palette (assets.py, tested — research.md R6): body `#1a1a1a`, secondary
  `#595959` (v1's `#777` fails AA and is replaced), link/focus `#0b5ed7`,
  badge pairs unchanged from v1 (all ≥ 4.5:1).
- No truncation anywhere: long model names, tooltip text, and non-ASCII
  content wrap and render intact (edge case).

## Focus & motion (FR-009, FR-014)

- `:focus-visible { outline: 2px solid #0b5ed7; outline-offset: 2px }`
  globally; every interactive element (nav links, selects, search, buttons,
  sort buttons, chart bars, tooltip triggers) is keyboard-reachable with a
  visible indicator.
- No information is pointer/hover-only: tooltips also show on keyboard
  focus (v1), and their full text is additionally visible inline (below).
- `@media (prefers-reduced-motion: reduce)`: `scroll-behavior: auto`, no
  transitions or animations (edge case).

## Progressive-enhancement contract (FR-010)

With JavaScript disabled, 100% of the following remain present and
readable in the static HTML:

- every metric value and unit;
- every tooltip's **text** — rendered as an always-visible "details" line
  (what / how / evidence + links) directly under its value in lineup tables
  and dead-end entries (research.md R7);
- every link (FINDINGS.md anchors, raw results);
- every badge/marker; all three view regions;
- navigation as plain anchors; tables in their documented default order
  (data-model.md); filter/sort controls absent or inert, never broken
  affordances (the controls render but do nothing without JS — they are
  native form elements, so they are not "broken").

## Kept verbatim from the v1 contract

- **Tooltip** (FR-004/FR-009): content order what → how → evidence → links;
  triggered by hover **and** keyboard focus; dismissible (blur/click/Esc);
  chart elements carry the same tooltip as their table row.
- **Markers** (FR-008 v1): stale ⇒ visible marker, no value, message names
  the file/path; no source ⇒ "no measurement" marker + documentation link;
  stale findings participate in filter/sort like any other (edge case).
- **Links**: FINDINGS.md `docs/FINDINGS.md#<github-slug>` computed from the
  section heading; raw results `../results/<file>.json`; both relative so
  they resolve from `file://`; a charted value links exactly like its table
  row.
