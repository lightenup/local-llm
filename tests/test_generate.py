"""Tests for the generator CLI contract (contracts/cli.md) — T006 red half.

Run: python3 -m unittest discover -s tests -v
Invokes the CLI as a subprocess against a temp repo fixture (temp findings/
+ real results/ + temp docs/FINDINGS.md). Stale (exit 2) behaviour is tested
in US5; this file covers the exit-0 / exit-1 contract plus determinism and
the empty-state edge case (extended in T010, T021, T023).
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GENERATE = ROOT / "dashboard" / "generate.py"
REAL_RESULTS = ROOT / "results"

FINDINGS_MD = """# Findings

## 19. The lineup: 8-bit for quality, oQ4e for speed

text

## 30. gemma-4-31B wins both measured axes

text
"""

VALID_FINDING = """\
id      = "lineup_quality_8bit"
title   = "Qwen3.8-27B-8bit ships the quality tier"
status  = "shipped"
models  = ["Qwen3.8-27B-8bit"]
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
source_file = "tp_mtp.json"
source_path  = "summary[?pp=16384].decode_tps"
"""


class FixturesMixin:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.findings = self.repo / "findings"
        self.findings.mkdir()
        self.results = self.repo / "results"
        self.results.mkdir()
        # real results files, copied so the fixture is self-contained
        for name in ("tp_mtp.json", "tp_oq4e.json", "ctx_mtp.json"):
            (self.results / name).write_bytes((REAL_RESULTS / name).read_bytes())
        self.docs = self.repo / "docs"
        self.docs.mkdir()
        (self.docs / "FINDINGS.md").write_text(FINDINGS_MD)

    def write_finding(self, name: str, body: str) -> Path:
        p = self.findings / name
        p.write_text(body)
        return p

    def run_cli(self):
        env = dict(os.environ)
        return subprocess.run(
            [sys.executable, str(GENERATE)],
            cwd=self.repo,
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

    def site(self) -> Path:
        return self.repo / "site" / "index.html"


class ValidLayerTests(FixturesMixin, unittest.TestCase):
    def test_exit_zero_and_site_written(self):
        self.write_finding("lineup_quality_8bit.toml", VALID_FINDING)
        r = self.run_cli()
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertTrue(self.site().is_file())

    def test_stdout_summary_line(self):
        self.write_finding("lineup_quality_8bit.toml", VALID_FINDING)
        r = self.run_cli()
        self.assertIn("1 finding", r.stdout)
        self.assertIn("site/index.html", r.stdout)

    def test_empty_findings_layer_is_exit_zero_with_empty_state(self):
        r = self.run_cli()
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        html = self.site().read_text()
        self.assertIn("no findings yet", html)


class InvalidLayerTests(FixturesMixin, unittest.TestCase):
    def test_exit_one_no_site_written(self):
        self.write_finding("lineup_quality_8bit.toml",
                           VALID_FINDING.replace('status  = "shipped"',
                                                 'status  = "bogus"'))
        r = self.run_cli()
        self.assertEqual(r.returncode, 1)
        self.assertFalse(self.site().exists())

    def test_preexisting_site_stays_byte_identical(self):
        self.write_finding("lineup_quality_8bit.toml", VALID_FINDING)
        r = self.run_cli()
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        before = self.site().read_bytes()
        # now corrupt the layer
        self.write_finding("lineup_quality_8bit.toml",
                           VALID_FINDING.replace('status  = "shipped"',
                                                 'status  = "bogus"'))
        r = self.run_cli()
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.site().read_bytes(), before)

    def test_stderr_lists_every_problem_with_file_and_field(self):
        body = (VALID_FINDING
                .replace('status  = "shipped"', 'status  = "bogus"')
                .replace("section = 19", "section = 0"))
        self.write_finding("lineup_quality_8bit.toml", body)
        r = self.run_cli()
        self.assertEqual(r.returncode, 1)
        self.assertIn("lineup_quality_8bit.toml", r.stderr)
        self.assertIn("status", r.stderr)
        self.assertIn("section", r.stderr)


class DeterminismTests(FixturesMixin, unittest.TestCase):
    """T010 red half: identical inputs -> byte-identical output except the
    "generated at" stamp, which is the only allowed difference (FR-012)."""

    def _stamp_line_index(self, lines):
        for i, line in enumerate(lines):
            if "generated at" in line.lower():
                return i
        self.fail("no 'generated at' stamp line found")

    def _assert_only_stamp_differs(self, run1: str, run2: str):
        lines1 = run1.splitlines()
        lines2 = run2.splitlines()
        self.assertEqual(len(lines1), len(lines2))
        i1 = self._stamp_line_index(lines1)
        i2 = self._stamp_line_index(lines2)
        self.assertEqual(i1, i2)
        diffs = [(i, a, b) for i, (a, b) in enumerate(zip(lines1, lines2)) if a != b]
        # the stamp MAY be identical if both runs land in the same second;
        # but if it differs, it must be the ONLY difference
        for i, a, b in diffs:
            self.assertEqual(i, i1)
            self.assertIn("generated at", a.lower())
            self.assertIn("generated at", b.lower())

    def test_two_runs_differ_only_in_stamp_line(self):
        self.write_finding("lineup_quality_8bit.toml", VALID_FINDING)
        r1 = self.run_cli()
        self.assertEqual(r1.returncode, 0, msg=r1.stderr)
        html1 = self.site().read_text()
        r2 = self.run_cli()
        html2 = self.site().read_text()
        self._assert_only_stamp_differs(html1, html2)

    def test_stamp_line_is_the_only_variable_field(self):
        # force the stamp to differ by editing it between runs' captures:
        # regenerate, read, regenerate, read; every non-stamp line must be
        # byte-identical across the two captures
        self.write_finding("lineup_quality_8bit.toml", VALID_FINDING)
        self.run_cli()
        html1 = self.site().read_text()
        self.run_cli()
        html2 = self.site().read_text()
        lines1 = html1.splitlines()
        lines2 = html2.splitlines()
        i = self._stamp_line_index(lines1)
        for j, (a, b) in enumerate(zip(lines1, lines2)):
            if j == i:
                continue
            self.assertEqual(a, b, f"line {j} differs: {a!r} != {b!r}")


class StaleSourceTests(FixturesMixin, unittest.TestCase):
    """T023 (US5): staleness is loud and local — exactly the affected
    findings are marked, the rest of the site renders normally, exit 2."""

    def test_missing_source_file_marks_exactly_that_finding_and_exits_2(self):
        self.write_finding("lineup_quality_8bit.toml", VALID_FINDING)
        self.write_finding("lineup_fast_oq4e.toml",
                           VALID_FINDING.replace(
                               "id      = \"lineup_quality_8bit\"",
                               "id      = \"lineup_fast_oq4e\"").replace(
                               "Qwen3.8-27B-8bit ships the quality tier",
                               "oQ4e ships the fast tier").replace(
                               'source_file = "tp_mtp.json"',
                               'source_file = "does_not_exist.json"'))
        r = self.run_cli()
        self.assertEqual(r.returncode, 2, msg=r.stderr)
        self.assertTrue(self.site().is_file())
        self.assertIn("lineup_fast_oq4e", r.stderr)
        self.assertIn("does_not_exist.json", r.stderr)
        html = self.site().read_text()
        # the stale finding carries a visible marker and no value
        self.assertIn("marker-stale", html)
        self.assertIn("does_not_exist.json", html)
        # the other finding renders normally with its resolved value
        self.assertIn("34.0", html)
        # the rest of the site renders normally
        self.assertIn('<section id="board"', html)
        self.assertIn("Qwen3.8-27B-8bit ships the quality tier", html)

    def test_unresolvable_path_marks_the_finding_and_names_the_path(self):
        self.write_finding("lineup_quality_8bit.toml",
                           VALID_FINDING.replace(
                               'source_path  = "summary[?pp=16384].decode_tps"',
                               'source_path  = "summary[?pp=16384].drifted_key"'))
        r = self.run_cli()
        self.assertEqual(r.returncode, 2, msg=r.stderr)
        self.assertIn("summary[?pp=16384].drifted_key", r.stderr)
        html = self.site().read_text()
        self.assertIn("marker-stale", html)
        self.assertIn("summary[?pp=16384].drifted_key", html)

    def test_all_sources_resolve_means_no_markers_and_exit_0(self):
        self.write_finding("lineup_quality_8bit.toml", VALID_FINDING)
        r = self.run_cli()
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        html = self.site().read_text()
        self.assertNotIn('class="marker-stale"', html)
        self.assertNotIn("stale", r.stderr)

    def test_stale_finding_does_not_affect_other_views(self):
        self.write_finding("lineup_quality_8bit.toml",
                           VALID_FINDING.replace(
                               'source_path  = "summary[?pp=16384].decode_tps"',
                               'source_path  = "summary[?pp=16384].drifted_key"'))
        self.write_finding("lineup_fast_oq4e.toml",
                           VALID_FINDING.replace(
                               "id      = \"lineup_quality_8bit\"",
                               "id      = \"lineup_fast_oq4e\"").replace(
                               "Qwen3.8-27B-8bit ships the quality tier",
                               "oQ4e ships the fast tier").replace(
                               'source_file = "tp_mtp.json"',
                               'source_file = "tp_oq4e.json"'))
        r = self.run_cli()
        self.assertEqual(r.returncode, 2, msg=r.stderr)
        html = self.site().read_text()
        board = re.search(r'<section id="board"[^>]*>.*?</section>', html, re.S).group(0)
        # both findings appear in the board despite one being stale
        self.assertIn("Qwen3.8-27B-8bit ships the quality tier", board)
        self.assertIn("oQ4e ships the fast tier", board)
        # the healthy finding's value is present
        self.assertIn("58.8", html)


class OneFileFlowTests(FixturesMixin, unittest.TestCase):
    """T021 (US4): the one-file flow is the design's point (SC-003). Adding,
    removing, or editing a single findings file is the entire curation
    interface; nothing under dashboard/ may need to change."""

    def _dashboard_hashes(self):
        import hashlib
        out = {}
        for p in sorted((Path(__file__).resolve().parents[1] / "dashboard").glob("*.py")):
            out[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
        return out

    def test_new_finding_renders_after_exactly_one_run_without_dashboard_changes(self):
        self.write_finding("lineup_quality_8bit.toml", VALID_FINDING)
        before = self._dashboard_hashes()
        r = self.run_cli()
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        html = self.site().read_text()
        self.assertIn("Qwen3.8-27B-8bit ships the quality tier", html)
        self.assertIn("34.0", html)  # resolved value, not hardcoded
        self.assertIn('data-tooltip="', html)
        self.assertEqual(self._dashboard_hashes(), before)

    def test_removing_the_file_removes_the_finding(self):
        self.write_finding("lineup_quality_8bit.toml", VALID_FINDING)
        self.run_cli()
        self.assertIn("Qwen3.8-27B-8bit ships the quality tier",
                      self.site().read_text())
        (self.findings / "lineup_quality_8bit.toml").unlink()
        r = self.run_cli()
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertNotIn("Qwen3.8-27B-8bit ships the quality tier",
                         self.site().read_text())

    def test_status_change_moves_the_finding_between_views(self):
        self.write_finding("lineup_quality_8bit.toml",
                           VALID_FINDING.replace('status  = "shipped"',
                                                 'status  = "on-trial"'))
        r = self.run_cli()
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        html = self.site().read_text()
        lineup = re.search(r'<section id="lineup"[^>]*>.*?</section>', html, re.S).group(0)
        board = re.search(r'<section id="board"[^>]*>.*?</section>', html, re.S).group(0)
        self.assertNotIn("Qwen3.8-27B-8bit ships the quality tier", lineup)
        self.assertIn("Qwen3.8-27B-8bit ships the quality tier", board)
        # now flip to shipped: it moves into the lineup
        self.write_finding("lineup_quality_8bit.toml", VALID_FINDING)
        r = self.run_cli()
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        html = self.site().read_text()
        lineup = re.search(r'<section id="lineup"[^>]*>.*?</section>', html, re.S).group(0)
        self.assertIn("Qwen3.8-27B-8bit ships the quality tier", lineup)

    def test_typoed_source_path_fails_naming_file_finding_and_path(self):
        # a well-formed path that does not resolve is a data condition:
        # the site is written with a visible stale marker on exactly that
        # finding and the run exits 2, naming the finding, the file and the
        # path (US4 acceptance 2, contracts/cli.md)
        self.write_finding("lineup_quality_8bit.toml",
                           VALID_FINDING.replace(
                               'source_path  = "summary[?pp=16384].decode_tps"',
                               'source_path  = "summary[?pp=16384].typo"'))
        r = self.run_cli()
        self.assertEqual(r.returncode, 2, msg=r.stderr)
        self.assertTrue(self.site().is_file())
        self.assertIn("lineup_quality_8bit", r.stderr)
        self.assertIn("tp_mtp.json", r.stderr)
        self.assertIn("summary[?pp=16384].typo", r.stderr)
        html = self.site().read_text()
        self.assertIn("stale", html)

    def test_generation_is_read_only_on_results_and_profiles(self):
        import hashlib
        self.write_finding("lineup_quality_8bit.toml", VALID_FINDING)
        self.run_cli()

        def hashes(d: Path):
            out = {}
            if d.is_dir():
                for p in sorted(d.rglob("*")):
                    if p.is_file():
                        out[str(p.relative_to(d))] = hashlib.sha256(p.read_bytes()).hexdigest()
            return out

        real = Path(__file__).resolve().parents[1]
        before = (hashes(real / "results"), hashes(real / "profiles"))
        r = self.run_cli()
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        after = (hashes(real / "results"), hashes(real / "profiles"))
        self.assertEqual(after, before)


class JSDisabledCompletenessTests(FixturesMixin, unittest.TestCase):
    """T012: FR-010 — strip all <script> blocks; every value, tooltip text,
    link, and rationale must still be present in the static HTML."""

    def test_all_content_present_without_scripts(self):
        self.write_finding("lineup_quality_8bit.toml", VALID_FINDING)
        r = self.run_cli()
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        html = self.site().read_text()
        stripped = re.sub(r"<script>.*?</script>", "", html, flags=re.S)
        # resolved metric value
        self.assertIn("34.0", stripped)
        # tooltip what/how/evidence
        self.assertIn("Sustained decode throughput at a 16,384-token prompt.",
                      stripped)
        self.assertIn("bench/throughput.py, code_python context.", stripped)
        self.assertIn("Deterministic sampling.", stripped)
        # links
        self.assertIn("docs/FINDINGS.md#", stripped)
        self.assertIn("../results/tp_mtp.json", stripped)
        # rationale (none in this fixture, but the structure is there)
        self.assertIn("Live lineup", stripped)
        self.assertIn("Candidate status board", stripped)
        self.assertIn("Settled dead ends", stripped)


class PerformanceTests(FixturesMixin, unittest.TestCase):
    """T031 (SC-008): generation must complete in under 5 s. Guards against
    gross regression, not micro-benchmarks."""

    def test_generation_completes_in_under_5s(self):
        self.write_finding("lineup_quality_8bit.toml", VALID_FINDING)
        import time
        start = time.monotonic()
        r = self.run_cli()
        elapsed = time.monotonic() - start
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertTrue(self.site().is_file())
        self.assertLess(elapsed, 5.0,
                        f"generation took {elapsed:.2f}s (limit 5s)")


if __name__ == "__main__":
    unittest.main()
