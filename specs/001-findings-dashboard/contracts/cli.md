# Contract: Generator CLI

**Feature**: 001-findings-dashboard

## Command

```text
python3 dashboard/generate.py
```

No flags in v1 (research.md R7). House style of `bench/report.py`: plain
script, `main() -> int`, `raise SystemExit(main())`.

## Inputs (read-only)

| Input | Role |
|---|---|
| `findings/*.toml` | curated findings layer (the only data this feature owns) |
| `results/*.json` | measurement record — resolved per metric source, never mutated (FR-010) |
| `docs/FINDINGS.md` | section headings, parsed for link anchors (R6) |

The generator MUST NOT write, delete, or re-order anything except its single
output. `results/`, `profiles/`, and the oMLX configuration must be
byte-identical before and after a run (US4 scenario 4).

## Output

Exactly one file: `site/index.html` — self-contained (inline CSS/JS/SVG),
opens from `file://`, no network (FR-011). Committed to the repo; every
findings-layer change ships with a regenerated site (FR-015).

Determinism: identical inputs ⇒ byte-identical output **except** the
"generated at" stamp, which records the invocation time and is the only
allowed difference (FR-012, clarification Q1).

## Exit codes

| Code | Meaning | Site written? |
|---|---|---|
| `0` | all findings valid, all sources resolved | yes |
| `1` | findings-layer validation failure — every problem listed on stderr (file + field) | **no** (FR-007) |
| `2` | one or more stale sources — site rendered with visible stale markers on exactly the affected findings (FR-008) | yes |

Warnings (e.g. `section_title` no longer matches the heading at that number)
go to stderr and never change the exit code.

## stdout / stderr

- stdout: a short summary line (findings count, views populated, stale count,
  output path).
- stderr: all problems, warnings, and per-stale-source messages naming the
  finding, the file, and the unresolvable path (US4 scenario 2, US5 scenario 2).
