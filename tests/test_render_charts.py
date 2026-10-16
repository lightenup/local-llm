import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))

from test_generate import FixturesMixin, VALID_FINDING  # noqa: E402
from test_render_lineup import FAST_FINDING, GEMMA_FINDING  # noqa: E402


class ChartTests(FixturesMixin, unittest.TestCase):
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

    def setUp(self):
        super().setUp()
        self.write_finding("lineup_quality_8bit.toml", VALID_FINDING)
        self.write_finding("lineup_fast_oq4e.toml", FAST_FINDING)
        self.write_finding("lineup_additive_gemma4.toml", GEMMA_FINDING)
        # gemma's source file is not in the default fixture copy; add it
        real = Path(__file__).resolve().parents[1] / "results" / "acc_gemma4_code.json"
        (self.results / "acc_gemma4_code.json").write_bytes(real.read_bytes())

    def test_lineup_contains_inline_svg_bar_chart(self):
        html = self._generate()
        m = re.search(r'<section id="lineup"[^>]*>.*?</section>', html, re.S)
        self.assertIn("<svg", m.group(0))

    @staticmethod
    def _table_values(lineup: str) -> set[str]:
        # values in table cells, unwrapping the raw-results link added in US6
        cells = set(re.findall(r"<td[^>]*>(.*?)</td>", lineup, re.S))
        return {re.sub(r"<[^>]+>", "", c).strip() for c in cells}

    def test_chart_shows_no_value_absent_from_a_table(self):
        html = self._generate()
        m = re.search(r'<section id="lineup"[^>]*>.*?</section>', html, re.S)
        lineup = m.group(0)
        # collect values present in table cells
        table_values = self._table_values(lineup)
        # collect value labels: text elements with text-anchor="start"
        # (value labels are start-anchored; tick labels are middle-anchored;
        # model labels are end-anchored)
        svg = re.search(r"<svg.*?</svg>", lineup, re.S).group(0)
        svg_value_labels = set(x.strip() for anchor, x in
                               re.findall(r'<text[^>]*text-anchor="(\w+)"[^>]*>([^<]+)</text>', svg)
                               if anchor == "start")
        for t in svg_value_labels:
            self.assertIn(t, table_values, f"chart value {t!r} not present in any table cell")

    def test_chart_elements_are_focusable_with_same_tooltip(self):
        html = self._generate()
        m = re.search(r'<section id="lineup"[^>]*>.*?</section>', html, re.S)
        lineup = m.group(0)
        svg = re.search(r"<svg.*?</svg>", lineup, re.S).group(0)
        bars = re.findall(r'<rect[^>]*>', svg)
        self.assertTrue(bars, "expected at least one bar in the chart")
        for bar in bars:
            self.assertIn('tabindex="0"', bar)
            self.assertIn('data-tooltip="', bar)

    def test_charted_values_equal_table_values(self):
        html = self._generate()
        m = re.search(r'<section id="lineup"[^>]*>.*?</section>', html, re.S)
        lineup = m.group(0)
        table_values = self._table_values(lineup)
        svg = re.search(r"<svg.*?</svg>", lineup, re.S).group(0)
        svg_value_labels = set(x.strip() for anchor, x in
                               re.findall(r'<text[^>]*text-anchor="(\w+)"[^>]*>([^<]+)</text>', svg)
                               if anchor == "start")
        self.assertTrue(svg_value_labels)
        for t in svg_value_labels:
            self.assertIn(t, table_values)


if __name__ == "__main__":
    unittest.main()
