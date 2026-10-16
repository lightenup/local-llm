# Research: Findings Dashboard — Presentation & UX v2

**Feature**: 002-findings-dashboard-ux | **Date**: 2026-09-22
**Purpose**: Resolve the implementation-level decisions behind the v2 spec.
All user-facing unknowns were settled with the owner during
`/speckit.specify` (the spec's "Settled design decisions" block); this file
covers how the generator and the generated page implement them.

## R1. Module layout: two new modules, three v1 modules untouched

**Decision**: Add `dashboard/charts.py` (SVG chart geometry) and
`dashboard/assets.py` (inline CSS/JS as static string constants). Rewrite
`render.py`'s page assembly. Do **not** touch `findings.py`, `sources.py`,
or `generate.py`.

**Rationale**:
- The spec requires no data-model change and no CLI change (FR-001, Key
  Entities): the load/validate/resolve/exit-code pipeline is a v1 guarantee,
  so the three modules that own it stay byte-identical.
- `render.py` today is 329 lines; v2 adds nav, a control bar, data
  attributes, sortable tables, no-match states, a redesigned stylesheet, and
  ~150 lines of interaction JS. In one file that is ~800 lines where the
  chart geometry (a pure, heavily-tested function) would be buried.
- `charts.py` makes the no-clip guarantee (FR-005, SC-002) a testable pure
  function: given entries, it returns SVG whose geometry can be asserted
  without rendering a page (research.md R3).
- `assets.py` makes determinism (FR-013) trivially auditable: the CSS and JS
  are module-level constants, so "identical inputs ⇒ identical bytes" reduces
  to "constants don't change" plus the existing stamp exception.
- Constitution III/VI: this is a split of existing concerns, not new
  abstraction; every module maps to a distinct test file.

**Alternatives considered**:
- *Extend `render.py` in place* — one fewer module, but the file becomes the
  place where geometry, styling, and interaction all entangle; the no-clip
  tests would need to parse a full page instead of a chart.
- *One new `ui.py` holding CSS + JS + charts* — a grab-bag module; chart
  geometry and presentation assets have different test strategies (geometry
  assertions vs. string/contrast assertions) and different change drivers.

## R2. Navigation: plain anchors + sticky bar + JS focus hand-off

**Decision**: A `<nav aria-label="Views">` with three plain
`<a href="#lineup|#board|#dead-ends">` links, rendered inside a sticky bar
(`position: sticky; top: 0`) together with the filter controls. Each view
heading carries `tabindex="-1"`. A small inline script adds: on nav-link
click, `scrollIntoView` the target and move focus to its heading.
`html { scroll-behavior: smooth }` is overridden to `auto` under
`prefers-reduced-motion`.

**Rationale**:
- FR-010 requires navigation to keep working with JavaScript disabled —
  plain in-page anchors are the only mechanism that guarantees that; the JS
  is a pure enhancement (focus hand-off so keyboard/screen-reader users land
  *on* the heading, not just near it — US1 scenario 1).
- Sticky positioning satisfies "from anywhere on the page, in a single
  action" (FR-002, SC-001) without a second page or a server.
- `tabindex="-1"` headings are programmatically focusable but stay out of
  the tab order, so keyboard navigation is not polluted by three extra stops.

**Alternatives considered**:
- *Hash-based view switching (hide two of three views)* — would hide content
  from JS-disabled readers and from the DOM, breaking FR-010's "all three
  views render"; also loses the "one long document" reading mode v1 users
  have.
- *`scroll-behavior: smooth` only, no focus hand-off* — fails US1 scenario 1
  ("its heading receives focus") and leaves screen-reader users without a
  position announcement.

## R3. Charts: horizontal bars, content-measured geometry, nice-tick axis

**Decision**: Replace v1's vertical bars with **horizontal bar charts**
emitted by `charts.py`. Geometry is computed from the content:

```text
row_h      = 30          # one row per model
pad        = 12          # outer padding
label_w    = ceil(max(len(model)) * char_w) + 12     # left column
value_w    = ceil(max(len(fmt(v))) * char_w) + 12    # right of bar end
plot_w     = 360         # default plot width (the "generator's default width")
char_w     = 6.6         # px per char, 11px system font (conservative)
width      = pad + label_w + plot_w + value_w + pad
height     = pad + n * row_h + pad + axis_h          # axis_h = 26

scale: nice_max = smallest "nice" number (1, 2, 2.5, 5 × 10^k) ≥ max(v)
       ticks at 0, nice_max/4, nice_max/2, 3·nice_max/4, nice_max
```

- Value axis: bottom line with tick labels; vertical gridlines at each tick
  (FR-005).
- Bars: `x = pad + label_w`, `width = plot_w · v / nice_max`; the value label
  sits at the bar end + 4 px, inside the reserved `value_w` column.
- Model labels: one line each in the left column (long names like
  `Qwen3.8-Flash-Next-oQ4e-mtp` fit because the column is sized to the
  longest name — no wrapping, no truncation).
- Every bar keeps v1's contract: `tabindex="0"`, `data-tooltip` identical to
  its table row, plus an `aria-label` ("model: value unit") (FR-006).
- Charts are produced for **every** headline metric with ≥ 2 resolved
  models; a single-model metric gets no chart (FR-006, US3 scenario 4).

**Why this fixes the v1 defect**: v1 scaled bars to `max_v`, so the tallest
bar reached the plot top and its value label (drawn 6 px *above* the bar,
with only 10 px top padding) was clipped — the "58.8 is cut off" bug. Here
the scale is `nice_max ≥ max_v`, so no bar reaches the plot edge, and every
text element has a reserved column/row whose width/height was computed from
the actual string lengths. No-clip is a structural property, asserted by
parsing the SVG in tests (test_charts.py): every `<text>`'s estimated
bounding box lies inside the viewport, and no two boxes overlap.

**Rationale for horizontal over vertical**:
- Long model names are the common case in this repo (20–27 chars); a left
  label column handles them with one line and zero overlap risk. Vertical
  bars would need multi-line `<tspan>` wrapping heuristics — more geometry
  to get wrong, for a page that compares 2–5 models.
- The value axis becomes a simple bottom axis with vertical gridlines; the
  clipped-label failure mode disappears entirely.
- Still ~60 lines of emission, still inline SVG, still zero dependencies
  (v1 research R2 stands).

**Alternatives considered**:
- *Fix v1's vertical chart (bigger top pad, wrapped labels)* — keeps the
  familiar shape, but wrapping heuristics re-introduce overlap risk exactly
  where the spec demands zero (SC-002), and the top-label clip would need a
  second padding pass for tick labels.
- *Vendored charting library* — rejected in v1 (R2) and still rejected:
  FR-011 forbids adding one, and per-element tooltips would need injection
  into a foreign render stack.

## R4. Filter/search: DOM data attributes + native controls, in-memory state

**Decision**: At generation time, every finding entry carries a data
contract (data-model.md §DOM data contract):

- Lineup: each shipped finding is wrapped in
  `<div class="finding" data-model="…" data-tier="…" data-status="shipped"
  data-title="…" data-rationale="…">` (heading + markers + table).
- Board: each row is `<tr class="finding" data-…>`.
- Dead ends: each entry is `<p class="finding" data-…>`.

The control bar holds native, labelled controls: three `<select>`s (model /
tier / status — options generated from the findings layer, sorted) and one
`<input type="search">`, plus a "Clear filters" `<button>` and a
`<p role="status">` match count ("N of M findings"). Inline JS (assets.py)
re-evaluates all `.finding` entries on every `input`/`change`: an entry is
visible iff it matches **every** active filter (model: any of the finding's
models equals the selection; tier/status: equality; text: case-insensitive
substring over `data-title` + `data-rationale`). Non-matching entries get
the `hidden` attribute; the count updates; each view whose findings are all
hidden shows its own "no findings match" paragraph with a "Clear filters"
button (wired to the same handler). State lives in JS variables only —
nothing is written to the URL, storage, or disk (FR-012).

**Rationale**:
- Native controls are keyboard-operable and screen-reader-labelled for free
  (FR-009, FR-014) — no custom widget code to get wrong.
- Data attributes make the filter logic a pure function of the DOM, so tests
  can verify the *contract* (every entry has complete, correct attributes)
  without a browser — the spec's stated testing approach (Assumptions:
  "verified against the generated HTML/SVG structure … not pixel
  snapshots").
- `hidden` (not `display:none` in a stylesheet) keeps the mechanism
  declarative and keeps all content in the DOM for JS-disabled readers.
- Options generated at generation time (not scraped from the DOM at view
  time) keep the bytes deterministic and the option set exactly the findings
  layer (constitution IV — no invented values).

**Alternatives considered**:
- *URL-driven filters (query string / hash)* — would persist state across
  reloads, which FR-012 explicitly forbids ("the generated document is the
  single source of truth").
- *Client-side scraping of visible text for search* — slower, and would
  match tooltip/label text the spec does not ask for; the data contract is
  exact (titles and rationales only, per FR-007).
- *One global no-match message* — the spec asks for the message where the
  blankness would be; per-view messages keep each region self-explanatory
  when a filter empties only one view.

## R5. Sorting: `th > button`, `aria-sort`, `data-value`, stale rows last

**Decision**: Tables with > 1 row render each header as
`<th scope="col" aria-sort="none"><button>Label</button></th>`. Numeric
cells carry `data-value="<resolved number>"`. The button label gains a text
arrow (↑ / ↓) reflecting the active direction. Inline JS implements:

- First activation of a column: **best-first** — for a metric-value column
  the initial direction honours the metric's `direction` (`higher` ⇒
  descending, `lower` ⇒ ascending; the direction is recorded as
  `data-sort="higher|lower"` on the `th`); for text columns, ascending.
- Repeat activation of the same column: toggle direction.
- Activating a different column: switch to it with its initial direction.
- Stale / "no measurement" rows (no `data-value`) sort **last, in stable
  original order, regardless of column and direction** — the documented
  position (FR-008, US5 scenario 2; data-model.md §Default table order).
- Single-row tables render plain headers, no buttons (edge case).

**Rationale**:
- `th > button` is the standard accessible sort pattern: the button is the
  operable control (FR-009), `aria-sort` on the `th` announces state to
  screen readers, and the visible text arrow satisfies "never by colour
  alone" (FR-004) while mirroring the state.
- `data-value` keeps numeric sorting exact (58.8 > 9.1, not "9.1" > "58.8")
  without the JS re-parsing display formatting (FR-008).
- Best-first initial direction makes "which model decodes fastest?" one
  click (US5), and the toggle keeps power users in control.

**Alternatives considered**:
- *`<th>` itself as the button (clickable header)* — inconsistent keyboard
  semantics across browsers; a real `<button>` inside the `th` is the
  unambiguous pattern.
- *Always ascending first* — fails the spec's "best first is meaningful"
  requirement for metrics with a direction.
- *Stale rows first* — would surface broken data as the headline; last is
  the conventional "unsortable" position and keeps healthy comparisons on
  top.

## R6. Accessibility: landmark structure, computed contrast, visible focus

**Decision**:
- Landmarks: `<header>` (title + stamp), `<nav aria-label="Views">`,
  `<main>`, three `<section aria-labelledby>` regions; a skip link
  (`#main`) visually hidden until focused; tables use `scope="col"` headers.
- Heading order is strict h1 → h2 (views) → h3 (tiers) → h4 (findings) — no
  levels skipped.
- Focus: global `:focus-visible { outline: 2px solid #0b5ed7; outline-offset:
  2px }`; every interactive element is a native control or `tabindex="0"`
  (bars, tooltip triggers) and is reachable in a sensible tab order.
- Colour: keep v1's five badge pairs (all measured ≥ 4.5:1, see below);
  **replace v1's `#777` secondary text with `#595959`** (7.0:1 on white —
  v1's `#777` measures 4.48:1 and *fails* AA, in the tier labels, stamp, and
  empty states); add a 1 px border in the badge's own text colour to every
  badge and marker so the shape boundary meets the 3:1 non-text bar
  (WCAG 1.4.11) and meaning is carried by text + shape + colour (FR-004).
  The palette lives in `assets.py` as a dict; a test computes WCAG relative
  luminance and contrast ratios and asserts ≥ 4.5:1 (text) / ≥ 3:1 (UI
  boundaries).
