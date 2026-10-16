# Research: Findings Dashboard

**Feature**: 001-findings-dashboard | **Date**: 2026-09-22
**Purpose**: Resolve the technical unknowns from the plan's Technical Context
before design. All NEEDS CLARIFICATION items from the spec were resolved with
the owner during `/speckit.specify` (Q1–Q3); this file covers the remaining
implementation-level decisions.

## R1. Findings-layer file format

**Decision**: TOML, one file per finding in `findings/`, read with stdlib
`tomllib`.

**Rationale**:
- Stdlib on Python ≥ 3.11 (repo runs 3.14) — zero new dependencies
  (constitution III).
- Findings are hand-edited (US4); TOML supports comments, which JSON does
  not — a status change ("on-trial → shipped because §30 closed it") wants a
  comment next to the field.
- Strict parsing with line-numbered errors fits FR-007's "fail loudly, list
  every problem found".
- `tomllib` is read-only, which is exactly the access pattern: the generator
  never writes findings files.

**Alternatives considered**:
- *JSON* — stdlib and already the repo's data format, but no comments make
  hand-editing long tooltip text painful (quote escaping on every line).
- *YAML* — comment-friendly, but requires PyYAML, a third-party dependency
  that would need justification for what TOML gives for free.
- *Python modules* (the `profiles/definitions.py` pattern) — findings would
  become executable code; the spec calls for "small structured files", and a
  data format keeps the curation surface inert.

## R2. Charts: generated inline SVG, no charting library

**Decision**: The lineup's comparison charts (FR-006) are simple bar charts
emitted as inline SVG by `render.py`. No charting library is added.

**Rationale**:
- FR-006 requires *every chart element* to carry the same tooltip and source
  links as its table row. SVG elements are focusable DOM nodes
  (`tabindex="0"`) that reuse the page's one tooltip mechanism — hover,
  keyboard focus, dismissible (FR-009) — with no extra machinery.
- FR-012 (deterministic bytes) and FR-011 (self-contained, vendored-only)
  both favor generated text over a vendored JS bundle or binary PNGs.
- Constitution VI (KISS): the charts needed are single-series bars of
  headline metrics across models — ~50 lines of SVG emission, no axes
  library, no interaction library.
- This upgrades the spec's assumption ("a charting library … is vendored"):
  the simpler alternative is strictly better on every constraint, so the
  plan justifies *no* dependency (constitution III) and the spec assumption
  is corrected accordingly.

**Alternatives considered**:
- *Vendored Chart.js* — built-in tooltips, but a ~200 KB JS file to vendor
  and keep current, tooltip content injected per dataset, and a second
  rendering stack to test for one page of bar charts.
- *matplotlib → PNG* — a heavy numeric dependency, binary output (breaks
  byte-diff review), and images cannot carry per-element tooltips, which
  would violate FR-006.

## R3. Source path-expression language

**Decision**: A minimal path expression with exactly three constructs,
resolved by `sources.py`:

```text
key                    # object member
[N]                    # integer list index
[?key=value]          # predicate filter over a list of objects
```

Chained: `summary[?pp=16384].decode_tps`,
`results[?benchmark=humaneval].accuracy`,
`raw.result.verified_tokens`.

**Predicate semantics — last match wins.** `bench/report.py::own_results`
documents that older `acc_*.json` files hold the whole server-side history
with the run's own records appended last, so the *own* record is the last
match, not the first. "Last match wins" is the safe universal rule: for
`tp_*.json` summary rows the (pp, batch) pair is unique, so first == last.

**Rationale**: Real schemas (verified in `results/`) need list selection —
`tp_*.json` keeps rows in `summary[]`, `acc_*.json` keeps suites in
`results[]` — so a plain dotted path is insufficient, but a full JSONPath
dependency is not. Three constructs cover every seed finding in FR-013.
The final element must resolve to a scalar (number or string) for display.

