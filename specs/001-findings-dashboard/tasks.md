# Tasks: Findings Dashboard

**Input**: Design documents from `/specs/001-findings-dashboard/`

**Prerequisites**: plan.md (required), spec.md (required for user stories), research.md, data-model.md, contracts/

**Tests**: MANDATORY — constitution VII (Test-First, NON-NEGOTIABLE) and plan.md bind this feature to red-green-refactor: every test task below must be written and **seen to fail** before its implementation task.

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2, US3)
- Include exact file paths in descriptions

## Path Conventions

Single project at repository root: `dashboard/`, `findings/`, `site/`, `tests/` (per plan.md Project Structure). No `__init__.py` — flat script-package house style of `bench/` and `profiles/`; test files bootstrap `sys.path` (pattern in T002).

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Project initialization and basic structure

- [x] T001 Create directory skeleton per plan.md Project Structure: `dashboard/`, `findings/`, `tests/` at repository root (the generator creates `site/` itself). No `__init__.py` files (flat script-package house style); no dependencies to install — Python 3 stdlib only (research.md R1, R4)

**Checkpoint**: Skeleton exists; `python3 --version` ≥ 3.11 (for `tomllib`)

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core pipeline every user story depends on — source resolution, findings validation, section index, and the CLI exit-code contract with a page shell

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

