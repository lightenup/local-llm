import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))

from test_generate import FixturesMixin, VALID_FINDING  # noqa: E402

FAST_FINDING = """\
id      = "lineup_fast_oq4e"
title   = "oQ4e ships the fast tier"
status  = "shipped"
tier    = "fast"
models  = ["Qwen3.8-27B-oQ4e"]
section = 19
section_title = "The lineup: 8-bit for quality, oQ4e for speed"

[[metrics]]
name      = "decode @ 16k prompt"
unit      = "tok/s"
direction = "higher"
headline  = true
tooltip_what     = "Sustained decode throughput at a 16,384-token prompt."
tooltip_how      = "bench/throughput.py, code_python context."
tooltip_evidence = "Deterministic sampling."
source_file = "tp_oq4e.json"
source_path  = "summary[?pp=16384].decode_tps"
"""

GEMMA_FINDING = """\
id      = "lineup_additive_gemma4"
title   = "gemma-4-31B is the additive tier"
status  = "shipped"
tier    = "additive"
models  = ["gemma-4-31B"]
section = 30
section_title = "gemma-4-31B wins both measured axes"

[[metrics]]
name      = "livecodebench accuracy"
unit      = "%"
direction = "higher"
headline  = true
tooltip_what     = "LiveCodeBench competitive-programming accuracy."
tooltip_how      = "bench/accuracy.py code, 500 samples, batch 8."
tooltip_evidence = "Paired McNemar against the 8-bit."
source_file = "acc_gemma4_code.json"
source_path  = "results[?benchmark=livecodebench].accuracy"
"""


class LineupViewTests(FixturesMixin, unittest.TestCase):
    def _generate(self):
        import findings as findings_mod
        import render
        from sources import resolve

        findings, problems = findings_mod.load_findings(self.findings)
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
                baseline_value = None
                if m.baseline_source_file:
                    br = resolve(self.results / m.baseline_source_file, m.baseline_source_path)
                    baseline_value = br.value
                out.append(render.ResolvedMetric(m, r.outcome, r.value, r.reason, baseline_value))
            resolved[f.id] = out
        return render.render_page(findings, sections, resolved, 0, "2026-01-01 00:00:00 UTC")

    def test_all_shipped_findings_render_in_lineup(self):
        self.write_finding("lineup_quality_8bit.toml", VALID_FINDING)
        self.write_finding("lineup_fast_oq4e.toml", FAST_FINDING)
        self.write_finding("lineup_additive_gemma4.toml", GEMMA_FINDING)
        html = self._generate()
        m = re.search(r'<section id="lineup"[^>]*>.*?</section>', html, re.S)
        self.assertIsNotNone(m)
        lineup = m.group(0)
        for title in ("Qwen3.8-27B-8bit ships the quality tier",
                      "oQ4e ships the fast tier",
                      "gemma-4-31B is the additive tier"):
            self.assertIn(title, lineup)

    def test_tier_grouping(self):
        self.write_finding("lineup_quality_8bit.toml", VALID_FINDING)
        self.write_finding("lineup_fast_oq4e.toml", FAST_FINDING)
        self.write_finding("lineup_additive_gemma4.toml", GEMMA_FINDING)
        html = self._generate()
        m = re.search(r'<section id="lineup"[^>]*>.*?</section>', html, re.S)
        lineup = m.group(0)
        quality_pos = lineup.index("quality tier")
        fast_pos = lineup.index("fast tier")
        additive_pos = lineup.index("additive tier")
        self.assertLess(quality_pos, fast_pos)
        self.assertLess(fast_pos, additive_pos)
        self.assertIn("Qwen3.8-27B-8bit ships the quality tier", lineup[quality_pos:fast_pos])
        self.assertIn("oQ4e ships the fast tier", lineup[fast_pos:additive_pos])
        self.assertIn("gemma-4-31B is the additive tier", lineup[additive_pos:])

    def test_no_number_rendered_without_tooltip(self):
        self.write_finding("lineup_quality_8bit.toml", VALID_FINDING)
        html = self._generate()
        m = re.search(r'<section id="lineup"[^>]*>.*?</section>', html, re.S)
        lineup = m.group(0)
        # every table row with a resolved value must carry a data-tooltip trigger
        rows = re.findall(r"<tr>.*?</tr>", lineup, re.S)
        value_rows = [r for r in rows
                      if re.search(r"<td[^>]*>(?:<a[^>]*>)?34\.0(?:</a>)?</td>", r)
                      or re.search(r"<td[^>]*>(?:<a[^>]*>)?58\.8(?:</a>)?</td>", r)]
        self.assertTrue(value_rows, "expected at least one resolved value row")
        for r in value_rows:
            self.assertIn('data-tooltip="', r)
            self.assertIn('tabindex="0"', r)

    def test_tooltip_contains_what_how_evidence_and_findings_link(self):
        self.write_finding("lineup_quality_8bit.toml", VALID_FINDING)
        html = self._generate()
        m = re.search(r'<section id="lineup"[^>]*>.*?</section>', html, re.S)
        lineup = m.group(0)
        tips = re.findall(r'data-tooltip="([^"]*)"', lineup)
        self.assertTrue(tips)
        for tip in tips:
            self.assertIn("what", tip)
            self.assertIn("how", tip)
            self.assertIn("evidence", tip)
            self.assertIn("FINDINGS", tip)
            self.assertIn("docs/FINDINGS.md#", tip)

    def test_value_equals_source_resolution(self):
        # prove nothing is hardcoded: point the finding at a temp results file
        # with a distinct value and assert the rendered HTML carries that value
        import json
        src = json.loads((Path(__file__).resolve().parents[1] / "results" / "tp_mtp.json").read_text())
        src["summary"] = [dict(row, decode_tps=99.9) for row in src["summary"]]
        (self.results / "tp_mtp.json").write_text(json.dumps(src))
        self.write_finding("lineup_quality_8bit.toml", VALID_FINDING)
        html = self._generate()
        m = re.search(r'<section id="lineup"[^>]*>.*?</section>', html, re.S)
        self.assertIn("99.9", m.group(0))
        self.assertNotIn("34.0", m.group(0))

    def test_tooltip_triggers_are_focusable_and_dismissible(self):
        self.write_finding("lineup_quality_8bit.toml", VALID_FINDING)
        html = self._generate()
        self.assertIn('tabindex="0"', html)
        self.assertIn("Escape", html)  # dismissible via JS
        self.assertIn("focusin", html)
        self.assertIn("mouseover", html)

    def test_metric_without_source_renders_no_measurement_marker(self):
        body = VALID_FINDING.replace(
            'source_file = "tp_mtp.json"\nsource_path  = "summary[?pp=16384].decode_tps"',
            "")
        self.write_finding("lineup_quality_8bit.toml", body)
        html = self._generate()
        m = re.search(r'<section id="lineup"[^>]*>.*?</section>', html, re.S)
        self.assertIn("no measurement", m.group(0))


if __name__ == "__main__":
    unittest.main()