**Alternatives considered**:
- *Full JSONPath library* — new dependency for a feature the seed data never
  uses (constitution VI).
- *Dotted paths + index only* — cannot express "the humaneval row", which is
  the common accuracy case.

**Known hazard (mitigated, not coded)**: FINDINGS §27 withdrew a decode rate
computed over a 2-token generation; `report.py` suppresses `gen_short` rows.
The dashboard does not re-implement that filter — a curated finding simply
must not reference a `gen_short` row, and the tooltip's "how" text documents
the measurement's conditions. If this ever bites, the fix is a finding edit,
not a generator change.

## R4. Testing framework

**Decision**: `unittest` (stdlib) in a new top-level `tests/` directory, run
with `python3 -m unittest discover -s tests`.

**Rationale**: The repo has **no tests today** (no `test_*.py`, no pytest
config, no `import pytest` anywhere), so there is no house test convention to
follow — the dashboard creates it. Stdlib `unittest` keeps the zero-dependency
line (constitution III) and runs with the same `python3` as everything else.
Constitution VII (test-first) binds the tasks phase: each capability gets a
failing test before its implementation.

**Alternatives considered**:
- *pytest* — nicer fixtures/parametrize, but a third-party dependency whose
  value is marginal for a single-user tool with a small test surface.

## R5. Site artifact: single committed `site/index.html`

**Decision**: The generator writes one file, `site/index.html`, with all CSS,
JS, and SVG inlined. It is committed (clarification Q3 → C): every
findings-layer change ships with a regenerated site (FR-015), so staleness is
caught at commit time and the dashboard is always openable without running
anything.

**Rationale**: One file is the smallest thing that satisfies FR-011
(self-contained, `file://`, no server) and keeps the committed artifact
trivially reviewable in git history. A directory of assets would add paths to
keep in sync for no user-visible benefit (constitution VI).

**Alternatives considered**:
- *Git-ignored, regenerate on demand* — rejected by the owner (Q3).
- *Multi-file site* — rejected: no navigation benefit for three views on one
  page.

## R6. FINDINGS.md section links

**Decision**: Findings record the section **number** (and, optionally, the
title for drift detection). At generation time the generator parses
`## <N>. <Title>` headings from `docs/FINDINGS.md`, builds a
number → (title, anchor) map, and emits links like
`docs/FINDINGS.md#19-the-lineup-8-bit-for-quality-oq4e-for-speed` (GitHub
slug rules: lowercase, punctuation stripped, spaces → dashes). A recorded
title that no longer matches the heading at that number produces a **warning
on stderr** (exit code unaffected) — the spec's accepted maintenance cost for
renumbering.

**Rationale**: Section numbers are the stable handle (34 sections, numbered);
anchors must be computed, never hand-typed, or every rename breaks links.
Raw results files link as relative paths (`../results/<file>.json`) so they
resolve from `file://`.

**Alternatives considered**:
- *Hand-typed anchors* — drift on every heading edit.
- *Link by title only* — titles are long and get reworded; numbers are what
  the prose cross-references ("§27").

## R7. CLI shape and exit codes

**Decision**: `python3 dashboard/generate.py` — no flags in v1 (YAGNI).
Exit codes: `0` clean; `1` findings-layer validation failure (site **not**
written, every problem listed on stderr — FR-007); `2` stale sources (site
**is** written with visible markers — FR-008). Warnings (e.g. FINDINGS title
mismatch) go to stderr and never change the exit code.

**Rationale**: FR-001 requires a single command; the two non-zero codes let an
unattended chain distinguish "fix your findings file" from "a source drifted"
without parsing output. House style from `bench/report.py`: plain script,
`main() -> int`, `raise SystemExit(main())`.

**Alternatives considered**:
- *`--check` / `--output` flags* — no consumer exists that needs them
  (constitution VI); add when a real need appears.

## Open items

None. All decisions above are made; the tasks phase may refine module
boundaries inside `dashboard/` but not these decisions.
