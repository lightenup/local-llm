# Data Model: Findings Dashboard — Presentation & UX v2

**Feature**: 002-findings-dashboard-ux | **Date**: 2026-09-22

**The persistent data model is unchanged.** v2 is presentation and
view-time behaviour over the v1 layer (spec Key Entities).
`findings/*.toml`, `results/*.json`, `docs/FINDINGS.md`, and the v1
entities — Finding, Metric, Source, Status, View — are exactly as defined
in [001 data-model.md](../001-findings-dashboard/data-model.md); the
generator's load/validate/resolve pipeline (`findings.py`, `sources.py`) is
untouched. This file makes the spec's four **view-time, non-persisted**
concepts concrete, plus the DOM data contract that the browser-side
features consume.

## Navigation target

A named link to one of the three view regions.

| Attribute | Value |
|---|---|
| label | `Live lineup` / `Candidate status board` / `Settled dead ends` |
| target | `#lineup` / `#board` / `#dead-ends` (the v1 section ids, unchanged) |
| rendering | plain `<a href>` inside `<nav aria-label="Views">`; sticky bar |
| JS enhancement | on activation, scroll target into view and focus its heading (`tabindex="-1"`) |
| JS-disabled behaviour | plain in-page anchor (FR-010) |

Always rendered, including for an empty findings layer (edge case).

## Filter/query state

The reader's current selections — browser memory only, reset on reload,
never written (FR-012).

| Field | Domain | Default | Matches a finding when |
|---|---|---|---|
| model | `""` or one of the layer's model ids (options generated from the layer, sorted) | `""` (all) | any of the finding's `models` equals the selection |
| tier | `""` or `quality` / `fast` / `additive` | `""` (all) | the finding's tier equals the selection (non-lineup findings have no tier ⇒ match only when `""`) |
| status | `""` or one of the five v1 statuses | `""` (all) | the finding's status equals the selection |
| text | free string | `""` | case-insensitive substring of `title` + `rationale` |

An entry is visible iff it matches **all** active fields. A field at its
default is inactive. Derived (never stored separately): match count
"N of M findings", and per-view no-match visibility.

## Sort state

Per table, in browser memory only, reset on reload:

| Field | Domain | Default |
|---|---|---|
| column | a sortable column of that table, or none | none (documented default order) |
| direction | `ascending` / `descending` | first activation: best-first for value columns (honours the metric's `direction`: `higher` ⇒ descending, `lower` ⇒ ascending), ascending for text columns; repeat activation toggles |

Stale / "no measurement" rows sort **last, in stable original order,
regardless of column and direction** (FR-008).

## Comparison chart

An inline-SVG rendering of one headline metric across models, derived at
generation time from the *same* resolved values as the tables — no new
source of truth (FR-006).

| Attribute | Rule |
|---|---|
| one chart per | headline metric with ≥ 2 models having a resolved `value` outcome |
| omitted when | fewer than 2 comparable models (US3 scenario 4) |
| scale | `nice_max` = smallest nice number (1, 2, 2.5, 5 × 10^k) ≥ max value; ticks at 0, ¼, ½, ¾, 1 of `nice_max` |
| geometry | content-measured (research.md R3): label column sized to the longest model name, value column to the longest value label, fixed plot width 360 px (the default width), 30 px rows |
| per bar | `tabindex="0"`, `data-tooltip` byte-identical to the table row's, `aria-label` "model: value unit" |
| invariant | a charted value is never shown that is not also in a table with a tooltip (v1 FR-006, kept) |

## DOM data contract (view-time, generated)

The attributes the inline JS consumes. Generated at build time from the
findings layer; complete and correct for **every** finding entry in all
three views (testable without a browser — research.md R9).

| Entry | Element | Attributes |
|---|---|---|
| lineup finding | `<div class="finding">` wrapping heading + markers + table | `data-model` (space-joined model ids), `data-tier`, `data-status="shipped"`, `data-title`, `data-rationale` |
| board row | `<tr class="finding">` | same five |
| dead-end entry | `<p class="finding">` | same five |

- `data-model`: space-joined `models` (empty string when the finding has
  none); the model filter matches if the selection is among them.
- `data-tier`: the finding's tier; absent/empty for non-lineup findings.
- `data-title` / `data-rationale`: the rendered text, for search.
- Numeric table cells additionally carry `data-value="<number>"` (the
  resolved value) for numeric sorting; stale / no-measurement cells carry
  none (they sort last).
- Sortable value columns carry `data-sort="higher|lower"` on the `<th>`
  (the metric's direction) so the JS can implement best-first.

Hiding is done with the `hidden` attribute; the DOM (and therefore the
JS-disabled document) always contains every entry (FR-010).

## Default table order (documented, FR-010 / US5 scenario 4)

The order the generated document renders in — what a JS-disabled reader
sees and the JS sort's reset state:

| Table | Order |
|---|---|
| lineup finding tables | findings: tier order `quality` → `fast` → `additive`, then finding id (v1 order, unchanged); metrics: declared order in the TOML (v1 order, unchanged) |
| candidate status board | status in `shipped` → `on-trial` → `retired` → `dead-end` → `open`, then title (case-insensitive) — v2's status grouping (FR-003) |
| dead ends | finding id (v1 load order, unchanged) |

## Validation rules

Unchanged from v1 (data-model.md of 001, FR-007): the generator validates
the findings layer exactly as before; v2 adds **no** findings-file fields.
If a richer chart wants a metric not yet marked `headline`, that is a
findings-layer edit (mark a metric), not a schema change (spec Assumptions).
