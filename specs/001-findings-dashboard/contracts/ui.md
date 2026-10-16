# Contract: Dashboard UI (site/index.html)

**Feature**: 001-findings-dashboard

Single self-contained HTML file, no network, no server, opens from `file://`
(FR-011). One page, three views as anchored sections, plus a header.

## Layout

```text
Findings — local-LLM stack            [generated at: <invocation time>]
──────────────────────────────────────────────────────────────────────────
#lineup        Live lineup            shipped findings, grouped by tier
#board         Candidate status board every evaluated model/lever, once each
#dead-ends     Settled dead ends      do-not-re-open list
```

- **Lineup**: per model/profile — name, tier (quality / fast / additive),
  headline metrics in a table (value + unit), plus simple bar charts
  comparing headline metrics across models (FR-006, research.md R2).
- **Board**: one row per finding — title, model(s), status badge (one of the
  five statuses), rationale naming the deciding measurement, links.
- **Dead ends**: one entry per dead-end finding — lever/idea, the
  measurement that closed it (or the argument, marked "no measurement"),
  source link.
- **Empty state**: an empty findings layer renders an explicit
  "no findings yet" message, not a blank page.

## Tooltip contract (FR-004, FR-009)

Every displayed metric — in tables **and** in charts — carries a tooltip
containing, in order:

1. **what** — what the metric measures
2. **how** — which bench script and suite, what settings
3. **evidence** — sample size, p-value, or "deterministic"
4. **links** — the FINDINGS.md section anchor and, when the metric has a
   source, the raw results file (FR-014)

Trigger: hover **and** keyboard focus (elements are focusable; chart elements
are SVG nodes with `tabindex="0"`). Dismissible (blur/click/Esc). No
information is hover-only: the same content is reachable by keyboard, and
links are real anchors.

## Marker contract

| Condition | Rendering |
|---|---|
| source resolves | value + unit, full tooltip, links |
| source stale (file missing or path drifted) | visible **stale** marker on the finding, no value shown, message names the file/path (FR-008) |
| metric has no source | "no measurement" marker, documentation link only (US6 scenario 2) |
| findings layer empty | "no findings yet" message |

Staleness is loud and local: exactly the affected findings are marked; the
rest of the site renders normally (US5).

## Link contract

- FINDINGS.md: `docs/FINDINGS.md#<github-slug>` computed from the
  `## N. Title` heading at the finding's recorded section number
  (research.md R6). Relative path so it resolves from `file://`.
- Raw results: `../results/<file>.json`, relative, resolves from `file://`.
- A charted value links exactly like its table row — a chart MUST NOT display
  any value that is not also present in a table with a tooltip (FR-006).

## Long content

Long tooltip text and non-ASCII content wrap and render intact — no
truncation (edge case).