- Motion: `@media (prefers-reduced-motion: reduce)` disables smooth
  scrolling and all transitions (edge case).
- Live region: the match count is `role="status"` so filter results are
  announced without stealing focus.

**Measured contrast (white background unless noted)**:

| Pair | Ratio | Use |
|---|---|---|
| `#1a1a1a` on `#ffffff` | 17.4:1 | body text |
| `#595959` on `#ffffff` | 7.0:1 | secondary text (stamp, tiers, empty states) |
| `#0b5ed7` on `#ffffff` | 5.8:1 | links, focus outline |
| `#155724` on `#d4edda` | 7.0:1 | shipped badge |
| `#856404` on `#fff3cd` | 5.0:1 | on-trial badge |
| `#383d41` on `#e2e3e5` | 8.5:1 | retired badge / no-measurement marker |
| `#721c24` on `#f8d7da` | 8.2:1 | dead-end badge / stale marker |
| `#0c5460` on `#d1ecf1` | 6.9:1 | open badge |
| `#ffffff` on `#222222` | 15.9:1 | tooltip body |

**Rationale**: FR-014 names WCAG 2.1 AA explicitly; making the palette a
tested dict turns "meets AA" from a review opinion into a red/green test
(constitution VII). The `#777` fix is the one place v1's render actually
misses AA, so v2 must change it even though it is cosmetic.

