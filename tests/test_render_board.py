import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))

from test_generate import FixturesMixin, VALID_FINDING  # noqa: E402

ON_TRIAL_FINDING = """\
id      = "trial_some_model"
title   = "some model is on trial"
status  = "on-trial"
models  = ["Some-Model"]
section = 30
section_title = "gemma-4-31B wins both measured axes"
rationale = "Waiting on the code suite before a verdict."

[[metrics]]
name      = "decode @ 16k prompt"
unit      = "tok/s"
direction = "higher"
tooltip_what     = "Sustained decode throughput at a 16,384-token prompt."
tooltip_how      = "bench/throughput.py, code_python context."
tooltip_evidence = "Deterministic sampling."
source_file = "tp_mtp.json"
source_path  = "summary[?pp=16384].decode_tps"
"""

RETIRED_FINDING = """\
id      = "closed_some_candidate"
title   = "some candidate is retired"
status  = "retired"
models  = ["Some-Candidate"]
section = 30
section_title = "gemma-4-31B wins both measured axes"
rationale = "livecodebench 43.2 vs 59.8, pooled p = 0.00003."

# no [[metrics]] — closed by the measurement named in the rationale
"""

DEAD_END_FINDING = """\
id      = "deadend_some_lever"
title   = "some lever is a dead end"
status  = "dead-end"
models  = ["Qwen3.8-27B-8bit"]
section = 30
section_title = "gemma-4-31B wins both measured axes"
rationale = "Measured worse on every number at a 20 GB cost."

# no [[metrics]] — renders with a "no measurement" marker
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


class BoardTests(FixturesMixin, unittest.TestCase):
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

    def test_every_finding_appears_exactly_once(self):
        self.write_finding("lineup_quality_8bit.toml", VALID_FINDING)
        self.write_finding("trial_some_model.toml", ON_TRIAL_FINDING)
        self.write_finding("closed_some_candidate.toml", RETIRED_FINDING)
        self.write_finding("deadend_some_lever.toml", DEAD_END_FINDING)
        self.write_finding("open_some_question.toml", OPEN_FINDING)
        html = self._generate()
        board = _board(html)
        for title in ("Qwen3.8-27B-8bit ships the quality tier",
                      "some model is on trial",
                      "some candidate is retired",
                      "some lever is a dead end",
                      "some question is open"):
            self.assertEqual(board.count(f"<td>{title}</td>"), 1,
                             f"{title!r} should appear exactly once in #board")

    def test_status_mapping(self):
        self.write_finding("lineup_quality_8bit.toml", VALID_FINDING)
        self.write_finding("trial_some_model.toml", ON_TRIAL_FINDING)
        self.write_finding("closed_some_candidate.toml", RETIRED_FINDING)
        self.write_finding("deadend_some_lever.toml", DEAD_END_FINDING)
        self.write_finding("open_some_question.toml", OPEN_FINDING)
        html = self._generate()
        board = _board(html)
        for status in ("shipped", "on-trial", "retired", "dead-end", "open"):
            self.assertIn(f'badge-{status}', board)

    def test_row_shows_models_and_badge(self):
        self.write_finding("closed_some_candidate.toml", RETIRED_FINDING)
        html = self._generate()
        board = _board(html)
        row = re.search(r'<tr[^>]*><td>some candidate is retired</td>.*?</tr>', board, re.S)
        self.assertIsNotNone(row)
        self.assertIn("Some-Candidate", row.group(0))
        self.assertIn("badge-retired", row.group(0))

    def test_retired_and_dead_end_carry_rationale_with_findings_link(self):
        self.write_finding("closed_some_candidate.toml", RETIRED_FINDING)
        self.write_finding("deadend_some_lever.toml", DEAD_END_FINDING)
        html = self._generate()
        board = _board(html)
        for title, rationale in (
            ("some candidate is retired", "livecodebench 43.2 vs 59.8, pooled p = 0.00003."),
            ("some lever is a dead end", "Measured worse on every number at a 20 GB cost."),
        ):
            row = re.search(rf'<tr[^>]*><td>{title}</td>.*?</tr>', board, re.S)
            self.assertIsNotNone(row, title)
            self.assertIn(rationale, row.group(0))
            self.assertIn("docs/FINDINGS.md#", row.group(0))

    def test_no_rationale_row_is_not_blank(self):
        # a finding without a rationale still renders a row (empty cell, not a
        # missing row)
        body = VALID_FINDING  # no rationale field
        self.write_finding("lineup_quality_8bit.toml", body)
        html = self._generate()
        board = _board(html)
        row = re.search(r'<tr[^>]*><td>Qwen3.8-27B-8bit ships the quality tier</td>.*?</tr>',
                        board, re.S)
        self.assertIsNotNone(row)


if __name__ == "__main__":
    unittest.main()
