import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))

from test_generate import FixturesMixin, VALID_FINDING  # noqa: E402
from test_render_board import (  # noqa: E402
    ON_TRIAL_FINDING, RETIRED_FINDING, DEAD_END_FINDING, OPEN_FINDING,
)
from test_render_lineup import FAST_FINDING, GEMMA_FINDING  # noqa: E402


class FilterDomContractTests(FixturesMixin, unittest.TestCase):
    """T019(a): every finding entry in all three views carries the five
    data attributes per data-model.md §DOM data contract."""

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
        self.write_finding("trial_some_model.toml", ON_TRIAL_FINDING)
        self.write_finding("closed_some_candidate.toml", RETIRED_FINDING)
        self.write_finding("deadend_some_lever.toml", DEAD_END_FINDING)
        self.write_finding("open_some_question.toml", OPEN_FINDING)

    def test_lineup_entries_carry_data_attrs(self):
        html = self._generate()
        lineup = re.search(r'<section id="lineup"[^>]*>.*?</section>', html, re.S).group(0)
        divs = re.findall(r'<div class="finding"[^>]*>', lineup)
        self.assertGreaterEqual(len(divs), 3, "expected at least 3 lineup finding divs")
        for d in divs:
            self.assertIn('data-model="', d)
            self.assertIn('data-tier="', d)
            self.assertIn('data-status="shipped"', d)
            self.assertIn('data-title="', d)
            self.assertIn('data-rationale="', d)

    def test_lineup_data_model_values(self):
        html = self._generate()
        lineup = re.search(r'<section id="lineup"[^>]*>.*?</section>', html, re.S).group(0)
        divs = re.findall(r'<div class="finding"[^>]*>', lineup)
        models_seen = set()
        for d in divs:
            m = re.search(r'data-model="([^"]*)"', d)
            self.assertIsNotNone(m)
            models_seen.update(m.group(1).split())
        self.assertIn("Qwen3.8-27B-8bit", models_seen)
        self.assertIn("Qwen3.8-27B-oQ4e", models_seen)
        self.assertIn("gemma-4-31B", models_seen)

    def test_board_rows_carry_data_attrs(self):
        html = self._generate()
        board = re.search(r'<section id="board"[^>]*>.*?</section>', html, re.S).group(0)
        rows = re.findall(r'<tr class="finding"[^>]*>', board)
        self.assertGreaterEqual(len(rows), 5, "expected at least 5 board finding rows")
        for r in rows:
            self.assertIn('data-model="', r)
            self.assertIn('data-status="', r)
            self.assertIn('data-title="', r)
            self.assertIn('data-rationale="', r)

    def test_board_data_status_values(self):
        html = self._generate()
        board = re.search(r'<section id="board"[^>]*>.*?</section>', html, re.S).group(0)
        rows = re.findall(r'<tr class="finding"[^>]*>', board)
        statuses = set()
        for r in rows:
            m = re.search(r'data-status="([^"]*)"', r)
            self.assertIsNotNone(m)
            statuses.add(m.group(1))
        self.assertEqual(statuses, {"shipped", "on-trial", "retired", "dead-end", "open"})

    def test_deadend_entries_carry_data_attrs(self):
        html = self._generate()
        dead = re.search(r'<section id="dead-ends"[^>]*>.*?</section>', html, re.S).group(0)
        ps = re.findall(r'<p class="finding"[^>]*>', dead)
        self.assertGreaterEqual(len(ps), 1, "expected at least 1 dead-end finding entry")
        for p in ps:
            self.assertIn('data-model="', p)
            self.assertIn('data-status="dead-end"', p)
            self.assertIn('data-title="', p)
            self.assertIn('data-rationale="', p)

    def test_data_title_matches_finding_title(self):
        html = self._generate()
        board = re.search(r'<section id="board"[^>]*>.*?</section>', html, re.S).group(0)
        rows = re.findall(r'<tr class="finding"[^>]*>', board)
        titles = set()
        for r in rows:
            m = re.search(r'data-title="([^"]*)"', r)
            self.assertIsNotNone(m)
            titles.add(m.group(1))
        self.assertIn("Qwen3.8-27B-8bit ships the quality tier", titles)
        self.assertIn("some model is on trial", titles)
        self.assertIn("some candidate is retired", titles)
        self.assertIn("some lever is a dead end", titles)
        self.assertIn("some question is open", titles)


