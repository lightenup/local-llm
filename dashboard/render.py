#!/usr/bin/env python3
"""Render site/index.html and site/findings.html (FR-011).

index.html: header with the "generated at" stamp (the only non-deterministic
field, FR-012), the roles strip, and the anchored views #lineup, #candidates,
#board, #dead-ends, #benchmarks, #glossary. findings.html: docs/FINDINGS.md
rendered with the same styling, so every "FINDINGS §N" link lands on a real
anchor offline. All CSS/JS is inline; no network.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

from assets import SCRIPTS, STYLE
from charts import bar_chart_svg
from findings import Finding, models_match, section_anchor
from markdown import Rendered, inline, link_sections
from suites import GROUP_TITLES, GROUPS, RunSummary, Suite

FINDINGS_PAGE = "findings.html"
STAGE_LABELS = {"gate": "Gate", "accuracy": "Accuracy", "task": "Task fit",
                "decision": "Decision"}
STATE_GLYPH = {"pass": "✓", "fail": "✗", "partial": "◐", "none": "–"}
SUITE_ORDER = {s: i for i, s in enumerate(
    ("livecodebench", "humaneval", "mbpp", "mmlu_pro", "gsm8k", "mathqa", "mmlu",
     "arc_challenge", "hellaswag", "winogrande", "truthfulqa", "safetybench", "bbq"))}
SUITE_SHORT = {"livecodebench": "LCB", "humaneval": "HumanEval", "mbpp": "MBPP",
               "mmlu_pro": "MMLU-Pro", "gsm8k": "GSM8K", "mathqa": "MathQA", "mmlu": "MMLU",
               "arc_challenge": "ARC", "hellaswag": "HellaSwag", "winogrande": "WinoGrande",
               "truthfulqa": "TruthfulQA", "safetybench": "SafetyBench", "bbq": "BBQ"}
STATE_WORD = {"pass": "passed", "fail": "failed", "partial": "partly", "none": "not run"}


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


def _findings_link(sections, num) -> str:
    return f"{FINDINGS_PAGE}#{esc(section_anchor(sections, num) or '')}"


def section_ids(sections) -> dict[int, str]:
    return {n: v[1] for n, v in sections.items()}


class _Gloss:
    """Mark the first use of each glossary term per card with <abbr>."""

    def __init__(self, terms):
        self.terms = [(t, re.compile(rf"(?<![\w-]){re.escape(esc(t['term']))}(?![\w-])"))
                      for t in terms or []]

    def __call__(self, escaped: str, used: set) -> str:
        # Find every first use in the original text, then splice once, so a
        # term inside an earlier term's inserted title="" is never matched.
        spans = []
        for t, rx in self.terms:
            if t["term"] in used:
                continue
            m = rx.search(escaped)
            if m and not any(m.start() < e and s < m.end() for s, e, _ in spans):
                used.add(t["term"])
                spans.append((m.start(), m.end(), t))
        for s, e, t in sorted(spans, key=lambda x: x[0], reverse=True):
            escaped = (escaped[:s] + f'<abbr title="{esc(t["definition"])}">{escaped[s:e]}</abbr>'
                       + escaped[e:])
        return escaped


def _prose(text: str, sections, gloss: _Gloss | None = None, used: set | None = None) -> str:
    """Escaped plain text with glossary terms and §N linked."""
    out = esc(text)
    if gloss is not None:
        out = gloss(out, used if used is not None else set())
    return link_sections(out, section_ids(sections), FINDINGS_PAGE)


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
                  section_num: int, sections) -> str:
    """One metric's what / how / evidence (FR-010: present without JS)."""
    links = [f'<a href="{esc(findings_link)}">FINDINGS §{section_num}</a>']
    if raw_link:
        links.append(f'<a href="{esc(raw_link)}">raw results</a>')
    return (f'<p class="details"><b class="metric-name">{esc(metric.name)}</b> '
            f'<b>what</b> {_prose(metric.tooltip_what, sections)} '
            f'<b>how</b> {_prose(metric.tooltip_how, sections)} '
            f'<b>evidence</b> {_prose(metric.tooltip_evidence, sections)} '
            f'<span class="links">{" · ".join(links)}</span></p>')


def _how_measured(rows: list[str]) -> str:
    if not rows:
        return ""
    return ('<details class="how"><summary>How this was measured</summary>'
            + "".join(rows) + "</details>")


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


def _roles_for(f: Finding, roles) -> list[str]:
    return [r.role for r in roles or [] if any(models_match(r.model, m) for m in f.models)]


