# Quickstart: Findings Dashboard

**Feature**: 001-findings-dashboard
**Purpose**: runnable validation scenarios that prove the feature works
end-to-end. Details live in [plan.md](plan.md), [data-model.md](data-model.md),
and [contracts/](contracts/) — this file only runs things.

## Prerequisites

- Python ≥ 3.11 on `python3` (repo runs 3.14; `tomllib` is stdlib).
- A checkout of the repo. No server, no network, no extra packages.

## 1. Run the test suite

```bash
python3 -m unittest discover -s tests -v
```

Expected: all tests pass. (Constitution VII: tests exist before the
implementation they cover.)

## 2. Generate and open the site

```bash
python3 dashboard/generate.py        # exit 0
open site/index.html                 # or just open the file in a browser
```

Expected: exit 0; `site/index.html` written; all three views populated
(lineup, board, dead ends) from the seed findings (FR-013).

Walk one metric end-to-end (SC-001, US1): hover **and** keyboard-focus a
lineup metric → tooltip shows what / how / evidence → its FINDINGS.md link
opens the right section → its raw-results link opens the JSON carrying the
number.

## 3. Determinism (FR-012, Q1)

```bash
python3 dashboard/generate.py
cp site/index.html /tmp/run1.html
python3 dashboard/generate.py
diff /tmp/run1.html site/index.html
```

Expected: the **only** differing line is the "generated at" stamp.

## 4. Stale source is loud, never silent (FR-008, US5)

```bash
mv results/tp_baseline.json /tmp/
python3 dashboard/generate.py; echo "exit=$?"   # expect exit=2, stderr names the finding + file
# open site/index.html → exactly the affected findings carry a stale marker;
# the rest of the site renders normally
mv /tmp/tp_baseline.json results/
python3 dashboard/generate.py; echo "exit=$?"   # expect exit=0, no stale markers
```

Same treatment for schema drift: point a scratch copy of a finding at a
nonexistent `source_path` → exit 2, message names the path.

## 5. Validation failure fails loudly (FR-007, US4)

In a scratch copy of a findings file: set `status = "bogus"`.

```bash
python3 dashboard/generate.py; echo "exit=$?"   # expect exit=1
```

Expected: exit 1, **no site written** (previous `site/index.html` untouched),
stderr lists every problem with file + field.

## 6. Read-only guarantee (FR-010, US4 scenario 4)

```bash
git status --porcelain results/ profiles/ > /tmp/before
python3 dashboard/generate.py
git status --porcelain results/ profiles/ > /tmp/after
diff /tmp/before /tmp/after                    # expect: empty
```

## 7. One-file update flow (US4, SC-003)

Add `findings/<new-id>.toml` referencing an existing results file (schema in
[contracts/findings-schema.md](contracts/findings-schema.md)), then:

```bash
python3 dashboard/generate.py                  # exit 0
```

Expected: the finding appears in the view matching its status, value resolved
from the source, tooltip intact — with **no generator code change**. Delete
the file and regenerate: it disappears.

## 8. Seed coverage (FR-013)

In the rendered site, verify the v1 seed is present:

- Lineup: Qwen3.8-27B-8bit with its four profiles (code / agent / reason /
  longctx), the oQ4e fast tier, gemma-4-31B.
- Board: Coder-Next, Qwen3.8-Flash-Next, OptiQ, the qwen3_5_moe 35B-A3B
  class — each with a status and a rationale naming its deciding measurement.
- Dead ends: the settled list from backlog §H, each with its closing
  measurement or a "no measurement" argument.
