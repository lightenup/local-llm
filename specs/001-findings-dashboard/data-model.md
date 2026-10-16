# Data Model: Findings Dashboard

**Feature**: 001-findings-dashboard | **Date**: 2026-09-22

Entities below are the spec's Key Entities, made concrete. The findings layer
(`findings/*.toml`) is the only persistent data this feature adds; everything
else is read (`results/*.json`, `docs/FINDINGS.md`) or derived
(`site/index.html`).

## Finding

One evaluated claim about a model or lever. One TOML file per finding;
filename stem == `id`.

| Field | Type | Required | Rules |
|---|---|---|---|
| `id` | string | yes | `^[a-z0-9][a-z0-9_-]*$`; unique across the layer; equals filename stem |
| `title` | string | yes | non-empty |
| `status` | enum | yes | one of `shipped`, `on-trial`, `retired`, `dead-end`, `open` (FR-003) |
| `models` | list[string] | yes (may be empty) | model ids the finding concerns; empty for pure lever/idea findings |
| `section` | integer | yes | FINDINGS.md section number, ≥ 1 |
| `section_title` | string | no | recorded heading title; generator warns on mismatch (R6) |
| `rationale` | string | no | required in practice for `retired`/`dead-end` (names the deciding measurement); rendered on the board and dead-ends views |
| `updated` | date | no | curation date, displayed if present |
| `metrics` | list[Metric] | yes (may be empty) | empty ⇒ finding renders with a "no measurement" marker (judgement/wiring decision) |

**View mapping** (status → where the finding renders, FR-006):

| status | lineup | candidate board | dead ends |
|---|---|---|---|
| `shipped` | ✔ (headline metrics) | ✔ | |
| `on-trial` | | ✔ | |
| `retired` | | ✔ | |
| `dead-end` | | ✔ | ✔ |
| `open` | | ✔ | |

## Metric

One number inside a finding.

| Field | Type | Required | Rules |
|---|---|---|---|
| `name` | string | yes | non-empty; unique within a finding |
| `unit` | string | yes | e.g. `tok/s`, `%`, `tokens`, `GB` |
| `direction` | enum | yes | `higher` or `lower` (which way is better) |
| `headline` | bool | no (default false) | `true` ⇒ shown in the lineup headline and in the comparison charts |
| `source` | Source | no | absent ⇒ "no measurement" marker; present ⇒ value resolved at generation time (FR-005) |
| `tooltip.what` | string | yes | what the metric measures |
| `tooltip.how` | string | yes | bench script + suite + settings |
| `tooltip.evidence` | string | yes | sample size, p-value, or "deterministic" |
| `baseline.label` | string | no | comparator name (e.g. "baseline, MTP off") |
| `baseline.source` | Source | no | if present, the baseline value is also resolved, never hardcoded |

## Source

A pointer into `results/`.

| Field | Type | Rules |
|---|---|---|
| `file` | string | relative to `results/`, no `..` components, must name a `.json` file |
| `path` | string | path expression per research.md R3: `key`, `[N]`, `[?key=value]`, chainable; must resolve to a scalar |

**Resolution outcome** (computed at generation time, drives rendering):

| Outcome | Condition | Rendering |
|---|---|---|
| `value` | file exists, path resolves to a scalar | number + unit, full tooltip, links |
| `stale` | file missing **or** path no longer resolves | visible stale marker on the finding, no value shown, generator exit 2 (FR-008) |
| `no measurement` | metric has no `source` | "no measurement" marker, documentation link only (US6 scenario 2) |

## Status

The finding's lifecycle state (FR-003). Transitions are curation edits, never
automatic:

```text
open ──► on-trial ──► shipped
  │          │
  │          └──────► retired
  └──────────────────► dead-end      (any state may close as dead-end on evidence)
```

Closing a finding (`retired`/`dead-end`) requires a `rationale` naming the
deciding measurement; per constitution I, closed findings are not
re-litigated — re-opening is a new finding with new evidence.

## View

A section of the single-page dashboard, populated by the mapping above:

- **lineup** — `shipped` findings, grouped by tier (quality / fast /
  additive); headline metrics in tables + comparison charts (FR-006).
- **candidate board** — every non-`shipped`-only finding, i.e. all evaluated
  models/levers exactly once, with status + rationale (US2).
- **dead ends** — `dead-end` findings: the lever/idea, the measurement that
  closed it (or the argument, marked "no measurement"), source link (US3).

## Validation rules (FR-007)

The generator validates every findings file and fails with exit 1, no site
written, listing **every** problem (file + field):

1. TOML parses (syntax errors reported with line numbers).
2. Required fields present with correct types.
3. `status` ∈ enum; `direction` ∈ enum.
4. `id` well-formed, unique layer-wide, equals filename stem.
5. `section` ≥ 1; if `section_title` given, heading exists at that number
   (mismatch ⇒ warning, not failure — R6).
6. Every `source.file` stays inside `results/`; every `source.path` parses as
   a path expression (unresolvable-at-runtime is *stale*, exit 2 — not a
   validation failure).
7. Metric names unique within a finding.

An **empty** `findings/` directory is not an error: the site renders an
explicit "no findings yet" message (edge case).
