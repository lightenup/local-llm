import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))

from test_generate import FixturesMixin, VALID_FINDING  # noqa: E402

SECOND_FINDING = """\
id      = "lineup_quality_8bit_v2"
title   = "Gemma-4-31B ships the quality tier"
status  = "shipped"
models  = ["Gemma-4-31B"]
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

ON_TRIAL_FINDING = """\
id      = "trial_some_model"
title   = "some model is on trial"
status  = "on-trial"
models  = ["Some-Model"]
section = 30
section_title = "gemma-4-31B wins both measured axes"
rationale = "Waiting on the code suite before a verdict."

# no [[metrics]]
"""

RETIRED_FINDING = """\
id      = "closed_some_candidate"
title   = "some candidate is retired"
status  = "retired"
models  = ["Some-Candidate"]
section = 30
section_title = "gemma-4-31B wins both measured axes"
rationale = "livecodebench 43.2 vs 59.8, pooled p = 0.00003."

# no [[metrics]]
"""

OPEN_FINDING = """\
id      = "open_some_question"
title   = "some question is open"
status  = "open"
models  = ["Qwen3.8-27B-8bit"]
section = 30
section_title = "gemma-4-31B wins both measured axes"
rationale = "Still to run; see the backlog."

# no [[metrics]]
"""


def _board(html: str) -> str:
    m = re.search(r'<section id="board"[^>]*>.*?</section>', html, re.S)
    assert m, "no #board section"
    return m.group(0)


def _lineup(html: str) -> str:
    m = re.search(r'<section id="lineup"[^>]*>.*?</section>', html, re.S)
    assert m, "no #lineup section"
    return m.group(0)


class SortHeaderTests(FixturesMixin, unittest.TestCase):
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
                out.append(render.ResolvedMetric(m, r.outcome, r.value, r.reason))
            resolved[f.id] = out
        return render.render_page(findings, sections, resolved, 0, "2026-01-01 00:00:00 UTC")

    def test_multi_row_board_has_sortable_th_with_button(self):
        self.write_finding("lineup_quality_8bit.toml", VALID_FINDING)
        self.write_finding("trial_some_model.toml", ON_TRIAL_FINDING)
        self.write_finding("closed_some_candidate.toml", RETIRED_FINDING)
        self.write_finding("open_some_question.toml", OPEN_FINDING)
        html = self._generate()
        board = _board(html)
        for label in ("title", "model(s)", "status", "rationale"):
            pat = rf'<th[^>]*scope="col"[^>]*aria-sort="none"[^>]*><button[^>]*>{re.escape(label)}</button></th>'
            self.assertRegex(board, pat, f"board column {label!r} missing sortable th")

    def test_multi_row_lineup_table_has_sortable_th_with_button(self):
        multi_metric = VALID_FINDING + """
[[metrics]]
name      = "prefill @ 16k prompt"
unit      = "tok/s"
direction = "higher"
headline  = true
tooltip_what     = "Prefill throughput at a 16,384-token prompt."
tooltip_how      = "bench/throughput.py, code_python context."
tooltip_evidence = "Deterministic sampling."
source_file = "tp_mtp.json"
source_path  = "summary[?pp=16384].prefill_tps"
"""
        self.write_finding("lineup_quality_8bit.toml", multi_metric)
        html = self._generate()
        lineup = _lineup(html)
        for label in ("metric", "value", "unit"):
            pat = rf'<th[^>]*scope="col"[^>]*aria-sort="none"[^>]*><button[^>]*>{re.escape(label)}</button></th>'
            self.assertRegex(lineup, pat, f"lineup column {label!r} missing sortable th")

    def test_single_row_table_has_no_sort_buttons(self):
        self.write_finding("lineup_quality_8bit.toml", VALID_FINDING)
        html = self._generate()
        lineup = _lineup(html)
        self.assertNotIn('aria-sort', lineup)

    def test_value_column_th_carries_data_sort(self):
        multi_metric = VALID_FINDING + """
[[metrics]]
name      = "prefill @ 16k prompt"
unit      = "tok/s"
direction = "higher"
headline  = true
tooltip_what     = "Prefill throughput at a 16,384-token prompt."
tooltip_how      = "bench/throughput.py, code_python context."
tooltip_evidence = "Deterministic sampling."
source_file = "tp_mtp.json"
source_path  = "summary[?pp=16384].prefill_tps"
"""
        self.write_finding("lineup_quality_8bit.toml", multi_metric)
        html = self._generate()
        lineup = _lineup(html)
        self.assertRegex(lineup, r'<th[^>]*data-sort="higher"[^>]*>')

    def test_numeric_cells_carry_data_value(self):
        self.write_finding("lineup_quality_8bit.toml", VALID_FINDING)
        html = self._generate()
        lineup = _lineup(html)
        self.assertRegex(lineup, r'<td[^>]*data-value="[\d.]+"[^>]*>')

    def test_stale_cells_have_no_data_value(self):
        body = VALID_FINDING.replace(
            'source_file = "tp_mtp.json"',
            'source_file = "nonexistent.json"',
        )
        self.write_finding("lineup_quality_8bit.toml", body)
        html = self._generate()
        lineup = _lineup(html)
        self.assertNotIn('data-value', lineup)


class SortScriptsTests(FixturesMixin, unittest.TestCase):
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
                out.append(render.ResolvedMetric(m, r.outcome, r.value, r.reason))
            resolved[f.id] = out
        return render.render_page(findings, sections, resolved, 0, "2026-01-01 00:00:00 UTC")

    def test_scripts_sort_by_data_value(self):
        self.write_finding("lineup_quality_8bit.toml", VALID_FINDING)
        self.write_finding("lineup_quality_8bit_v2.toml", SECOND_FINDING)
        html = self._generate()
        m = re.search(r'<script>(.*?)</script>', html, re.S)
        self.assertIsNotNone(m)
        scripts = m.group(1)
        self.assertIn("data-value", scripts)

    def test_scripts_set_aria_sort(self):
        self.write_finding("lineup_quality_8bit.toml", VALID_FINDING)
        self.write_finding("lineup_quality_8bit_v2.toml", SECOND_FINDING)
        html = self._generate()
        m = re.search(r'<script>(.*?)</script>', html, re.S)
        self.assertIsNotNone(m)
        scripts = m.group(1)
        self.assertIn("aria-sort", scripts)

    def test_scripts_render_arrow_in_active_button(self):
        self.write_finding("lineup_quality_8bit.toml", VALID_FINDING)
        self.write_finding("lineup_quality_8bit_v2.toml", SECOND_FINDING)
        html = self._generate()
        m = re.search(r'<script>(.*?)</script>', html, re.S)
        self.assertIsNotNone(m)
        scripts = m.group(1)
        self.assertIn("\u2191", scripts)
        self.assertIn("\u2193", scripts)

    def test_scripts_no_network_or_persistence(self):
        self.write_finding("lineup_quality_8bit.toml", VALID_FINDING)
        html = self._generate()
        m = re.search(r'<script>(.*?)</script>', html, re.S)
        self.assertIsNotNone(m)
        scripts = m.group(1)
        for forbidden in ("fetch", "XMLHttpRequest", "WebSocket", "import(",
                          "localStorage", "sessionStorage", "indexedDB",
                          "document.cookie", "history.pushState",
                          "history.replaceState", "new Date()"):
            self.assertNotIn(forbidden, scripts,
                             f"SCRIPTS contains forbidden API {forbidden!r}")


if __name__ == "__main__":
    unittest.main()
