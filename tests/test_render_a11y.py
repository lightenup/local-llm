"""T005 + T011: Structural accessibility and US2 design tests.

T005: html lang, single h1, skip link, main landmark, sections, headings, badges
T011: board status grouping, no truncation, badge borders, tooltip text visible
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from assets import STYLE  # noqa: E402
from test_generate import FixturesMixin, VALID_FINDING  # noqa: E402
from test_render_board import DEAD_END_FINDING, ON_TRIAL_FINDING, RETIRED_FINDING  # noqa: E402

NO_METRIC_FINDING = """\
id      = "nometric_open"
title   = "a finding with no metrics"
status  = "open"
models  = ["TestModel"]
section = 30
section_title = "gemma-4-31B wins both measured axes"
rationale = "No measurement yet."
"""

ON_TRIAL_FINDING = """\
id      = "trial_alpha"
title   = "alpha is on trial"
status  = "on-trial"
models  = ["Alpha-Model"]
section = 30
section_title = "gemma-4-31B wins both measured axes"
rationale = "Waiting on results."

[[metrics]]
name      = "decode @ 16k prompt"
unit      = "tok/s"
direction = "higher"
tooltip_what     = "Sustained decode throughput."
tooltip_how      = "bench/throughput.py."
tooltip_evidence = "Deterministic sampling."
source_file = "tp_mtp.json"
source_path  = "summary[?pp=16384].decode_tps"
"""


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


class A11yStructureTests(FixturesMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.write_finding("lineup_quality_8bit.toml", VALID_FINDING)
        self.write_finding("trial_alpha.toml", ON_TRIAL_FINDING)
        self.write_finding("nometric_open.toml", NO_METRIC_FINDING)
        self.write_finding("deadend_some_lever.toml", DEAD_END_FINDING)
        self.html = _generate(self.findings, self.results, self.docs)

    def test_html_lang_en(self):
        self.assertIn('<html lang="en">', self.html)

    def test_exactly_one_h1(self):
        h1s = re.findall(r"<h1[^>]*>", self.html)
        self.assertEqual(len(h1s), 1, f"expected 1 h1, got {len(h1s)}")

    def test_skip_link(self):
        self.assertIn('<a class="skip" href="#main">', self.html)

    def test_main_landmark(self):
        self.assertIn('<main id="main">', self.html)

    def test_sections_aria_labelledby(self):
        for sec_id, h_id in (("lineup", "lineup-h"),
                             ("board", "board-h"),
                             ("dead-ends", "deadends-h")):
            self.assertIn(f'<section id="{sec_id}" aria-labelledby="{h_id}">',
                          self.html, f"section {sec_id} missing aria-labelledby")
            self.assertIn(f'<h2 id="{h_id}" tabindex="-1">',
                          self.html, f"h2 {h_id} missing or no tabindex=-1")

    def test_heading_levels_never_skip(self):
        headings = re.findall(r"<h([1-6])", self.html)
        prev = 0
        for h in headings:
            level = int(h)
            self.assertLessEqual(level, prev + 1,
                                 f"heading h{level} skips from h{prev}")
            prev = level

    def test_badges_carry_status_text(self):
        badges = re.findall(r'<span class="badge[^"]*">([^<]*)</span>', self.html)
        self.assertTrue(badges, "no badges found")
        for badge_text in badges:
            self.assertIn(badge_text.strip(),
                          ("shipped", "on-trial", "retired", "dead-end", "open"),
                          f"badge text {badge_text!r} is not a status word")

    def test_no_measurement_marker_text(self):
        self.assertIn("no measurement", self.html)


class US2DesignTests(FixturesMixin, unittest.TestCase):
    """T011: US2 — status grouping, no truncation, badge borders, tooltip text."""

    def setUp(self):
        super().setUp()
        self.write_finding("lineup_quality_8bit.toml", VALID_FINDING)
        self.write_finding("trial_alpha.toml", ON_TRIAL_FINDING)
        self.write_finding("closed_some_candidate.toml", RETIRED_FINDING)
        self.write_finding("deadend_some_lever.toml", DEAD_END_FINDING)
        self.html = _generate(self.findings, self.results, self.docs)

    def test_board_status_grouped_order(self):
        board = re.search(r'<section id="board"[^>]*>.*?</section>', self.html, re.S)
        self.assertIsNotNone(board)
        board_html = board.group(0)
        positions = {}
        for status in ("shipped", "on-trial", "retired", "dead-end", "open"):
            m = re.search(rf'badge-{status}"', board_html)
            if m:
                positions[status] = m.start()
        order = ["shipped", "on-trial", "retired", "dead-end", "open"]
        present = [s for s in order if s in positions]
        pos_list = [positions[s] for s in present]
        self.assertEqual(pos_list, sorted(pos_list),
                         f"board rows not in status order: {present}")

    def test_no_truncation_in_style(self):
        self.assertNotIn("text-overflow: ellipsis", STYLE)
        self.assertNotIn("overflow: hidden", STYLE)

    def test_long_model_name_renders_verbatim(self):
        long_name = "A" * 60
        body = VALID_FINDING.replace("Qwen3.8-27B-8bit", long_name)
        self.write_finding("lineup_long.toml", body.replace(
            "lineup_quality_8bit", "lineup_long"))
        html = _generate(self.findings, self.results, self.docs)
        self.assertIn(long_name, html)

    def test_badge_styles_carry_border(self):
        for status in ("shipped", "on-trial", "retired", "dead-end", "open"):
            pattern = rf"\.badge-{status}\s*\{{[^}}]*\}}"
            m = re.search(pattern, STYLE)
            self.assertIsNotNone(m, f"no CSS rule for .badge-{status}")
            self.assertIn("border", m.group(0),
                          f".badge-{status} missing border")

    def test_tooltip_text_visible_without_js(self):
        self.assertIn("Sustained decode throughput at a 16,384-token prompt.",
                      self.html)
        self.assertIn("bench/throughput.py, code_python context.", self.html)
        self.assertIn("Deterministic sampling.", self.html)


if __name__ == "__main__":
    unittest.main()
