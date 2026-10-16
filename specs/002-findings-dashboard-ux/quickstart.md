# Quickstart: Findings Dashboard — Presentation & UX v2

**Feature**: 002-findings-dashboard-ux
**Purpose**: runnable validation scenarios that prove the feature works
end-to-end. Details live in [plan.md](plan.md), [data-model.md](data-model.md),
and [contracts/](contracts/) — this file only runs things.

## Prerequisites

- Python ≥ 3.11 on `python3` (repo runs 3.14; stdlib only).
- A checkout of the repo; any modern browser for `file://` viewing.
- No server, no network, no extra packages.

## 1. Run the test suite

```bash
python3 -m unittest discover -s tests -v
```

Expected: all tests pass, including the v2 additions (nav, filter/sort data
contract, SVG no-clip geometry, computed contrast ratios, JS-disabled
completeness). Constitution VII: the v2 tests existed and failed before
their implementation.

## 2. Generate and open the site

```bash
python3 dashboard/generate.py        # exit 0
open site/index.html
```

Expected: exit 0; the redesigned page shows a distinct header, a sticky bar
with the three nav links and the filter controls, and three visually
separated, headed view regions. Lineup findings are grouped under tier
headings; the board is ordered by status; every badge/marker carries a
text label and a border.

## 3. Navigation (US1, SC-001)

With the page open:

1. Scroll to the bottom; activate each nav link (pointer, then keyboard
   Tab + Enter). Expected: the corresponding view is brought into view and
   its heading shows the focus outline.
2. Repeat from the top. Expected: same single-action jumps.
3. Disable JavaScript (Safari: Develop → Disable JavaScript; Chrome:
   `chrome://flags` or DevTools → Rendering → "Emulate CSS
   'prefers-reduced-motion: reduce'" off + a JS-disabled profile), reload,
   and activate each nav link. Expected: plain anchor jumps still work.

## 4. Filter and search (US4, SC-004)

1. Model → `Qwen3.8-27B-oQ4e`: only the fast-tier finding (lineup) and its
   board row remain visible; the count reads "2 of 16 findings".
2. Add Status → `dead-end`: the oQ4e board row is hidden too; the board
   shows its "no findings match the current filters" message with a Clear
   button; the count updates.
3. Clear via the bar's button: the full page is restored in one action.
4. Search `co-residency`: the Flash-Next dead end and its board row are
   the only visible entries.
5. Search `zzz-no-match`: every view shows its no-match message; no blank
   page.
6. DevTools → Network: zero requests fired by any of the above.

## 5. Sort (US5, SC-005)

1. Board → click the `status` header: rows order shipped → on-trial →
   retired → dead-end → open (best-first for a text column is ascending);
   the header shows ↑ and `aria-sort="ascending"`; click again for ↓.
2. A lineup value column (e.g. `decode @ 16k prompt`): first click orders
   best-first per the metric's direction (higher ⇒ descending); the value
   column sorts numerically (58.8 above 9.1, never lexicographic).
3. Stale rows (create one per scenario 8): under any sort they sit last,
   in stable order.
4. Keyboard: Tab to a sort button, press Enter — same behaviour, visible
   focus.
5. Reload: tables return to their documented default order; no sort
   state persists.

## 6. JavaScript disabled — full content (FR-010, SC-007)

With JavaScript disabled (see scenario 3), verify in the rendered page:

- every metric value and unit is visible;
- every metric's what / how / evidence text and its FINDINGS/raw links are
  visible inline (the details line under each value);
- all three views, all badges/markers, and all links render; navigation
  works as anchors; tables show their default order; the filter/sort
  controls are inert, not broken.

## 7. Determinism (FR-013, SC-008)

```bash
python3 dashboard/generate.py
cp site/index.html /tmp/v2-run1.html
python3 dashboard/generate.py
diff /tmp/v2-run1.html site/index.html
```

Expected: the **only** differing line is the "generated at" stamp — the
inline CSS/JS introduce no new non-deterministic field.

## 8. v1 non-regression (FR-001, SC-008)

The v1 quickstart scenarios still hold — run at least:

- **Stale is loud**: `mv results/tp_mtp.json /tmp/` → regenerate → exit 2,
  stderr names the finding + file, exactly the affected finding carries the
  stale marker (now with text + border), the rest renders normally; the
  stale row sorts last under any sort (scenario 5.3). Restore the file →
  exit 0.
- **Validation fails loudly**: scratch findings file with
  `status = "bogus"` → exit 1, **no site written**.
- **Read-only**: `git status --porcelain results/ profiles/` identical
  before/after a run.
- **Deep links**: a tooltip link and its inline-details twin both resolve
  to the FINDINGS section and the raw JSON.

## 9. Multi-metric charts (FR-006, SC-003)

The current layer yields one chart (decode, 2 models). Prove the generator
is metric-agnostic in a scratch copy of the repo (or a scratch findings
dir): mark `context ceiling` (lineup_quality_8bit.toml) `headline = true`
and add a second model's `context ceiling` metric with a resolvable source,
then regenerate. Expected: a second comparison chart appears with a value
axis, gridlines, no clipped labels, and per-bar tooltips identical to its
table rows; the single-model `livecodebench accuracy` still gets no chart.
Discard the scratch layer afterwards.

## 10. Accessibility spot-check (FR-014, SC-006)

- Automated: `test_render_a11y.py` + `test_assets.py` assert landmarks,
  heading order, `:focus-visible`, `prefers-reduced-motion`, no
  colour-only meaning, and computed contrast ≥ 4.5:1 (text) / ≥ 3:1 (UI).
- Manual: zoom the page to 200% — no truncation or overlap; operate the
  whole page (nav → filters → search → sort → tooltips → chart bars) with
  the keyboard alone; set the OS to "reduce motion" and confirm jumps are
  instant.
