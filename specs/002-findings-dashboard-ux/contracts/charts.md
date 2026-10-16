# Contract: Comparison Charts (inline SVG)

**Feature**: 002-findings-dashboard-ux
Supersedes the v1 chart behaviour (simple vertical bars, clipped top
label). The v1 invariants — inline SVG, no charting library, per-element
tooltips, charted values ⊆ table values — are kept and strengthened.

## When a chart exists (FR-006, SC-003)

- One chart per **headline** metric with **≥ 2** lineup models whose metric
  outcome is `value` (resolved at generation time).
- A headline metric with only one comparable model gets **no** chart
  (US3 scenario 4) — a single bar implies no comparison.
- The generator is metric-agnostic: any future headline metric with ≥ 2
  models gets a chart with no code change (a findings-layer edit marks the
  metric — spec Assumptions).
- With the current findings layer this means one chart ("decode @ 16k
  prompt", 2 models); multi-metric output is proven by the quickstart
  scenario 9 scratch layer.

## Geometry (research.md R3)

Horizontal bars, one row per model, geometry computed from the content:

```text
row_h   = 30        pad = 12        plot_w = 360 (the default width)
char_w  = 6.6       axis_h = 26
label_w = ceil(max(len(model))  * char_w) + 12
value_w = ceil(max(len(fmt(v))) * char_w) + 12
width   = pad + label_w + plot_w + value_w + pad
height  = pad + n * row_h + pad + axis_h
scale   = nice_max, the smallest of {1, 2, 2.5, 5} × 10^k  ≥  max(v)
ticks   = 0, nice_max/4, nice_max/2, 3·nice_max/4, nice_max
```

- **Value axis** (FR-005): bottom axis line with the five tick labels;
  vertical gridlines at each tick across the plot.
- **Bars**: x = pad + label_w; width = plot_w · v / nice_max; bar height
  row_h − 10, vertically centred in its row; `class="bar"`.
- **Value labels**: at the bar end + 4 px, inside the reserved value
  column — the max bar never reaches the plot edge (scale is nice_max), so
  no label can clip (SC-002).
- **Model labels**: one line, left column, right-aligned at the plot start.
- **Viewport**: `width`/`height` attributes exactly as computed; every
  `<text>` element's bounding box (estimated with the same char_w metric)
  lies inside the viewport and no two boxes overlap — asserted by
  test_charts.py parsing the SVG.

## Content per bar (FR-006, v1 FR-006 kept)

| Attribute | Contract |
|---|---|
| `tabindex` | `"0"` — every bar is keyboard-focusable |
| `data-tooltip` | byte-identical to the corresponding table row's tooltip (what / how / evidence / links) |
| `aria-label` | `"<model>: <value> <unit>"` |
| value | equals the table value for the same model + metric (SC-005's chart twin; test: charted values ⊆ table values) |

## Accessibility

- The `<svg>` keeps `role="img"` and an `aria-label` naming the metric
  (v1), and the chart sits under an `<h4>` heading naming the metric and
  unit.
- No information is hover-only: the value label is always rendered text,
  and the tooltip also shows on focus (v1 contract).

## Determinism

All geometry derives from the resolved values and fixed constants; the
emitted SVG is byte-stable for identical inputs (FR-013). No timestamps,
no randomness, no external references inside the SVG.
