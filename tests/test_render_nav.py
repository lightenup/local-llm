"""T008: Navigation tests for the v2 sticky bar (US1).

- nav[aria-label="Views"] exists inside .bar
- Exactly three links with correct hrefs and labels
- .bar has position: sticky; top: 0 in STYLE
- Each target h2 has tabindex="-1"
- Nav renders for empty findings layer
- SCRIPTS contains nav click handler with scrollIntoView + focus()
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from assets import SCRIPTS, STYLE  # noqa: E402
from test_generate import FixturesMixin, VALID_FINDING  # noqa: E402


def _generate(findings_dir, results_dir, docs_dir):
    import findings as findings_mod
    import render
    from sources import resolve

    findings, problems = findings_mod.load_findings(findings_dir)
    assert problems == [], f"validation problems: {problems}"
    sections = findings_mod.load_sections(docs_dir / "FINDINGS.md")
    resolved = {}
    for f in findings:
        out = []
        for m in f.metrics:
            if m.source_file is None:
                out.append(render.ResolvedMetric(m, "none"))
                continue
            r = resolve(results_dir / m.source_file, m.source_path)
            out.append(render.ResolvedMetric(m, r.outcome, r.value, r.reason))
        resolved[f.id] = out
    return render.render_page(findings, sections, resolved, 0,
                              "2026-01-01 00:00:00 UTC")


class NavTests(FixturesMixin, unittest.TestCase):
    def test_nav_exists_in_bar(self):
        self.write_finding("lineup_quality_8bit.toml", VALID_FINDING)
        html = _generate(self.findings, self.results, self.docs)
        self.assertIn('<nav aria-label="Views">', html)
        bar_match = re.search(r'<div class="bar">.*?</div>', html, re.S)
        self.assertIsNotNone(bar_match)
        self.assertIn('<nav aria-label="Views">', bar_match.group(0))

    def test_exactly_three_links_with_correct_hrefs(self):
        self.write_finding("lineup_quality_8bit.toml", VALID_FINDING)
        html = _generate(self.findings, self.results, self.docs)
        nav = re.search(r'<nav aria-label="Views">(.*?)</nav>', html, re.S)
        self.assertIsNotNone(nav)
        links = re.findall(r'<a href="([^"]*)"[^>]*>([^<]*)</a>', nav.group(1))
        self.assertEqual(len(links), 3, f"expected 3 links, got {len(links)}")
        expected = [
            ("#lineup", "Live lineup"),
            ("#board", "Candidate status board"),
            ("#dead-ends", "Settled dead ends"),
        ]
        for href, label in expected:
            self.assertIn((href, label), links,
                          f"missing nav link ({href}, {label!r})")

    def test_bar_is_sticky(self):
        self.assertIn("position: sticky", STYLE)
        self.assertIn("top: 0", STYLE)

    def test_target_headings_have_tabindex(self):
        self.write_finding("lineup_quality_8bit.toml", VALID_FINDING)
        html = _generate(self.findings, self.results, self.docs)
        for h_id in ("lineup-h", "board-h", "deadends-h"):
            self.assertIn(f'<h2 id="{h_id}" tabindex="-1">', html)

    def test_nav_renders_for_empty_findings(self):
        html = _generate(self.findings, self.results, self.docs)
        self.assertIn('<nav aria-label="Views">', html)
        self.assertIn('href="#lineup"', html)
        self.assertIn('href="#board"', html)
        self.assertIn('href="#dead-ends"', html)

    def test_scripts_contain_nav_handler(self):
        self.assertIn("scrollIntoView", SCRIPTS)
        self.assertIn(".focus()", SCRIPTS)


if __name__ == "__main__":
    unittest.main()