- [x] T002 [P] Write FAILING tests for source resolution in `tests/test_sources.py` covering research.md R3 and the Source table in data-model.md: constructs are exactly `key`, `[N]`, `[?key=value]` and chain (e.g. `summary[?pp=16384].decode_tps`, `raw.result.verified_tokens`); predicate filter takes the **LAST matching element** (older `acc_*.json` files append the run's own records after server-side history — `bench/report.py::own_results`); final element must resolve to a scalar (number or string); missing file → stale outcome naming the file; well-formed but unresolvable path → stale outcome naming the path; a `file` reference containing `..` that escapes `results/` is rejected. Use real files (`results/tp_baseline.json`, `results/ctx_mtp.json`) plus temp files for stale cases. Every test file starts with the bootstrap: `sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))` then `import sources`
- [x] T003 Implement the path-expression parser and resolver in `dashboard/sources.py`: `resolve(file: Path, path: str) -> Resolution` where Resolution is `value` (scalar) or `stale` (reason: missing file | unresolvable path); exactly the three constructs from research.md R3; last-match-wins predicates; scalar check; `..` rejection. Make T002 pass (green)
- [x] T004 [P] Write FAILING tests for findings loading and validation in `tests/test_findings.py` covering data-model.md "Validation rules (FR-007)" verbatim: (1) TOML parse errors reported with line numbers; (2) required fields `id`, `title`, `status`, `models`, `section`, `metrics` present with correct types; (3) `status` ∈ {`shipped`, `on-trial`, `retired`, `dead-end`, `open`} and `direction` ∈ {`higher`, `lower`}; (4) `id` matches `^[a-z0-9][a-z0-9_-]*$`, is unique layer-wide, and equals the filename stem; (5) `section` ≥ 1; (6) every `source_file` stays inside `results/` and every `source_path` parses as a path expression (unresolvable-at-runtime is stale, NOT a validation failure); (7) metric names unique within a finding; an empty findings directory is not an error; a single run aggregates ALL problems (file + field each), not just the first
- [x] T005 Implement findings loading and validation in `dashboard/findings.py`: `load_findings(findings_dir) -> (findings, problems)` enforcing rules 1–7 with file + field on every problem; `load_sections(findings_md_path) -> dict[int, (title, anchor)]` parsing `## N. Title` headings into number → (title, GitHub-slug anchor) per research.md R6 (lowercase, punctuation stripped, spaces → dashes); a recorded `section_title` that no longer matches the heading at that number yields a warning, not a Problem. Make T004 pass (green)
- [x] T006 Write FAILING CLI contract tests in `tests/test_generate.py` per `contracts/cli.md`: valid layer with all sources resolvable → exit 0, `site/index.html` written, stdout summary line (findings count, views populated, stale count, output path); invalid layer (e.g. `status = "bogus"`) → exit 1, **no site written** (a pre-existing `site/index.html` stays byte-identical), stderr lists every problem with file + field. (Exit-2 stale behaviour is tested in US5.) Invoke the CLI as a subprocess: `python3 dashboard/generate.py` with cwd set to a temp repo fixture (temp `findings/` + real `results/` + temp `docs/FINDINGS.md`)
- [x] T007 Implement the page shell and CLI entry: `dashboard/render.py` with `render_page(...) -> str` emitting one self-contained HTML document — header with "generated at" stamp, three empty anchored sections `#lineup`, `#board`, `#dead-ends`, and the "no findings yet" empty state when the layer is empty — and `dashboard/generate.py` with `main() -> int` + `raise SystemExit(main())` (house style of `bench/report.py`): loads findings + sections, resolves every metric source, writes exactly `site/index.html` (creating `site/` if absent), exits 0 clean / 1 validation failure (site NOT written, all problems on stderr). Strictly read-only everywhere else (FR-010). Make T006 pass (green)

**Checkpoint**: `python3 dashboard/generate.py` runs end-to-end with correct exit codes; the site is an empty shell; `python3 -m unittest discover -s tests -v` is green

---

## Phase 3: User Story 1 - Read the live lineup with evidence-backed numbers (Priority: P1) 🎯 MVP

**Goal**: The lineup view lists the shipped models/profiles with headline metrics; every displayed number is resolved from its named source at generation time and carries a hover/keyboard tooltip (what / how / evidence + FINDINGS.md link); headline metrics also appear in simple comparison charts.

**Independent Test**: Generate the site with only the lineup seed populated; open it from disk; for every displayed number, verify a tooltip appears on hover AND keyboard focus stating what/how/evidence, that its FINDINGS.md link resolves to a real section, and that the displayed value equals the value resolved from the source file at generation time (spec US1).

### Tests for User Story 1 ⚠️ (write FIRST, watch them fail)

- [x] T008 [P] [US1] Write FAILING tests in `tests/test_render_lineup.py`: every `shipped` finding renders in `#lineup` grouped by tier (quality / fast / additive); **no number is rendered without a tooltip** (FR-004); each tooltip contains what / how / evidence and a link to the FINDINGS.md section (FR-004); the value rendered in HTML equals the value resolved from the source file at generation time — assert against a temp results fixture, proving nothing is hardcoded (FR-005, SC-002); tooltip triggers are hover AND keyboard focus (focusable elements) and dismissible, with no information hover-only (FR-009); a metric with no source renders a "no measurement" marker
- [x] T009 [P] [US1] Write FAILING tests in `tests/test_render_charts.py` for FR-006 + research.md R2: the lineup includes inline-SVG bar charts comparing headline metrics across models; a chart displays **no value that is not also present in a table with a tooltip**; every chart element is focusable (`tabindex="0"`) and carries the same tooltip as its table row; charted values equal table values
- [x] T010 [P] [US1] Write FAILING determinism and empty-state tests in `tests/test_generate.py` (extend): two runs over identical inputs produce byte-identical `site/index.html` except the "generated at" stamp line (FR-012, clarification Q1 — the stamp is the ONLY allowed difference); an empty findings layer renders the explicit "no findings yet" message, not a blank page (edge case)
- [x] T011 [P] [US1] Seed the lineup findings (FR-013, part 1) in `findings/`: `lineup_quality_8bit.toml` (Qwen3.8-27B-8bit with its four profiles — code / agent / reason / longctx — FINDINGS §19 and §4; headline metrics: decode @ 16k prompt tok/s and context ceiling 262,144 tokens), `lineup_fast_oq4e.toml` (oQ4e fast tier, §19; headline decode tok/s), `lineup_additive_gemma4.toml` (gemma-4-31B, §30 and §32; headline accuracy and 65k context ceiling) — each per `contracts/findings-schema.md` with `tooltip_what/how/evidence` taken from the cited FINDINGS section and `source_file`/`source_path` pointing at the results files that section cites; verify every `source_path` resolves (generator exits 0)

### Implementation for User Story 1

- [x] T012 [US1] Implement the lineup view in `dashboard/render.py`: `#lineup` section with per-model/profile rows grouped by tier, headline-metric tables (value + unit), the shared tooltip mechanism (one inline CSS/JS block: hover + keyboard focus + dismissible, FR-009), tooltip content in the order what / how / evidence / links per `contracts/ui.md` (FINDINGS.md anchor from `load_sections`), and the "no measurement" marker. Make T008 pass (green)
- [x] T013 [US1] Implement the comparison charts in `dashboard/render.py`: inline-SVG single-series bar charts of headline metrics across models (research.md R2 — ~50 lines of SVG emission, no library), each bar a focusable element reusing the tooltip mechanism, values identical to the tables. Make T009 pass (green)
- [x] T014 [US1] Enforce determinism in `dashboard/render.py` + `dashboard/generate.py`: stable ordering everywhere (findings sorted by `id`, metrics in file order, sorted iteration for anything else), the "generated at" stamp as the only non-deterministic field (FR-012). Make T010 pass (green)

**Checkpoint**: US1 independently testable — open `site/index.html`, walk one metric end-to-end (SC-001: what / how / evidence in under 10 s)

---

## Phase 4: User Story 2 - See the status of every candidate model (Priority: P1)

**Goal**: A status board where every evaluated model/lever appears exactly once with one of the five statuses and a rationale naming its deciding measurement, so the lineup's history is legible at a glance.

**Independent Test**: Generate the site with only the board seed populated; verify every evaluated model appears exactly once with a valid status and a rationale that names the deciding measurement and links to its FINDINGS section (spec US2).

### Tests for User Story 2 ⚠️ (write FIRST, watch them fail)

- [x] T015 [P] [US2] Write FAILING tests in `tests/test_render_board.py`: `#board` renders every finding exactly once per the status→view mapping in data-model.md (`shipped` → lineup + board; `on-trial` / `retired` / `open` → board; `dead-end` → board + dead ends); each row shows title, model(s), a status badge from the enum `shipped | on-trial | retired | dead-end | open` (FR-003), and a rationale; rows with status `retired` or `dead-end` carry a rationale that names the deciding measurement and links to the FINDINGS section (US2 acceptance 2)
- [x] T016 [P] [US2] Seed the closed-candidate findings (FR-013, part 2) in `findings/`: `closed_codernext.toml` (Coder-Next, `retired`, §12/§13), `closed_flashnext.toml` (Qwen3.8-Flash-Next, `dead-end`, §26/§27), `closed_optiq.toml` (OptiQ, `retired`, §29), `closed_qwen35_moe.toml` (qwen3_5_moe 35B-A3B class, `dead-end`, §34) — each per `contracts/findings-schema.md` with `rationale` naming the deciding measurement and its evidence (accuracy gap with p-value, or residency cost) taken from the cited FINDINGS section

### Implementation for User Story 2

- [x] T017 [US2] Implement the candidate status board in `dashboard/render.py`: `#board` section, one row per finding (title, model(s), status badge, rationale, links), populated per the data-model.md status→view mapping. Make T015 pass (green)

**Checkpoint**: US1 + US2 independently testable — board shows the full evaluated history with statuses and deciding measurements

---

## Phase 5: User Story 3 - See the settled dead ends (Priority: P1)

**Goal**: The "do not re-open" list from backlog §H rendered as first-class entries, each with the measurement that closed it — or, for argument-closed entries, a visible "no measurement" marker and the argument.

**Independent Test**: Generate the site with only the dead-ends seed populated; verify every entry from backlog §H appears with its closing measurement and source link (spec US3).

### Tests for User Story 3 ⚠️ (write FIRST, watch them fail)

- [x] T018 [P] [US3] Write FAILING tests in `tests/test_render_deadends.py`: `#dead-ends` lists exactly the findings with status `dead-end`; each entry shows the measurement that closed it with a source link (US3 acceptance 1); an entry closed by reasoning rather than measurement is marked "no measurement" and states the argument (US3 acceptance 2)
- [x] T019 [P] [US3] Seed the dead-end findings (FR-013, part 3) in `findings/` — one `deadend_*.toml` per settled item in `docs/backlog.md` §H: ANE prefill, TurboQuant 4-bit KV, the in-memory hot cache, `chunked_prefill`, `specprefill` on a dense checkpoint, raising the concurrency limit, `qwen-code` temperature 0.7 vs 0.3 (all closed by measurement, §3/§5/§8), GLM-5.2 / Kimi-class viability on 128 GB (closed by argument → "no measurement"), and plain-mmap overflow handling (closed by argument/measurement) — each per `contracts/findings-schema.md` with `rationale` carrying the closing measurement or argument verbatim in substance from §H

### Implementation for User Story 3

- [x] T020 [US3] Implement the dead-ends view in `dashboard/render.py`: `#dead-ends` section, one entry per `dead-end` finding with the closing measurement + source link, and the "no measurement" marker with the argument for reasoning-closed entries. Make T018 pass (green)

**Checkpoint**: All three P1 stories independently testable — a new session sees the lineup, the full candidate history, and the do-not-re-open list

---

## Phase 6: User Story 4 - Add or update a finding by editing one file (Priority: P2)

**Goal**: The marginal cost of a new finding is one TOML file plus one generator run — no generator code change — and the generator is provably read-only.

**Independent Test**: Add a new findings file referencing an existing results file; regenerate; verify the new finding renders with correct values and tooltip; remove the file and regenerate; verify it is gone (spec US4).

### Tests for User Story 4 ⚠️ (write FIRST, watch them fail)

- [x] T021 [P] [US4] Write FAILING tests in `tests/test_generate.py` (extend): (a) one-file flow (SC-003) — a new valid findings file referencing an existing results file renders with resolved values and tooltip after exactly one run, with **no change to any file under `dashboard/`** (assert via before/after content hashes); (b) removing the file and regenerating removes the finding from the site; (c) a status change (`on-trial` → `shipped`) moves the finding between views accordingly (US4 acceptance 3); (d) a findings file with a typo'd `source_path` makes the generator fail with a message naming the file, the finding, and the unresolvable path, and a non-zero exit (US4 acceptance 2); (e) read-only guarantee (FR-010, US4 scenario 4) — a full generation run leaves `results/` and `profiles/` byte-identical (before/after comparison in a temp fixture)

### Implementation for User Story 4

- [x] T022 [US4] Close any gaps the T021 tests expose in `dashboard/generate.py` / `dashboard/findings.py` / `dashboard/render.py` (expected: none or minimal — the one-file flow is the design's point, SC-003); confirm the stderr message for an unresolvable path names file + finding + path. Make T021 pass (green)

**Checkpoint**: US4 independently testable — the curation loop costs one file + one run; the live stack is untouched

---

## Phase 7: User Story 5 - Stale sources are flagged, never silent (Priority: P2)

**Goal**: A deleted or schema-drifted results file produces a visible stale marker on exactly the affected findings, a non-zero exit, and an intact rest of the site — never a silent wrong or absent number.

**Independent Test**: Generate a valid site; delete one referenced results file; regenerate; verify the site shows a stale marker on exactly that finding and the generator's exit code is non-zero (spec US5).

### Tests for User Story 5 ⚠️ (write FIRST, watch them fail)

- [x] T023 [P] [US5] Write FAILING tests in `tests/test_generate.py` (extend): (a) a finding whose source file no longer exists → site still written, a visible stale marker on exactly that finding, no value shown, exit 2, stderr names the finding + file (FR-008, US5 acceptance 1); (b) a finding whose `source_path` no longer resolves (schema drift) → same stale treatment, message names the path (US5 acceptance 2); (c) all sources resolve → no stale markers anywhere and exit 0 (US5 acceptance 3); (d) the rest of the site renders normally around a stale finding (other views and findings unaffected)

### Implementation for User Story 5

- [x] T024 [US5] Implement stale rendering and the exit-2 path in `dashboard/render.py` + `dashboard/generate.py`: stale metrics render the visible stale marker (no value), the finding carries the marker, exit code 2 when any source is stale (site IS written), per `contracts/cli.md` and `contracts/ui.md` marker table. Make T023 pass (green)

**Checkpoint**: US5 independently testable — the evolution guarantee (constitution IV) holds: drift is loud and local

---

## Phase 8: User Story 6 - Deep links from any metric to its raw sources (Priority: P3)

**Goal**: From any metric the reader reaches the FINDINGS.md section that argues the point and the raw results file that carries the number — trust delegated to the primary sources in one click.

**Independent Test**: For a sample of metrics, click through to the FINDINGS section anchor and the raw JSON; verify both resolve (spec US6).

### Tests for User Story 6 ⚠️ (write FIRST, watch them fail)

- [x] T025 [P] [US6] Write FAILING tests in `tests/test_render_links.py`: every metric with a source links to both its FINDINGS.md section anchor and its raw results file (FR-014); anchors are computed from the real `docs/FINDINGS.md` `## N. Title` headings (GitHub slug rules, research.md R6) — assert against at least three real sections, e.g. §4, §19, §30; links are relative paths that resolve from `file://` (`docs/FINDINGS.md#…`, `../results/<file>.json`); a finding with no measurement is marked "no measurement" and offers only the documentation link (US6 acceptance 2); a recorded `section_title` that no longer matches the heading at that number produces a stderr warning and leaves the exit code unchanged (edge case, research.md R6)

### Implementation for User Story 6

- [x] T026 [US6] Implement the deep-link contract in `dashboard/render.py`: raw-results-file links on every sourced metric (table rows, tooltip, and chart elements), "no measurement" entries with documentation link only, and the section-title mismatch warning emitted from `dashboard/generate.py`. Make T025 pass (green)

**Checkpoint**: All six user stories independently functional

---

## Phase 9: Polish & Cross-Cutting Concerns

**Purpose**: Whole-feature verification against the spec's success criteria and the quickstart

- [x] T027 Run the full quickstart validation in `specs/001-findings-dashboard/quickstart.md` (scenarios 1–8) against the real repo: test suite green; generation exit 0; determinism diff shows only the stamp line; stale and validation-failure scenarios behave per `contracts/cli.md`; read-only check clean; seed coverage per FR-013 (lineup: Qwen3.8-27B-8bit + four profiles, oQ4e, gemma-4-31B; board: Coder-Next, Qwen3.8-Flash-Next, OptiQ, qwen3_5_moe 35B-A3B class; dead ends: backlog §H list)
- [x] T028 [P] Verify SC-004: time a full generation against the current `results/` volume (~189 files + seed findings) and confirm it completes in under 5 seconds; record the measured time in the commit message or backlog
- [x] T029 [P] Final read-only audit (FR-010, constitution II): after a full generation, `git status --porcelain results/ profiles/` is empty and no oMLX configuration file was touched; confirm `site/index.html` is the only artifact written and is committed alongside the findings layer (FR-015)
- [x] T030 [P] HTML sanity pass on `site/index.html`: no external references (no `http://`, `https://`, `src=` to remote, no `@import`), valid enough to open from `file://` in a browser with all three views, tooltips, and charts working offline (FR-011); long tooltip text and non-ASCII content wrap intact, no truncation (edge case)

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — can start immediately
- **Foundational (Phase 2)**: Depends on Setup — **BLOCKS all user stories**
- **User Stories (Phases 3–8)**: All depend on Foundational. US1/US2/US3 (P1) are independent of each other once the shell exists (each adds its own view to `dashboard/render.py` — see serial note below); US4/US5 (P2) build on the CLI + resolution core; US6 (P3) refines link rendering
- **Polish (Phase 9)**: Depends on all desired user stories being complete

### User Story Dependencies

- **US1 (P1)**: After Foundational — no dependencies on other stories
- **US2 (P1)**: After Foundational — independent of US1 (separate view, separate test file)
- **US3 (P1)**: After Foundational — independent of US1/US2
- **US4 (P2)**: After Foundational (CLI + validation exist); its tests exercise the whole pipeline, so run after US1–US3 for a meaningful site, though the code it guards is foundational
- **US5 (P2)**: After Foundational (resolution outcomes exist); stale rendering extends the views, so schedule after US1–US3
- **US6 (P3)**: After US1 (tooltips exist to carry the raw-file links)

### Within Each User Story

- Tests MUST be written and **seen to fail** before implementation (constitution VII)
- Seed data tasks ([P]) can land any time after the story's rendering exists, but must resolve cleanly (generator exit 0) before the story checkpoint
- Story complete (checkpoint green) before moving to the next priority

### ⚠️ Serial note: `dashboard/render.py`

View implementation tasks T012, T013, T014, T017, T020, T024, T026 all edit `dashboard/render.py` — they are **not parallel with each other** despite belonging to different stories. The [P] test/seed tasks around them are parallel (different files). With one implementer, execute stories in priority order; with several, split by story but serialize on `render.py` (or land one view per merge).

### Parallel Opportunities

- T002 ∥ T004 (disjoint test files, both before their implementations)
- Within a story: its [P] test tasks ∥ its [P] seed task (e.g. T008 ∥ T009 ∥ T010 ∥ T011)
- Polish: T028 ∥ T029 ∥ T030
- Across stories: seed-data curation (T011/T016/T019) is pure data work and can proceed while another story's render code is in flight

---

## Parallel Example: User Story 1

```bash
# Launch together (disjoint files):
Task: "T008 [US1] Failing lineup/tooltip tests in tests/test_render_lineup.py"
Task: "T009 [US1] Failing chart tests in tests/test_render_charts.py"
Task: "T010 [US1] Failing determinism/empty-state tests in tests/test_generate.py (extend)"
Task: "T011 [US1] Seed lineup findings in findings/ (FR-013 part 1)"

# Then, strictly serial (same file: dashboard/render.py, generate.py):
Task: "T012 [US1] Lineup view + tooltip mechanism"
Task: "T013 [US1] Comparison charts (inline SVG)"
Task: "T014 [US1] Determinism + stable ordering"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup
2. Complete Phase 2: Foundational (CRITICAL — blocks all stories)
3. Complete Phase 3: User Story 1
4. **STOP and VALIDATE**: open `site/index.html`; every lineup number has a tooltip (what/how/evidence + link) and matches its source file (SC-001, SC-002)
5. Demo-ready: the core value — "on board with the findings" — works

### Incremental Delivery

1. Setup + Foundational → pipeline runs, exit codes correct
2. + US1 → lineup with evidence-backed numbers (MVP!)
3. + US2 → candidate status board
4. + US3 → settled dead ends
5. + US4 → one-file curation loop proven
6. + US5 → stale-flag guarantee
7. + US6 → deep links to raw sources
8. Polish → quickstart 1–8 green, SC-004 timing, read-only audit
9. Each story adds value without breaking previous stories; commit the regenerated `site/index.html` with every findings-layer change (FR-015)

### Parallel Team Strategy

1. One person completes Setup + Foundational
2. Then, per story: tests + seed data in parallel, render tasks serialized on `dashboard/render.py`
3. Alternative split: person A owns render.py (all view tasks in priority order) while person B curates seed findings (T011/T016/T019) and writes the story tests

---

## Notes

- [P] tasks = different files, no dependencies on incomplete tasks
- [Story] label maps task to specific user story for traceability
- Constitution VII: no production code lands without a test that failed first — the failing-test tasks T002, T004, T006, T008, T009, T010, T015, T018, T021, T023, T025 are the red half of each green-red pair (T011, T016, T019 are seed-data tasks, not tests)
- Commits happen only when the user asks (constitution workflow); when they happen, `site/index.html` travels with its findings-layer change (FR-015)
- Stop at any checkpoint to validate the story independently
- Avoid: vague tasks, same-file conflicts (see serial note), cross-story dependencies that break independence