**Alternatives considered**:
- *Leave `#777` (it "looks fine")* — 4.48:1 is a measurable AA failure;
  SC-006 requires measured contrast ≥ 4.5:1.
- *Icon-only sort indicators / colour-only badges* — violates FR-004/FR-009
  directly.
- *Full ARIA tooltip widgets (`role="tooltip"` + `aria-describedby`)* —
  heavier; the v1 data-attribute tooltip already works on hover **and**
  focus, and v2 adds the visible fallback (R7), so the information is
  reachable without any ARIA state machine.

## R7. Progressive enhancement: visible tooltip fallback for JS-disabled readers

**Decision**: v1 keeps tooltip text only in `data-tooltip` attributes, which
a JS-disabled reader can never see — that would violate FR-010 ("tooltip
text … MUST remain present and readable"). v2 therefore renders, for every
metric, an **always-visible** "details" line directly under its value (in
lineup tables and dead-end entries): the what / how / evidence text and the
FINDINGS/raw links as plain inline content. The hover/focus tooltip remains
for JS-enabled readers (same content, same `data-tooltip` bytes).

**Rationale**:
- FR-010 is explicit that tooltip *text* must be readable with JavaScript
  disabled; an attribute is present in the DOM but not readable.
- Always-visible details also serve JS-enabled readers (no hover needed to
  audit a number — the spec's trust goal), and make the "evidence at a
  glance" value (US2) real instead of hidden behind interaction.
- Cost: the page gets longer; that is the accepted trade — v2's problem
  statement is that the v1 page costs trust, and a number whose method is
  one scroll away is more auditable than one behind a hover.
- Determinism is unaffected: the fallback is static HTML generated from the
  same resolved data (constitution IV).

**Alternatives considered**:
- *`<details>/<summary>` per metric* — progressive and keyboard-friendly,
  but collapsed by default, which is *less* readable with JS disabled than
  plain visible text, and adds a second interaction to every metric.
- *Print-only fallback (CSS `@media print`)* — does not satisfy "readable"
  on screen with JavaScript disabled.

## R8. Determinism and the JS surface

**Decision**: All CSS and JS are static string constants in `assets.py`;
the JS uses no `Date`, no randomness, no `localStorage`/`sessionStorage`,
no `fetch`/`XMLHttpRequest`, no URL writes (`history` untouched). The only
non-constant bytes in the document are the resolved values, the findings
content, and the "generated at" stamp — all as in v1.

**Rationale**: FR-013 (byte-identical output except the stamp) and FR-012
(no persistence) are both verifiable by inspection of a static string plus
the existing determinism test (two runs, diff = stamp line). FR-011's
"no network requests" is verifiable by asserting the generated HTML contains
no `src=`/`href=` pointing off-document (test_assets.py).

**Alternatives considered**: none — there is no alternative that satisfies
FR-011 + FR-012 + FR-013 simultaneously.

## R9. Testing strategy: structural tests, no browser, no pixels

**Decision**: Extend the `unittest` suite with structural assertions on the
generated HTML/SVG (the spec's stated approach):

- `test_charts.py` — parse each SVG with `xml.etree.ElementTree`: viewport
  contains every `<text>` bounding box (estimated with the same `char_w`
  metric the generator uses), no two text boxes overlap, axis + gridlines
  present, tick count = 5, bar `width` proportional to value / nice_max,
  every bar has `tabindex="0"` + `data-tooltip`, charted values ⊆ table
  values, one chart per ≥ 2-model headline metric, none for 1-model.
- `test_render_nav.py` — nav present with exactly the three anchors,
  `position: sticky` in the CSS, headings `tabindex="-1"`, skip link.
- `test_render_filter.py` — every `.finding` entry (all three views) carries
  complete `data-model/tier/status/title/rationale` attributes matching the
  findings layer; controls present and labelled; options == layer's model /
  tier / status sets (sorted); per-view no-match paragraphs present.
- `test_render_sort.py` — multi-row tables have `th > button` +
  `aria-sort="none"` default; numeric cells carry `data-value`; single-row
  tables have no buttons; board default order = status then title;
  `data-sort` direction recorded on value columns.
- `test_render_a11y.py` — landmarks present once each; heading levels never
  skip; every badge/marker carries its text label; `prefers-reduced-motion`
  block present; `:focus-visible` rule present; tooltip fallback text
  present in the static HTML for every metric (FR-010).
- `test_assets.py` — WCAG contrast ratios computed from the palette dict
  (≥ 4.5:1 text, ≥ 3:1 UI); no `http(s)://` in any `src`/`href`; no
  `<link>`/external `<script src>`; JS string contains no
  `fetch(`/`localStorage`/`history.`/`Date(`.
- `test_generate.py` (extended) — JS-disabled completeness: strip
  `<script>…</script>` from the generated HTML and assert every metric
  value, tooltip triple, and link is still present; determinism (existing)
  unchanged.

Browser-level behaviour (filter actually hides rows, sort actually reorders,
smooth scroll) is validated manually via quickstart.md — the spec's
Assumptions section scopes automated verification to structure, not pixels.

**Rationale**: Constitution VII needs a failing test per capability;
structural tests are the strongest form available without a browser
dependency (none may be added — FR-011/constitution III), and they assert
exactly the contracts the JS consumes, so a broken contract fails red
before the JS is even written.

**Alternatives considered**:
- *Headless browser (Playwright/Selenium)* — a third-party dependency plus a
  browser binary for a single-user tool; rejected on constitution III/VI.
- *Pixel snapshots* — explicitly rejected by the spec's Assumptions.

## Open items

None. All decisions above are made; the tasks phase may refine function
boundaries inside `charts.py`/`render.py` but not these decisions.
