#!/usr/bin/env python3
"""CLI entry: python3 dashboard/generate.py (FR-001, contracts/cli.md).

Exit codes:
  0  all findings valid, all sources resolved            (site written)
  1  findings-layer validation failure, every problem on (site NOT written)
     stderr with file + field
  2  one or more stale sources, site written with visible
     stale markers on exactly the affected findings

Strictly read-only w.r.t. results/, profiles/, and the oMLX configuration
(FR-010): the only files written are site/index.html and site/findings.html
(docs/FINDINGS.md rendered, so every FINDINGS link has a real target).
Benchmark suite cards come from findings/suites/*.toml, the glossary from
findings/glossary/glossary.toml, the roles strip from proxy/litellm-config.yaml and the
candidates view from results/ (gate, acc and task files).
"""

from __future__ import annotations

import sys
import tomllib
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from findings import (Problem, load_findings, load_sections,  # noqa: E402
                      section_title_mismatches)
from render import ResolvedMetric, render_page  # noqa: E402
from sources import resolve  # noqa: E402
from suites import load_suites, run_summaries  # noqa: E402
from candidates import build as build_candidates  # noqa: E402
from markdown import render_markdown  # noqa: E402
from render import render_findings_page  # noqa: E402
from roles import load_roles  # noqa: E402


def load_glossary(path: Path) -> tuple[list[dict], list[Problem]]:
    if not path.is_file():
        return [], []
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as e:
        return [], [Problem(path.name, "", f"TOML parse error at line {e.lineno}: {e.msg}")]
    terms, problems = [], []
    for i, t in enumerate(data.get("term", [])):
        if not t.get("term") or not t.get("definition"):
            problems.append(Problem(path.name, f"term[{i}]", "needs term and definition"))
            continue
        terms.append(t)
    return terms, problems


def _repo_root() -> Path:
    """The repo root is the cwd the CLI is invoked from (contracts/cli.md
    paths are all relative to it). Fall back to the script's parent's parent
    only when the cwd has no findings/ directory."""
    cwd = Path.cwd()
    if (cwd / "findings").is_dir() or (cwd / "results").is_dir():
        return cwd
    return Path(__file__).resolve().parents[1]


def _doc_link(url: str) -> str:
    """Links inside FINDINGS.md are relative to docs/; the page is in site/."""
    if url.startswith(("http://", "https://", "#", "mailto:")):
        return url
    return "../docs/" + url


def main() -> int:
    root = _repo_root()
    findings_dir = root / "findings"
    results_dir = root / "results"
    findings_md = root / "docs" / "FINDINGS.md"
    site_dir = root / "site"

    findings, problems = load_findings(findings_dir)
    suites, suite_problems = load_suites(findings_dir / "suites")
    glossary, glossary_problems = load_glossary(findings_dir / "glossary" / "glossary.toml")
    problems += suite_problems + glossary_problems
    if problems:
        for p in problems:
            print(str(p), file=sys.stderr)
        return 1

    sections = load_sections(findings_md)
    for w in section_title_mismatches(findings, sections):
        print(f"warning: {w}", file=sys.stderr)

    stale = 0
    resolved: dict[str, list[ResolvedMetric]] = {}
    for f in findings:
        rm = []
        for m in f.metrics:
            if m.source_file is None:
                rm.append(ResolvedMetric(m, "none"))
                continue
            r = resolve(results_dir / m.source_file, m.source_path)
            baseline_value = None
            baseline_stale = False
            if m.baseline_source_file is not None:
                br = resolve(results_dir / m.baseline_source_file, m.baseline_source_path)
                if br.outcome == "value":
                    baseline_value = br.value
                else:
                    baseline_stale = True
                    stale += 1
            if r.outcome == "value":
                rm.append(ResolvedMetric(m, "value", r.value, None, baseline_value,
                                         baseline_stale))
            else:
                stale += 1
                rm.append(ResolvedMetric(m, "stale", None, r.reason,
                                         baseline_value, baseline_stale))
                print(f"stale: {f.id}: {m.name}: {r.reason}", file=sys.stderr)
        resolved[f.id] = rm

    generated_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    roles = load_roles(root / "proxy" / "litellm-config.yaml")
    if not roles and (root / "proxy" / "litellm-config.yaml").is_file():
        print("warning: no role aliases found in proxy/litellm-config.yaml", file=sys.stderr)
    html = render_page(findings, sections, resolved, stale, generated_at,
                       suites, run_summaries(results_dir), roles,
                       build_candidates(results_dir, findings, roles), glossary)

    site_dir.mkdir(exist_ok=True)
    (site_dir / "index.html").write_text(html)
    if findings_md.is_file():
        md = render_markdown(findings_md.read_text(),
                             {n: v[1] for n, v in sections.items()}, "",
                             _doc_link)
        (site_dir / "findings.html").write_text(render_findings_page(md, generated_at))

    views = []
    if any(f.status == "shipped" for f in findings):
        views.append("lineup")
    if findings:
        views.append("board")
    if any(f.status == "dead-end" for f in findings):
        views.append("dead-ends")
    if suites:
        views.append("benchmarks")
    print(f"{len(findings)} findings, views: {', '.join(views) or 'none'}, "
          f"stale: {stale}, output: site/index.html")
    return 2 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