class FilterControlsTests(FixturesMixin, unittest.TestCase):
    """T019(b)-(d): controls exist, native and labelled; initial count;
    per-view no-match paragraphs hidden by default."""

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
        self.write_finding("trial_some_model.toml", ON_TRIAL_FINDING)
        self.write_finding("closed_some_candidate.toml", RETIRED_FINDING)
        self.write_finding("deadend_some_lever.toml", DEAD_END_FINDING)
        self.write_finding("open_some_question.toml", OPEN_FINDING)

    def test_model_select_exists_with_options(self):
        html = self._generate()
        m = re.search(r'<select id="f-model">(.*?)</select>', html, re.S)
        self.assertIsNotNone(m, "f-model select not found")
        opts = re.findall(r'<option value="([^"]*)">', m.group(1))
        self.assertIn("all", opts)
        self.assertIn("Qwen3.8-27B-8bit", opts)
        self.assertIn("Qwen3.8-27B-oQ4e", opts)
        self.assertIn("gemma-4-31B", opts)
        self.assertIn("Some-Model", opts)
        self.assertIn("Some-Candidate", opts)

    def test_model_options_are_sorted(self):
        html = self._generate()
        m = re.search(r'<select id="f-model">(.*?)</select>', html, re.S)
        opts = re.findall(r'<option value="([^"]*)">', m.group(1))
        non_all = [o for o in opts if o != "all"]
        self.assertEqual(non_all, sorted(non_all))

    def test_tier_select_exists_with_options(self):
        html = self._generate()
        m = re.search(r'<select id="f-tier">(.*?)</select>', html, re.S)
        self.assertIsNotNone(m, "f-tier select not found")
        opts = re.findall(r'<option value="([^"]*)">', m.group(1))
        self.assertIn("all", opts)
        self.assertIn("quality", opts)
        self.assertIn("fast", opts)
        self.assertIn("additive", opts)

    def test_status_select_exists_with_options(self):
        html = self._generate()
        m = re.search(r'<select id="f-status">(.*?)</select>', html, re.S)
        self.assertIsNotNone(m, "f-status select not found")
        opts = re.findall(r'<option value="([^"]*)">', m.group(1))
        self.assertIn("all", opts)
        for s in ("shipped", "on-trial", "retired", "dead-end", "open"):
            self.assertIn(s, opts)

    def test_search_input_exists(self):
        html = self._generate()
        self.assertIn('<input type="search" id="f-text"', html)

    def test_clear_button_exists(self):
        html = self._generate()
        self.assertIn('<button id="f-clear" type="button">', html)

    def test_count_paragraph_exists_with_role_status(self):
        html = self._generate()
        m = re.search(r'<p id="f-count" role="status">(.*?)</p>', html, re.S)
        self.assertIsNotNone(m, "f-count paragraph not found")

    def test_initial_count_text(self):
        html = self._generate()
        m = re.search(r'<p id="f-count" role="status">(.*?)</p>', html, re.S)
        self.assertIsNotNone(m)
        text = m.group(1).strip()
        self.assertRegex(text, r"\d+ of \d+ findings")

    def test_no_match_paragraphs_hidden_by_default(self):
        html = self._generate()
        no_match = re.findall(r'<p[^>]*class="no-match"[^>]*>', html)
        self.assertGreaterEqual(len(no_match), 3, "expected at least 3 no-match paragraphs")
        for p in no_match:
            self.assertIn("hidden", p)

    def test_no_match_paragraphs_have_clear_buttons(self):
        html = self._generate()
        no_match_blocks = re.findall(
            r'<p[^>]*class="no-match"[^>]*>(.*?)</p>', html, re.S)
        self.assertGreaterEqual(len(no_match_blocks), 3)
        for block in no_match_blocks:
            self.assertIn("Clear filters", block)
            self.assertIn('type="button"', block)


class FilterScriptsTests(FixturesMixin, unittest.TestCase):
    """T019(e): SCRIPTS re-evaluates on input/change, toggles hidden,
    updates #f-count."""

    def test_scripts_reevaluate_on_input_and_change(self):
        from assets import SCRIPTS
        self.assertIn("addEventListener('input'", SCRIPTS)
        self.assertIn("addEventListener('change'", SCRIPTS)

    def test_scripts_toggle_hidden_attribute(self):
        from assets import SCRIPTS
        self.assertIn("hidden", SCRIPTS)
        self.assertNotIn(".remove(", SCRIPTS)

    def test_scripts_update_f_count(self):
        from assets import SCRIPTS
        self.assertIn("f-count", SCRIPTS)

    def test_scripts_no_network_or_persistence(self):
        from assets import SCRIPTS
        for forbidden in ("fetch(", "XMLHttpRequest", "WebSocket", "import(",
                          "localStorage", "sessionStorage", "indexedDB",
                          "document.cookie", "history.pushState",
                          "history.replaceState", "new Date("):
            self.assertNotIn(forbidden, SCRIPTS,
                             f"SCRIPTS contains forbidden API: {forbidden}")

    def test_scripts_use_clear_handler(self):
        from assets import SCRIPTS
        self.assertIn("f-clear", SCRIPTS)


if __name__ == "__main__":
    unittest.main()