# ---------------------------------------------------------------- views

def _roles_strip(roles) -> str:
    if not roles:
        return ""
    out = ['<aside class="roles" aria-label="Roles at a glance">',
           '<p class="roles-intro">Clients call a <b>role</b>; LiteLLM maps it to a '
           'model and profile, so the model behind it can change without touching a '
           'client.</p>', '<ul class="role-grid">']
    for r in roles:
        ctx = f'<span class="role-ctx">{r.max_input_tokens // 1024}k context</span>' \
            if r.max_input_tokens else ""
        prof = f'<span class="role-prof">profile {esc(r.profile)}</span>' if r.profile else ""
        out.append(f'<li class="role"><span class="role-name">{esc(r.role)}</span>'
                   f'<span class="role-model">{esc(r.model)}</span>{prof}{ctx}</li>')
    out.append("</ul></aside>")
    return "".join(out)


_PIPELINE = [
    ("scout", "Scout", "⌕", "daily, automatic",
     "A new model fits this machine and its architecture can run here.",
     "Anything about speed or quality; it reads metadata only."),
    ("gate", "Gate", "⏱", "about 10 min",
     "It loads, what it really costs in memory, and that it serves a 16k prompt "
     "with the shipped lineup still loaded.",
     "Answer quality. A pass only earns the hours of the next step."),
    ("accuracy", "Accuracy suites", "▤", "hours per suite",
     "Correctness on 500 questions per suite, compared question by question "
     "(paired) with the models already in the lineup.",
     "How it does on this stack's own jobs, or whether a capped answer would "
     "have been right."),
    ("task", "Task fit", "⚒", "about 1 hour",
     "Tool calls, diagrams and code for the jobs this stack does, through the "
     "LiteLLM roles clients use.",
     "Anything beyond the tasks in the harness."),
    ("decision", "Decision", "⚑", "written up",
     "A role assignment or a retirement, recorded in FINDINGS with the evidence.",
     ""),
]


def _pipeline() -> str:
    out = ['<ol class="pipeline" id="pipeline" aria-label="How a model gets into the lineup">']
    for key, name, icon, cost, proves, cannot in _PIPELINE:
        out.append(f'<li class="step step-{key}"><span class="step-icon" aria-hidden="true">'
                   f'{icon}</span><span class="step-name">{esc(name)}</span>'
                   f'<span class="cost">{esc(cost)}</span>'
                   f'<span class="proves"><b>Shows</b> {esc(proves)}</span>'
                   + (f'<span class="cannot"><b>Cannot show</b> {esc(cannot)}</span>'
                      if cannot else "") + "</li>")
    out.append("</ol>")
    return "".join(out)


def _stepper(c) -> str:
    from candidates import STAGES
    cells = []
    for s in STAGES:
        st = c.stage(s)
        cells.append(f'<li class="dot dot-{st}" title="{STAGE_LABELS[s]}: {STATE_WORD[st]}">'
                     f'<span aria-hidden="true">{STATE_GLYPH[st]}</span>'
                     f'<span class="dot-label">{STAGE_LABELS[s]}</span>'
                     f'<span class="sr">: {STATE_WORD[st]}</span></li>')
    return f'<ol class="stepper">{"".join(cells)}</ol>'


