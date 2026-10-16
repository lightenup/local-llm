import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))

from test_generate import FixturesMixin, VALID_FINDING  # noqa: E402
from test_render_board import DEAD_END_FINDING, RETIRED_FINDING  # noqa: E402

DEAD_END_WITH_METRIC = """\
id      = "deadend_with_metric"
title   = "a lever closed by measurement"
status  = "dead-end"
models  = ["Qwen3.8-27B-8bit"]
section = 30
section_title = "gemma-4-31B wins both measured axes"
rationale = "Measured worse on every number at a 20 GB cost."

[[metrics]]
name      = "repeat median latency"
unit      = "s"
direction = "lower"
tooltip_what     = "Median latency of a repeated prompt with the lever on."
tooltip_how      = "bench A/B, 5 repeats per arm."
tooltip_evidence = "3.96 s with the lever on vs 3.49 s off; see §8."
source_file = "tp_mtp.json"
source_path  = "summary[?pp=16384].decode_tps"
"""


def _dead_ends(html: str) -> str:
    m = re.search(r'<section id="dead-ends"[^>]*>.*?</section>', html, re.S)
    assert m, "no #dead-ends section"
    return m.group(0)


class DeadEndsTests(FixturesMixin, unittest.TestCase):
    def _generate(self):
        import findings as findings_mod
        import render
        from sources import resolve

        findings, problems = findings_mod_load = findings_mod.load_findings(self.findings)
        self.assertEqual(problems, [])
        sections = findings_mod.load_sections(self.docs / "FINDINGS.md")
        resolved = {}
        for f in findings:
            out = []
            for m in f.metrics:
                if m.source_file is None:
                    out.append(render.ResolvedMetric(m, "none"))
                    continue
                r = resolve(self.results / m.source_file, m.source_path)
                out.append(render.ResolvedMetric(m, r.outcome, r.value, r.reason))
            resolved[f.id] = out
        return render.render_page(findings, sections, resolved, 0, "2026-01-01 00:00:00 UTC")

    def test_lists_exactly_the_dead_end_findings(self):
        self.write_finding("deadend_some_lever.toml", DEAD_END_FINDING)
        self.write_finding("closed_some_candidate.toml", RETIRED_FINDING)
        self.write_finding("lineup_quality_8bit.toml", VALID_FINDING)
        html = self._generate()
        dead = _dead_ends(html)
        self.assertIn("some lever is a dead end", dead)
        self.assertNotIn("some candidate is retired", dead)
        self.assertNotIn("Qwen3.8-27B-8bit ships the quality tier", dead)

    def test_entry_shows_closing_measurement_with_source_link(self):
        self.write_finding("deadend_with_metric.toml", DEAD_END_WITH_METRIC)
        html = self._generate()
        dead = _dead_ends(html)
        entry = re.search(r'<p[^>]*><b>a lever closed by measurement</b>.*?</p>', dead, re.S)
        self.assertIsNotNone(entry)
        text = entry.group(0)
        self.assertIn("34.0", text)  # the resolved value
        self.assertIn("../results/tp_mtp.json", text)  # raw source link
        self.assertIn("docs/FINDINGS.md#", text)

    def test_entry_closed_by_argument_marked_no_measurement(self):
        self.write_finding("deadend_some_lever.toml", DEAD_END_FINDING)
        html = self._generate()
        dead = _dead_ends(html)
        entry = re.search(r'<p[^>]*><b>some lever is a dead end</b>.*?</p>', dead, re.S)
        self.assertIsNotNone(entry)
        text = entry.group(0)
        self.assertIn("no measurement", text)
        # the argument (rationale) is stated
        self.assertIn("Measured worse on every number at a 20 GB cost.", text)


if __name__ == "__main__":
    unittest.main()
