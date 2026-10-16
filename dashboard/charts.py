"""Inline-SVG comparison charts for the v2 findings dashboard.

Emits horizontal bar charts with a value axis, gridlines, and content-
measured no-clip geometry (contracts/charts.md, research.md R3).
"""

from __future__ import annotations

import html
import math

ROW_H = 30
PAD = 12
PLOT_W = 360
CHAR_W = 6.6
AXIS_H = 26


def _nice_max(v: float) -> float:
    if v <= 0:
        return 1.0
    k = math.floor(math.log10(v))
    for base in (1, 2, 2.5, 5):
        candidate = base * (10 ** k)
        if candidate >= v:
            return candidate
    return 10 ** (k + 1)


def _fmt(v: float) -> str:
    return str(v)


def bar_chart_svg(name: str, unit: str, direction: str,
                  entries: list[tuple[str, float, str]]) -> str:
    """Render a horizontal bar chart as an inline SVG string.

    Args:
        name: metric name (used in aria-label).
        unit: metric unit.
        direction: "higher" or "lower" (unused in geometry, kept for aria-label).
        entries: list of (model, value, tooltip) tuples.

    Returns:
        A complete <svg> element string.
    """
    n = len(entries)
    models = [e[0] for e in entries]
    values = [e[1] for e in entries]
    max_v = max(values) if values else 0.0
    nice = _nice_max(max_v)

    label_w = math.ceil(max(len(m) for m in models) * CHAR_W) + 12
    value_w = math.ceil(max(len(_fmt(v)) for v in values) * CHAR_W) + 12
    width = PAD + label_w + PLOT_W + value_w + PAD
    height = PAD + n * ROW_H + PAD + AXIS_H

    plot_x = PAD + label_w
    plot_bottom = PAD + n * ROW_H

    ticks = [0, nice / 4, nice / 2, 3 * nice / 4, nice]

    parts: list[str] = []
    parts.append(
        f'<svg width="{width}" height="{height}" '
        f'role="img" aria-label="{html.escape(name)}">'
    )

    for tv in ticks:
        tx = plot_x + PLOT_W * tv / nice
        parts.append(
            f'<line x1="{tx:.1f}" y1="{PAD}" '
            f'x2="{tx:.1f}" y2="{plot_bottom}" '
            f'stroke="#e0e0e0" stroke-width="1"/>'
        )

    parts.append(
        f'<line x1="{plot_x}" y1="{plot_bottom}" '
        f'x2="{plot_x + PLOT_W}" y2="{plot_bottom}" '
        f'stroke="#999" stroke-width="1"/>'
    )

    for tv in ticks:
        tx = plot_x + PLOT_W * tv / nice
        parts.append(
            f'<text x="{tx:.1f}" y="{plot_bottom + 16}" '
            f'text-anchor="middle" font-size="11" fill="#595959">'
            f'{tv}</text>'
        )

    for i, (model, value, tip) in enumerate(entries):
        row_y = PAD + i * ROW_H
        bar_y = row_y + 5
        bar_h = ROW_H - 10
        bar_w = PLOT_W * value / nice

        parts.append(
            f'<text x="{plot_x - 8}" y="{row_y + ROW_H / 2 + 4:.1f}" '
            f'text-anchor="end" font-size="11" fill="#1a1a1a">'
            f'{html.escape(model)}</text>'
        )

        parts.append(
            f'<rect class="bar" x="{plot_x}" y="{bar_y}" '
            f'width="{bar_w:.1f}" height="{bar_h}" '
            f'tabindex="0" data-tooltip="{html.escape(tip, quote=True)}" '
            f'aria-label="{html.escape(f"{model}: {value} {unit}")}" '
            f'fill="#0b5ed7"/>'
        )

        parts.append(
            f'<text x="{plot_x + bar_w + 4:.1f}" y="{row_y + ROW_H / 2 + 4:.1f}" '
            f'text-anchor="start" font-size="11" fill="#1a1a1a">'
            f'{_fmt(value)}</text>'
        )

    parts.append("</svg>")
    return "".join(parts)
