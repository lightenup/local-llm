#!/usr/bin/env python3
"""CLI entry: python3 dashboard/generate.py (FR-001, contracts/cli.md).

Exit codes:
  0  all findings valid, all sources resolved            (site written)
  1  findings-layer validation failure, every problem on (site NOT written)
     stderr with file + field
  2  one or more stale sources, site written with visible
     stale markers on exactly the affected findings

Strictly read-only w.r.t. results/, profiles/, and the oMLX configuration
(FR-010): the only file written is site/index.html.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from findings import (Problem, load_findings, load_sections,  # noqa: E402
                      section_title_mismatches)
from render import ResolvedMetric, render_page  # noqa: E402
from sources import resolve  # noqa: E402


def _repo_root() -> Path:
    """The repo root is the cwd the CLI is invoked from (contracts/cli.md
    paths are all relative to it). Fall back to the script's parent's parent
    only when the cwd has no findings/ directory."""
    cwd = Path.cwd()
    if (cwd / "findings").is_dir() or (cwd / "results").is_dir():
        return cwd
    return Path(__file__).resolve().parents[1]


def main() -> int:
    root = _repo_root()
    findings_dir = root / "findings"
    results_dir = root / "results"
    findings_md = root / "docs" / "FINDINGS.md"
    site_dir = root / "site"

    findings, problems = load_findings(findings_dir)
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
    html = render_page(findings, sections, resolved, stale, generated_at)

    site_dir.mkdir(exist_ok=True)
    (site_dir / "index.html").write_text(html)

    views = []
    if any(f.status == "shipped" for f in findings):
        views.append("lineup")
    if findings:
        views.append("board")
    if any(f.status == "dead-end" for f in findings):
        views.append("dead-ends")
    print(f"{len(findings)} findings, views: {', '.join(views) or 'none'}, "
          f"stale: {stale}, output: site/index.html")
    return 2 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
