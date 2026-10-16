# Contract: Generator CLI — v2 invariants

**Feature**: 002-findings-dashboard-ux

**The CLI is unchanged from v1** ([001 contracts/cli.md](../../001-findings-dashboard/contracts/cli.md)
continues to apply in full). v2 adds no flags, changes no exit codes, and
writes no additional files. This file records the invariants v2 MUST keep
(FR-001) so the tasks phase has them in one place.

## Command

```text
python3 dashboard/generate.py
```

No flags. House style of `bench/report.py`: plain script,
`main() -> int`, `raise SystemExit(main())`.

## Inputs (read-only) — unchanged

`findings/*.toml`, `results/*.json`, `docs/FINDINGS.md`. The generator
MUST NOT write, delete, or re-order anything except its single output;
`results/`, `profiles/`, and the oMLX configuration must be byte-identical
before and after a run (FR-015, v1 US4 scenario 4).

## Output — same file, v2 content

Exactly one file: `site/index.html` — self-contained (inline CSS/JS/SVG),
opens from `file://`, no network (FR-011). Committed; every
findings-layer change ships with a regenerated site (v1 FR-015).

Determinism (FR-013): identical inputs ⇒ byte-identical output **except**
the "generated at" stamp — the only allowed difference, as in v1. No v2
feature may introduce another non-deterministic field (the inline CSS/JS
are static constants — research.md R8).

## Exit codes — unchanged

| Code | Meaning | Site written? |
|---|---|---|
| `0` | all findings valid, all sources resolved | yes |
| `1` | findings-layer validation failure — every problem listed on stderr (file + field) | **no** (v1 FR-007) |
| `2` | one or more stale sources — site rendered with visible stale markers on exactly the affected findings (v1 FR-008) | yes |

Warnings (e.g. `section_title` mismatch) go to stderr and never change the
exit code. Stale findings render their markers in v2 exactly as in v1 and
participate in filter/sort like any other finding (spec edge case).

## stdout / stderr — unchanged

- stdout: the short summary line (findings count, views populated, stale
  count, output path).
- stderr: all problems, warnings, and per-stale-source messages naming the
  finding, the file, and the unresolvable path.
