#!/usr/bin/env python3
"""Render the single self-contained site/index.html (FR-011).

One HTML document: header with the "generated at" stamp (the only
non-deterministic field, FR-012), and the three anchored views
(#lineup, #board, #dead-ends). All CSS/JS is inline; no network.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from pathlib import Path

from assets import SCRIPTS, STYLE
from charts import bar_chart_svg
from findings import Finding, section_anchor


@dataclass(frozen=True)
class ResolvedMetric:
    metric: object  # findings.Metric
    outcome: str  # "value" | "stale" | "none"
    value: object = None
    reason: str | None = None
    baseline_value: object = None
    baseline_stale: bool = False


def esc(s: object) -> str:
    return html.escape(str(s), quote=True)


def tooltip_html(metric, findings_link: str, raw_link: str | None,
                 section_num: int,
                 baseline_label: str | None = None,
                 baseline_value: object = None) -> str:
    """Tooltip content: what / how / evidence / links, in that order (contracts/ui.md)."""
    parts = [
        f'<div class="tt-title">{esc(metric.name)}</div>',
        f'<div><b>what</b> — {esc(metric.tooltip_what)}</div>',
        f'<div><b>how</b> — {esc(metric.tooltip_how)}</div>',
        f'<div><b>evidence</b> — {esc(metric.tooltip_evidence)}</div>',
    ]
    links = [f'<a href="{esc(findings_link)}">FINDINGS §{section_num}</a>']
    if raw_link:
        links.append(f'<a href="{esc(raw_link)}">raw results</a>')
    parts.append('<div><b>links</b> — ' + ' · '.join(links) + '</div>')
    if baseline_label is not None and baseline_value is not None:
        parts.append(f'<div><b>baseline</b> ({esc(baseline_label)}) — {esc(baseline_value)}</div>')
    return "".join(parts)


def _details_html(metric, findings_link: str, raw_link: str | None,
                  section_num: int) -> str:
    """Always-visible tooltip details line (FR-010, research.md R7)."""
    parts = [
        f'<b>what</b> {esc(metric.tooltip_what)} ',
        f'<b>how</b> {esc(metric.tooltip_how)} ',
        f'<b>evidence</b> {esc(metric.tooltip_evidence)} ',
    ]
    links = [f'<a href="{esc(findings_link)}">FINDINGS §{section_num}</a>']
    if raw_link:
        links.append(f'<a href="{esc(raw_link)}">raw results</a>')
    parts.append(' <b>links</b> ' + ' · '.join(links))
    return f'<div class="details">{"".join(parts)}</div>'


def _data_attrs(f) -> str:
    models = " ".join(f.models)
    tier = f.tier or ""
    return (f' data-model="{esc(models)}" data-tier="{esc(tier)}" '
            f'data-status="{esc(f.status)}" data-title="{esc(f.title)}" '
            f'data-rationale="{esc(f.rationale or "")}"')


def _control_bar(findings: list[Finding]) -> str:
    all_models = sorted({m for f in findings for m in f.models})
    model_opts = '<option value="all">all</option>' + "".join(
        f'<option value="{esc(m)}">{esc(m)}</option>' for m in all_models)
    tier_opts = ('<option value="all">all</option>'
                 '<option value="quality">quality</option>'
                 '<option value="fast">fast</option>'
                 '<option value="additive">additive</option>')
    status_opts = ('<option value="all">all</option>'
                   '<option value="shipped">shipped</option>'
                   '<option value="on-trial">on-trial</option>'
                   '<option value="retired">retired</option>'
                   '<option value="dead-end">dead-end</option>'
                   '<option value="open">open</option>')
    total = len(findings)
    return (
        '<form class="controls" role="search">\n'
        f'<label>Model <select id="f-model">{model_opts}</select></label>\n'
        f'<label>Tier <select id="f-tier">{tier_opts}</select></label>\n'
        f'<label>Status <select id="f-status">{status_opts}</select></label>\n'
        '<label>Search <input type="search" id="f-text"></label>\n'
        '<button id="f-clear" type="button">Clear filters</button>\n'
        f'<p id="f-count" role="status">{total} of {total} findings</p>\n'
        "</form>")


def _no_match(view_id: str) -> str:
    return (f'<p class="no-match" id="no-match-{view_id}" hidden>'
            f'no findings match the current filters '
            f'<button type="button" class="no-match-clear">Clear filters</button></p>')


def _sortable_th(label: str, data_sort: str | None = None) -> str:
    attrs = 'scope="col" aria-sort="none"'
    if data_sort:
        attrs += f' data-sort="{data_sort}"'
    return f'<th {attrs}><button>{esc(label)}</button></th>'


def _lineup_header(sortable: bool, direction: str | None = None) -> str:
    if not sortable:
        return "<tr><th>metric</th><th>value</th><th>unit</th></tr>"
    return (f"<tr>{_sortable_th('metric')}"
            f'{_sortable_th("value", direction)}'
            f"{_sortable_th('unit')}</tr>")


def _board_header(sortable: bool) -> str:
    if not sortable:
        return "<tr><th>title</th><th>model(s)</th><th>status</th><th>rationale</th></tr>"
    return (f"<tr>{_sortable_th('title')}"
            f"{_sortable_th('model(s)')}"
            f"{_sortable_th('status')}"
            f"{_sortable_th('rationale')}</tr>")


def render_page(findings: list[Finding],
                sections: dict[int, tuple[str, str]],
                resolved: dict[str, list[ResolvedMetric]],
                stale_count: int,
                generated_at: str) -> str:
    """Render the complete page. `findings` is the full layer; views are
    populated per the data-model.md status -> view mapping."""
    out = []
    out.append("<!DOCTYPE html>")
    out.append('<html lang="en">')
    out.append("<head>")
    out.append('<meta charset="utf-8">')
    out.append("<title>Findings — local-LLM stack</title>")
    out.append(STYLE)
    out.append("</head>")
    out.append("<body>")
    out.append('<a class="skip" href="#main">Skip to content</a>')
    out.append('<div id="tooltip"></div>')
    out.append("<header>")
    out.append(f"<h1>Findings — local-LLM stack "
               f"<span class=\"stamp\">[generated at: {esc(generated_at)}]</span></h1>")
    out.append('<div class="bar">')
    out.append('<nav aria-label="Views">')
    out.append('<a href="#lineup">Live lineup</a>')
    out.append('<a href="#board">Candidate status board</a>')
    out.append('<a href="#dead-ends">Settled dead ends</a>')
    out.append("</nav>")
    if findings:
        out.append(_control_bar(findings))
    out.append("</div>")
    out.append("</header>")
    out.append('<main id="main">')

    if not findings:
        out.append('<section id="lineup" aria-labelledby="lineup-h">'
                   '<h2 id="lineup-h" tabindex="-1">Live lineup</h2>'
                   '<p class="empty-state">no findings yet</p>'
                   + _no_match("lineup") + '</section>')
        out.append('<section id="board" aria-labelledby="board-h">'
                   '<h2 id="board-h" tabindex="-1">Candidate status board</h2>'
                   '<p class="empty-state">no findings yet</p>'
                   + _no_match("board") + '</section>')
        out.append('<section id="dead-ends" aria-labelledby="deadends-h">'
                   '<h2 id="deadends-h" tabindex="-1">Settled dead ends</h2>'
                   '<p class="empty-state">no findings yet</p>'
                   + _no_match("dead-ends") + '</section>')
        out.append("</main>")
        out.append(SCRIPTS)
        out.append("</body></html>")
        return "\n".join(out) + "\n"

    shipped = [f for f in findings if f.status == "shipped"]
    board = [f for f in findings if f.status in ("shipped", "on-trial", "retired", "dead-end", "open")]
    dead_ends = [f for f in findings if f.status == "dead-end"]

    # ---- lineup ----------------------------------------------------------
    out.append('<section id="lineup" aria-labelledby="lineup-h">')
    out.append('<h2 id="lineup-h" tabindex="-1">Live lineup</h2>')
    if not shipped:
        out.append('<p class="empty-state">no shipped findings yet</p>')
    # precompute resolved headlines for the comparison charts:
    chart_data: list[tuple[str, str, list[tuple[str, object, str]]]] = []
    seen_chart_names: set[str] = set()
    for tier in ("quality", "fast", "additive"):
        for f in sorted((f for f in shipped if (f.tier or "quality") == tier),
                        key=lambda f: f.id):
            for r in resolved.get(f.id, []):
                m = r.metric
                if not m.headline or r.outcome != "value":
                    continue
                model = f.models[0] if f.models else f.id
                link = f"docs/FINDINGS.md#{esc(section_anchor(sections, f.section) or '')}"
                raw = f"../results/{esc(m.source_file)}" if m.source_file else None
                tip = tooltip_html(m, link, raw, f.section,
                                   m.baseline_label if r.baseline_value is not None else None,
                                   r.baseline_value)
                if m.name not in seen_chart_names:
                    seen_chart_names.add(m.name)
                    chart_data.append([m.name, m.unit, []])
                for entry in chart_data:
                    if entry[0] == m.name:
                        entry[2].append((model, r.value, tip))
    for tier in ("quality", "fast", "additive"):
        tier_findings = [f for f in shipped if (f.tier or "quality") == tier]
        if not tier_findings:
            continue
        out.append(f'<h3 class="tier">{esc(tier)} tier</h3>')
        for f in tier_findings:
            fm = resolved.get(f.id, [])
            stale_here = any(r.outcome == "stale" for r in fm)
            out.append(f'<div class="finding"{_data_attrs(f)}>')
            out.append(f"<h4>{esc(f.title)}</h4>")
            if stale_here:
                out.append('<span class="marker-stale">stale source</span>')
            if not fm:
                link = f"docs/FINDINGS.md#{esc(section_anchor(sections, f.section) or '')}"
                out.append(
                    f'<p><span class="marker-none">no measurement</span> — '
                    f'<a href="{esc(link)}">FINDINGS §{f.section}</a></p>')
            out.append("<table>")
            out.append(_lineup_header(sortable=len(fm) >= 2,
                                      direction=fm[0].metric.direction if fm else None))
            for r in fm:
                m = r.metric
                link = f"docs/FINDINGS.md#{esc(section_anchor(sections, f.section) or '')}"
                raw = f"../results/{esc(m.source_file)}" if m.source_file else None
                tip = tooltip_html(m, link, raw, f.section,
                                   m.baseline_label if r.baseline_value is not None else None,
                                   r.baseline_value)
                if r.outcome == "value":
                    dv = f' data-value="{r.value}"'
                    raw_cell = (f'<td{dv}><a href="../results/{esc(m.source_file)}">'
                                f'{esc(r.value)}</a></td>' if m.source_file
                                else f'<td{dv}>{esc(r.value)}</td>')
                    out.append(
                        f'<tr><td><span tabindex="0" data-tooltip="{esc(tip)}">{esc(m.name)}</span></td>'
                        f"{raw_cell}<td>{esc(m.unit)}</td></tr>")
                    out.append(_details_html(m, link, raw, f.section))
                elif r.outcome == "stale":
                    out.append(
                        f'<tr><td><span tabindex="0" data-tooltip="{esc(tip)}">{esc(m.name)} '
                        f'<span class="marker-stale">stale</span></span></td>'
                        f'<td>—</td><td>{esc(m.unit)}</td></tr>'
                        f'<tr><td colspan="3"><code class="stale-reason">{esc(r.reason or "")}</code></td></tr>')
                else:
                    out.append(
                        f'<tr><td>{esc(m.name)} <span class="marker-none">no measurement</span></td>'
                        f'<td>—</td><td>{esc(m.unit)}</td></tr>')
            out.append("</table>")
            out.append("</div>")
    for name, unit, entries in chart_data:
        if len(entries) < 2:
            continue
        out.append(f"<h4>{esc(name)} ({esc(unit)})</h4>")
        out.append(bar_chart_svg(name, unit, "higher", entries))
    out.append(_no_match("lineup"))
    out.append("</section>")

    # ---- board -----------------------------------------------------------
    out.append('<section id="board" aria-labelledby="board-h">')
    out.append('<h2 id="board-h" tabindex="-1">Candidate status board</h2>')
    if not board:
        out.append('<p class="empty-state">no findings yet</p>')
    else:
        status_order = {"shipped": 0, "on-trial": 1, "retired": 2,
                        "dead-end": 3, "open": 4}
        board_sorted = sorted(board, key=lambda f: (status_order.get(f.status, 9),
                                                    f.title.lower()))
        out.append("<table>")
        out.append(_board_header(sortable=len(board_sorted) >= 2))
        for f in board_sorted:
            link = f"docs/FINDINGS.md#{esc(section_anchor(sections, f.section) or '')}"
            rationale = f'<a href="{esc(link)}">{esc(f.rationale or "")}</a>' if f.rationale else ""
            models = ", ".join(esc(m) for m in f.models)
            out.append(
                f'<tr class="finding"{_data_attrs(f)}>'
                f'<td>{esc(f.title)}</td><td>{models}</td>'
                f'<td><span class="badge badge-{esc(f.status)}">{esc(f.status)}</span></td>'
                f"<td>{rationale}</td></tr>")
        out.append("</table>")
    out.append(_no_match("board"))
    out.append("</section>")

    # ---- dead ends -------------------------------------------------------
    out.append('<section id="dead-ends" aria-labelledby="deadends-h">')
    out.append('<h2 id="deadends-h" tabindex="-1">Settled dead ends</h2>')
    if not dead_ends:
        out.append('<p class="empty-state">no dead ends yet</p>')
    else:
        for f in dead_ends:
            link = f"docs/FINDINGS.md#{esc(section_anchor(sections, f.section) or '')}"
            fm = resolved.get(f.id, [])
            measurements = []
            for r in fm:
                m = r.metric
                if r.outcome != "value":
                    continue
                raw = f"../results/{esc(m.source_file)}" if m.source_file else None
                tip = tooltip_html(m, link, raw, f.section)
                measurements.append(
                    f'<span tabindex="0" data-tooltip="{esc(tip)}">'
                    f'{esc(m.name)}: {esc(r.value)} {esc(m.unit)}</span>'
                    + (f' <a href="{esc(raw)}">raw</a>' if raw else ""))
                out.append(_details_html(m, link, raw, f.section))
            if measurements:
                out.append(
                    f'<p class="finding"{_data_attrs(f)}>'
                    f'<b>{esc(f.title)}</b> — closed by measurement: '
                    + "; ".join(measurements)
                    + f' · <a href="{esc(link)}">{esc(f.rationale or "")}</a></p>')
            else:
                out.append(
                    f'<p class="finding"{_data_attrs(f)}>'
                    f'<b>{esc(f.title)}</b> '
                    '<span class="marker-none">no measurement</span> — '
                    f'<a href="{esc(link)}">{esc(f.rationale or "")}</a></p>')
    out.append(_no_match("dead-ends"))
    out.append("</section>")

    out.append("</main>")
    out.append(SCRIPTS)
    out.append("</body></html>")
    return "\n".join(out) + "\n"