def _candidate_notes(c, sections) -> str:
    notes = []
    for g in c.gates:
        ok = g.get("verdict", "").startswith("PASS")
        where = (f"replacing {esc(g['replaces'])}" if g.get("replaces")
                 else "beside the lineup")
        mem = f", {g['resident_gib']} GiB" if g.get("resident_gib") else ""
        notes.append(f'<li><span class="chip chip-{"pass" if ok else "fail"}">'
                     f'gate {"✓" if ok else "✗"}</span> {where}{mem} '
                     f'<a href="../results/{esc(g["file"])}">raw</a></li>')
    chips = []
    for b, run in sorted(c.suites.items(), key=lambda kv: (SUITE_ORDER.get(kv[0], 99), kv[0])):
        capped = run.truncated >= max(5, run.total // 50)
        old = run.harness == "pre-0.7.1"
        title = [f"{run.accuracy * 100:.1f}% of {run.total} questions"]
        if capped:
            title.append(f"{run.truncated} answers hit the token cap and count as wrong")
        if old:
            title.append("old harness: scored before oMLX 0.7.1rc1 re-prompted 216 problems; "
                         "compare only with runs from the same harness")
        flags = ("" if not capped else f' <span class="flag" aria-label="capped">✂{run.truncated}</span>') + \
                ("" if not old else ' <span class="flag" aria-label="old harness">old</span>')
        chips.append(f'<a class="chip score{" chip-old" if old else ""}" '
                     f'href="../results/{esc(run.file)}" title="{esc("; ".join(title))}">'
                     f'{esc(SUITE_SHORT.get(b, b))} <b>{run.accuracy * 100:.1f}</b>'
                     f'{"" if run.total == 500 else f" <small>n={run.total}</small>"}{flags}</a>')
    if chips:
        notes.append('<li class="score-row">' + "".join(chips) + "</li>")
    if c.samples and not c.suites:
        notes.append(f"<li>{c.samples} timing sample(s) only</li>")
    if c.tasks:
        notes.append(f"<li>taskbench: {len(c.tasks)} run(s)</li>")
    if c.finding is not None:
        notes.append(f'<li><a href="{_findings_link(sections, c.finding.section)}">'
                     f'FINDINGS §{c.finding.section}</a>: {esc(c.finding.title)}</li>')
    return f'<ul class="notes">{"".join(notes)}</ul>' if notes else ""


def _candidates_section(cands, sections) -> str:
    out = ['<section id="candidates" aria-labelledby="candidates-h">',
           '<h2 id="candidates-h" tabindex="-1">Candidates</h2>',
           '<h3>How a model gets into the lineup</h3>', _pipeline(),
           '<p class="lede">Each step costs more than the one before and answers a '
           'different question, so a model only moves on when the cheaper step '
           'passes. A gate pass says the model <em>can run here</em>; only the '
           'accuracy suites say whether it is <em>any good</em>.</p>',
           '<h3>Where each model stands</h3>']
    if not cands:
        out.append('<p class="empty-state">no gate or accuracy results yet</p>')
    else:
        out.append('<p class="legend" aria-hidden="true"><span class="dot-inline dot-pass">✓</span> '
                   'passed <span class="dot-inline dot-partial">◐</span> partly '
                   '<span class="dot-inline dot-fail">✗</span> failed '
                   '<span class="dot-inline dot-none">–</span> not run · scores are % '
                   'correct on 500 questions unless n= says otherwise; '
                   '<span class="flag">✂N</span> N answers hit the token cap; '
                   '<span class="flag">old</span> scored on the old livecodebench '
                   'harness. Hover a score for detail; click it for the raw result.</p>')
        out.append('<div class="table-wrap"><table class="cands"><tr><th scope="col">model</th>'
                   '<th scope="col">progress</th><th scope="col">decision</th>'
                   '<th scope="col">evidence</th></tr>')
        for c in cands:
            dec = c.decision
            badge = {"in lineup": "shipped", "open": "open", "not recorded": "retired"}.get(dec, dec)
            role = f'<br><span class="meta">as {esc(", ".join(c.roles))}</span>' if c.roles else ""
            out.append(f'<tr><th scope="row" class="model">{esc(c.model)}</th>'
                       f'<td>{_stepper(c)}</td>'
                       f'<td><span class="badge badge-{esc(badge)}">{esc(dec)}</span>{role}</td>'
                       f'<td>{_candidate_notes(c, sections)}</td></tr>')
        out.append("</table></div>")
    out.append("</section>")
    return "\n".join(out)


def _suite_card(s: Suite, summary: RunSummary | None, sections, gloss) -> str:
    cls = "suite" if s.run else "suite not-run"
    used: set = set()
    out = [f'<article class="{cls}" id="suite-{esc(s.id)}" aria-labelledby="suite-{esc(s.id)}-h">',
           f'<h4 id="suite-{esc(s.id)}-h">{esc(s.name)}</h4>',
           f'<p>{_prose(s.measures, sections, gloss, used)}</p>',
           f'<p class="meta"><b>Size</b> {esc(s.size)}</p>',
           f'<p class="meta"><b>How we run it</b> {_prose(s.how_we_run, sections, gloss, used)}</p>']
    if not s.harness and s.run:
        if summary and summary.runs:
            models = ", ".join(esc(m) for m in sorted(summary.models))
            out.append(f'<p class="meta"><b>Stored runs</b> {summary.runs}, '
                       f'{len(summary.models)} models: {models}</p>')
        else:
            out.append('<p class="meta"><b>Stored runs</b> none yet</p>')
    links = [f'<a href="{esc(ln.url)}">{esc(ln.label)}</a>' for ln in s.links]
    if s.id == "candidate_gate":
        links.append('<a href="#pipeline">where it sits in the pipeline</a>')
    for n in s.findings:
        links.append(f'<a href="{_findings_link(sections, n)}">FINDINGS §{n}</a>')
    out.append(f'<p class="links">{"".join(links)}</p>')
    out.append("</article>")
    return "".join(out)


def _benchmarks_section(suites, summaries, sections, gloss) -> str:
    out = ['<section id="benchmarks" aria-labelledby="benchmarks-h">',
           '<h2 id="benchmarks-h" tabindex="-1">Benchmarks</h2>']
    if not suites:
        out.append('<p class="empty-state">no benchmark suites described yet</p>')
    else:
        out.append('<p class="lede">What each suite measures, how this stack runs it, '
                   'and where to read more. Scores are not shown here: suites only '
                   'compare paired, on the same harness version. The per-model numbers '
                   'are under <a href="#candidates">Candidates</a>.</p>')
        for g in GROUPS:
            in_group = [s for s in suites if s.group == g]
            if not in_group:
                continue
            out.append(f'<h3 class="tier">{esc(GROUP_TITLES[g])}</h3>')
            out.append('<div class="cards">')
            out.extend(_suite_card(s, (summaries or {}).get(s.id), sections, gloss)
                       for s in in_group)
            out.append("</div>")
    out.append("</section>")
    return "\n".join(out)


def _glossary_section(terms, sections) -> str:
    out = ['<section id="glossary" aria-labelledby="glossary-h">',
           '<h2 id="glossary-h" tabindex="-1">Glossary</h2>']
    if not terms:
        out.append('<p class="empty-state">no terms yet</p>')
    else:
        out.append('<dl class="glossary">')
        for t in terms:
            ref = (f' <a href="{_findings_link(sections, t["section"])}">§{t["section"]}</a>'
                   if t.get("section") and section_anchor(sections, t["section"]) else "")
            out.append(f'<dt id="term-{esc(re.sub(r"[^a-z0-9]+", "-", t["term"].lower()))}">'
                       f'{esc(t["term"])}</dt><dd>{esc(t["definition"])}{ref}</dd>')
        out.append("</dl>")
    out.append("</section>")
    return "\n".join(out)


def _header(out: list[str], generated_at: str, title: str = "Findings — local-LLM stack",
            other: tuple[str, str] = ("running.html", "Running now")) -> None:
    out.append("<header>")
    out.append('<div class="head-row">')
    out.append(f"<h1>{esc(title)} "
               f"<span class=\"stamp\">[generated at: {esc(generated_at)}]</span></h1>")
    out.append(f'<a class="running-link" href="{other[0]}">{esc(other[1])}</a>')
    out.append("</div>")


NAV = [("#lineup", "Live lineup"), ("#candidates", "Candidates"),
       ("#board", "Candidate status board"), ("#dead-ends", "Settled dead ends"),
       ("#benchmarks", "Benchmarks"), ("#glossary", "Glossary")]


def render_page(findings: list[Finding],
                sections: dict[int, tuple[str, str]],
                resolved: dict[str, list[ResolvedMetric]],
                stale_count: int,
                generated_at: str,
                suites: list[Suite] | None = None,
                summaries: dict[str, RunSummary] | None = None,
                roles=None, candidates=None, glossary=None) -> str:
    """Render the complete page. `findings` is the full layer; views are
    populated per the data-model.md status -> view mapping."""
    gloss = _Gloss(glossary)
    out = []
    out.append("<!DOCTYPE html>")
    out.append('<html lang="en">')
    out.append("<head>")
    out.append('<meta charset="utf-8">')
    out.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
    out.append("<title>Findings — local-LLM stack</title>")
    out.append(STYLE)
    out.append("</head>")
    out.append("<body>")
    out.append('<a class="skip" href="#main">Skip to content</a>')
    out.append('<div id="tooltip"></div>')
    _header(out, generated_at)
    out.append('<div class="bar">')
    out.append('<nav aria-label="Views">')
    out.extend(f'<a href="{h}">{label}</a>' for h, label in NAV)
    out.append("</nav>")
    if findings:
        out.append(_control_bar(findings))
    out.append("</div>")
    out.append("</header>")
    out.append('<main id="main">')
    out.append(_roles_strip(roles))

    tail = [_candidates_section(candidates or [], sections),
            _benchmarks_section(suites, summaries, sections, gloss),
            _glossary_section(glossary, sections)]

    if not findings:
        out.append('<section id="lineup" aria-labelledby="lineup-h">'
                   '<h2 id="lineup-h" tabindex="-1">Live lineup</h2>'
                   '<p class="empty-state">no findings yet</p>'
                   + _no_match("lineup") + '</section>')
        out.append(tail[0])
        out.append('<section id="board" aria-labelledby="board-h">'
                   '<h2 id="board-h" tabindex="-1">Candidate status board</h2>'
                   '<p class="empty-state">no findings yet</p>'
                   + _no_match("board") + '</section>')
        out.append('<section id="dead-ends" aria-labelledby="deadends-h">'
                   '<h2 id="deadends-h" tabindex="-1">Settled dead ends</h2>'
                   '<p class="empty-state">no findings yet</p>'
                   + _no_match("dead-ends") + '</section>')
        out.extend(tail[1:])
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
                link = _findings_link(sections, f.section)
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
    out.append('<div class="cards tiers">')
    for tier in ("quality", "fast", "additive"):
        tier_findings = [f for f in shipped if (f.tier or "quality") == tier]
        if not tier_findings:
            continue
        out.append('<div class="tier-col">')
        out.append(f'<h3 class="tier">{esc(tier)} tier</h3>')
        for f in tier_findings:
            fm = resolved.get(f.id, [])
            link = _findings_link(sections, f.section)
            stale_here = any(r.outcome == "stale" for r in fm)
            out.append(f'<div class="finding"{_data_attrs(f)}>')
            out.append(f'<h4><a class="title-link" href="{link}">{esc(f.title)}</a></h4>')
            served = _roles_for(f, roles)
            if served:
                out.append(f'<p class="roleline">serves <b>{esc(", ".join(served))}</b></p>')
            elif roles:
                out.append('<p class="roleline muted">no role points at it now</p>')
            if stale_here:
                out.append('<span class="marker-stale">stale source</span>')
            if not fm:
                out.append(
                    f'<p><span class="marker-none">no measurement</span> — '
                    f'<a href="{link}">FINDINGS §{f.section}</a></p>')
            out.append("<table>")
            out.append(_lineup_header(sortable=len(fm) >= 2,
                                      direction=fm[0].metric.direction if fm else None))
            how = []
            for r in fm:
                m = r.metric
                raw = f"../results/{esc(m.source_file)}" if m.source_file else None
                tip = tooltip_html(m, link, raw, f.section,
                                   m.baseline_label if r.baseline_value is not None else None,
                                   r.baseline_value)
                if r.outcome == "value":
                    dv = f' data-value="{r.value}"'
                    raw_cell = (f'<td{dv} class="num"><a href="../results/{esc(m.source_file)}">'
                                f'{esc(r.value)}</a></td>' if m.source_file
                                else f'<td{dv} class="num">{esc(r.value)}</td>')
                    out.append(
                        f'<tr><td><span tabindex="0" data-tooltip="{esc(tip)}">{esc(m.name)}</span></td>'
                        f"{raw_cell}<td>{esc(m.unit)}</td></tr>")
                    how.append(_details_html(m, link, raw, f.section, sections))
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
            out.append(_how_measured(how))
            out.append("</div>")
        out.append("</div>")
    out.append("</div>")
    for name, unit, entries in chart_data:
        if len(entries) < 2:
            continue
        out.append(f'<figure class="chart"><figcaption>{esc(name)} ({esc(unit)})</figcaption>'
                   f'{bar_chart_svg(name, unit, "higher", entries)}</figure>')
    out.append(_no_match("lineup"))
    out.append("</section>")

    out.append(tail[0])

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
        out.append('<div class="table-wrap"><table class="board">')
        out.append(_board_header(sortable=len(board_sorted) >= 2))
        for f in board_sorted:
            link = _findings_link(sections, f.section)
            text = f.rationale or ""
            if not text and f.status == "shipped":
                served = _roles_for(f, roles)
                text = (f"In the lineup, {f.tier or 'quality'} tier"
                        + (f"; serves {', '.join(served)}." if served else "."))
            out.append(
                f'<tr class="finding"{_data_attrs(f)}>'
                f'<td><a class="title-link" href="{link}">{esc(f.title)}</a></td>'
                f'<td>{", ".join(esc(m) for m in f.models)}</td>'
                f'<td><span class="badge badge-{esc(f.status)}">{esc(f.status)}</span></td>'
                f"<td>{_rationale_cell(text, sections, gloss)}</td></tr>")
        out.append("</table></div>")
    out.append(_no_match("board"))
    out.append("</section>")

    # ---- dead ends -------------------------------------------------------
    out.append('<section id="dead-ends" aria-labelledby="deadends-h">')
    out.append('<h2 id="deadends-h" tabindex="-1">Settled dead ends</h2>')
    if not dead_ends:
        out.append('<p class="empty-state">no dead ends yet</p>')
    else:
        out.append('<div class="cards">')
        for f in dead_ends:
            link = _findings_link(sections, f.section)
            fm = resolved.get(f.id, [])
            chips, how, raws = [], [], []
            stale_here = any(r.outcome == "stale" for r in fm)
            for r in fm:
                m = r.metric
                if r.outcome != "value":
                    continue
                raw = f"../results/{esc(m.source_file)}" if m.source_file else None
                tip = tooltip_html(m, link, raw, f.section)
                chips.append(f'<span class="chip" tabindex="0" data-tooltip="{esc(tip)}">'
                             f'{esc(m.name)}: <b>{esc(r.value)}</b> {esc(m.unit)}</span>')
                if raw:
                    raws.append(f'<a href="{esc(raw)}">raw</a>')
                how.append(_details_html(m, link, raw, f.section, sections))
            out.append(f'<article class="finding dead"{_data_attrs(f)}>')
            out.append(f'<h3><a class="title-link" href="{link}">{esc(f.title)}</a></h3>')
            if stale_here:
                out.append('<p class="chips"><span class="marker-stale">stale source</span></p>')
            if chips:
                out.append('<p class="chips">closed by measurement: ' + " ".join(chips) + "</p>")
            elif not fm:
                out.append('<p class="chips"><span class="marker-none">no measurement</span> '
                           'closed by argument</p>')
            elif not stale_here:
                out.append('<p class="chips"><span class="marker-none">no measurement</span></p>')
            out.append(f'<p class="rationale">{_prose(f.rationale or "", sections, gloss, set())}</p>')
            out.append(f'<p class="links"><a href="{link}">FINDINGS §{f.section}</a>'
                       + "".join(f" · {r}" for r in raws) + "</p>")
            out.append(_how_measured(how))
            out.append("</article>")
        out.append("</div>")
    out.append(_no_match("dead-ends"))
    out.append("</section>")

    out.extend(tail[1:])
    out.append("</main>")
    out.append(SCRIPTS)
    out.append("</body></html>")
    return "\n".join(out) + "\n"


def _rationale_cell(text: str, sections, gloss) -> str:
    """First sentence up front, the rest folded, so the board scans as a table."""
    if not text:
        return ""
    m = re.match(r"(.{80,260}?\.)\s+([A-Z(].+)$", text, re.S)
    if not m:
        return f'<span class="rationale">{_prose(text, sections, gloss, set())}</span>'
    used: set = set()
    return (f'<span class="rationale">{_prose(m.group(1), sections, gloss, used)}</span>'
            f'<details class="more"><summary>more</summary>'
            f'{_prose(m.group(2), sections, gloss, used)}</details>')


# ---------------------------------------------------------------- findings page

def render_findings_page(md: Rendered, generated_at: str) -> str:
    """docs/FINDINGS.md as a page. The document's own # title becomes the h1;
    the ## sections get a sticky table of contents."""
    body = md.html
    title = "Findings"
    m = re.match(r'<h1 id="[^"]*">(.*?)</h1>\n?', body)
    if m:
        title = html.unescape(re.sub(r"<[^>]+>", "", m.group(1)))
        body = body[m.end():]
    toc = [h for h in md.headings if h.level == 2]
    out = ["<!DOCTYPE html>", '<html lang="en">', "<head>", '<meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width, initial-scale=1">',
           f"<title>{esc(title)}</title>", STYLE, "</head>", '<body class="docpage">',
           '<a class="skip" href="#main">Skip to content</a>']
    _header(out, generated_at, title, ("index.html", "Dashboard"))
    out.append("</header>")
    out.append('<div class="doc-layout">')
    out.append('<nav class="toc" aria-label="Sections"><p class="toc-h">Sections</p><ol>')
    for h in toc:
        label = html.unescape(re.sub(r"<[^>]+>", "", inline(h.text)))
        out.append(f'<li><a href="#{h.id}">{esc(label)}</a></li>')
    out.append("</ol></nav>")
    out.append(f'<main id="main" class="doc">{body}</main>')
    out.append("</div>")
    out.append("</body></html>")
    return "\n".join(out) + "\n"
