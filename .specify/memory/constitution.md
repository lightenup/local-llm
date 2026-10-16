# local-llm Constitution

## Core Principles

### I. Evidence First
Every claim — in code, docs, or UI — is traceable to a measurement in
`results/*.json` or explicitly labelled an assumption. Numbers ship with their
method: suite, sample size, and p-value (or "deterministic"). Items closed by
measurement (FINDINGS "closed" sections, backlog §H "Settled — do not re-open")
are not re-litigated; re-opening requires new evidence, not a second opinion.

### II. Never Break the Live Stack
Anything that touches oMLX settings, profiles, or engine variants restores its
state on every exit path (`finally` blocks, `EXIT` traps) and verifies after.
Long unattended chains stop at deadlines and always hand back the shipped
configuration. Read-only consumers (dashboards, reports, this spec workflow)
MUST NOT mutate `results/`, `profiles/`, or the oMLX configuration.

### III. Reuse Before Build
Prefer what already exists: the `bench/` harness and `omlx_client.py` for
measuring, the oMLX API for state, the Python standard library, and established
libraries over new dependencies. A new dependency or a reimplementation of an
existing capability must be justified in the implementation plan.

### IV. Data Over Hardcoding
UIs and reports derive values from data via explicit references (file + path),
never from copy-pasted numbers. When the underlying measurement changes or
disappears, the derived artifact must fail loudly or flag itself stale — never
display a silent wrong number.

### V. Local-First, No Telemetry
Artifacts run offline on this machine: no network access at runtime, no
uploads, no secrets in the repo. (Known exception, documented: oMLX uploads
accuracy suites with ≥100 questions to omlx.ai; run `SAMPLES=99` to stay fully
local.)

### VI. KISS / YAGNI
Keep it simple. The smallest implementation that satisfies the spec wins. No
speculative features, no abstractions for hypothetical needs, no configuration
that is not read. If a capability is not in the spec, do not build it — record
it in the backlog instead. Any added complexity must be justified in the
implementation plan; simplicity is the default, not the fallback.

### VII. Test-First (NON-NEGOTIABLE)
TDD is mandatory for new code: write a failing test, watch it fail (red), write
the minimum code to pass (green), then refactor — and only while green. No
production code lands without a test that failed first. This applies to the
dashboard generator, findings-layer validation, and any change to the
benchmark harness. A change that cannot be tested is a change that is not
ready to be made.

## Additional Constraints

- Python 3, standard library first; shell scripts for orchestration.
- Generated artifacts are deterministic and idempotent: same inputs, same
  bytes.
- `results/` is append-only from every consumer's perspective.
- Engine-level oMLX fields live once on the base model; profiles carry only
  universal fields (see `profiles/definitions.py` design rule). Dashboard work
  does not change this boundary.

## Development Workflow

- Spec-driven flow: `/speckit.specify` → optional `/speckit.clarify` →
  `/speckit.plan` → `/speckit.tasks` → optional `/speckit.analyze` →
  `/speckit.implement`.
- New code follows red-green-refactor (Principle VII): the test exists and has
  been seen to fail before the implementation is written.
- Verification before "done": run the feature's full test suite; anything
  touching profiles additionally runs `python3 profiles/apply.py --verify-only`
  and `python3 bench/smoke.py`.
- FINDINGS.md is the record of measurements; backlog.md is the record of open
  work. They supersede specs on measured facts.
- Commits happen only when the user asks.

## Governance

This constitution supersedes other project practices. Amendments require
editing this file, bumping the version, and a one-paragraph rationale in the
commit or backlog. Where a spec conflicts with this constitution, the
constitution wins and the spec is corrected.

**Version**: 1.1.0 | **Ratified**: 2026-09-22 | **Last Amended**: 2026-09-22

**Amendment 1.1.0 (2026-09-22):** Added Principle VI (KISS / YAGNI) and
Principle VII (Test-First, red-green-refactor, non-negotiable), mandated by
the owner during dashboard spec review; the workflow now requires seeing a
test fail before implementation.
