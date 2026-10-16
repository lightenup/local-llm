"""Tests for the deep-link contract (FR-014, US6) — T025 red half.

Every metric with a source links to both its FINDINGS.md section anchor and
its raw results file. Anchors are computed from the real docs/FINDINGS.md
`## N. Title` headings (GitHub slug rules, research.md R6). Links are
relative paths that resolve from file://. A finding with no measurement is
marked "no measurement" and offers only the documentation link. A recorded
section_title that no longer matches the heading at that number produces a
stderr warning and leaves the exit code unchanged.

Run: python3 -m unittest discover -s tests -v
"""

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
REAL_FINDINGS_MD = ROOT / "docs" / "FINDINGS.md"

# real headings from docs/FINDINGS.md, asserted against in the tests
REAL_HEADING_4 = "MTP speculative decode — the big win, shipped"
REAL_HEADING_19 = "The lineup: 8-bit for quality, oQ4e for speed"
REAL_HEADING_30 = "gemma-4-31B wins both measured axes, and costs 1.8x the decode to do it"

FINDINGS_MD = f"""# Findings

## 4. {REAL_HEADING_4}

text

## 19. {REAL_HEADING_19}

text

## 30. {REAL_HEADING_30}

text
"""


def slug(title: str) -> str:
    """GitHub anchor slug (same rules as dashboard/findings.py)."""
    import re as _re
    s = title.lower()
    s = _re.sub(r"[^\w\s-]", "", s, flags=_re.UNICODE)
    s = _re.sub(r"[\s]+", "-", s.strip())
    return s


ANCHOR_4 = slug(f"4. {REAL_HEADING_4}")
ANCHOR_19 = slug(f"19. {REAL_HEADING_19}")
ANCHOR_30 = slug(f"30. {REAL_HEADING_30}")

FINDING_S19 = """\
id      = "lineup_quality_8bit"
title   = "Qwen3.8-27B-8bit ships the quality tier"
status  = "shipped"
models  = ["Qwen3.8-27B-8bit"]
section = 19
section_title = "{h19}"

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
""".format(h19=REAL_HEADING_19)

FINDING_S4_NO_MEASUREMENT = """\
id      = "mtp_shipped"
title   = "MTP speculative decode ships"
status  = "shipped"
models  = ["Qwen3.8-27B-8bit"]
section = 4
section_title = "{h4}"
""".format(h4=REAL_HEADING_4)

FINDING_S30 = """\
id      = "gemma_additive"
title   = "gemma-4-31B wins both measured axes"
status  = "shipped"
models  = ["gemma-4-31B"]
section = 30
section_title = "{h30}"

[[metrics]]
name      = "decode @ 4k prompt"
unit      = "tok/s"
direction = "higher"
headline  = true
tooltip_what     = "Sustained decode throughput at a 4,096-token prompt."
tooltip_how      = "bench/throughput.py, code_python context."
tooltip_evidence = "Deterministic sampling."
source_file = "tp_gemma4.json"
source_path  = "summary[?pp=4096].decode_tps"
""".format(h30=REAL_HEADING_30)


class FixturesMixin:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.findings = self.repo / "findings"
        self.findings.mkdir()
        self.results = self.repo / "results"
        self.results.mkdir()
        for name in ("tp_mtp.json", "tp_gemma4.json"):
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

    def html(self) -> str:
        return self.site().read_text()


class AnchorTests(FixturesMixin, unittest.TestCase):
    """Anchors are computed from the real FINDINGS.md headings (research.md R6)."""

    def test_anchors_match_github_slug_of_real_headings(self):
        # sanity: the slugs the tests assert must be the GitHub slugs of the
        # real headings, so a heading rename in FINDINGS.md breaks the tests
        self.assertEqual(ANCHOR_4, slug(f"4. {REAL_HEADING_4}"))
        self.assertIn("mtp-speculative-decode", ANCHOR_4)
        self.assertEqual(ANCHOR_19, slug(f"19. {REAL_HEADING_19}"))
        self.assertIn("the-lineup-8-bit-for-quality-oq4e-for-speed", ANCHOR_19)
        self.assertEqual(ANCHOR_30, slug(f"30. {REAL_HEADING_30}"))
        self.assertIn("gemma-4-31b-wins-both-measured-axes", ANCHOR_30)

    def test_section_links_use_real_heading_anchors(self):
        self.write_finding("lineup_quality_8bit.toml", FINDING_S19)
        self.write_finding("gemma_additive.toml", FINDING_S30)
        self.write_finding("mtp_shipped.toml", FINDING_S4_NO_MEASUREMENT)
        r = self.run_cli()
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        html = self.html()
        self.assertIn(f'docs/FINDINGS.md#{ANCHOR_19}', html)
        self.assertIn(f'docs/FINDINGS.md#{ANCHOR_30}', html)
        self.assertIn(f'docs/FINDINGS.md#{ANCHOR_4}', html)


class RawFileLinkTests(FixturesMixin, unittest.TestCase):
    """Every metric with a source links to its raw results file (FR-014)."""

    def test_metric_links_to_raw_results_file(self):
        self.write_finding("lineup_quality_8bit.toml", FINDING_S19)
        r = self.run_cli()
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        html = self.html()
        self.assertIn("../results/tp_mtp.json", html)

    def test_raw_link_resolves_from_file_protocol(self):
        # relative link from site/index.html to results/ must resolve
        self.write_finding("lineup_quality_8bit.toml", FINDING_S19)
        r = self.run_cli()
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        html = self.html()
        link = "../results/tp_mtp.json"
        self.assertIn(f'href="{link}"', html)
        # and the target exists relative to the site directory
        target = (self.repo / "site" / link).resolve()
        self.assertTrue(target.is_file())


class NoMeasurementTests(FixturesMixin, unittest.TestCase):
    """A finding with no measurement is marked and offers only the doc link."""

    def test_no_measurement_marker_and_doc_link_only(self):
        self.write_finding("mtp_shipped.toml", FINDING_S4_NO_MEASUREMENT)
        r = self.run_cli()
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        html = self.html()
        self.assertIn("no measurement", html)
        self.assertIn(f'docs/FINDINGS.md#{ANCHOR_4}', html)
        # no raw-results link for this finding (it has no metrics)
        self.assertNotIn("../results/", html)


class SectionTitleMismatchTests(FixturesMixin, unittest.TestCase):
    """A stale section_title warns on stderr but does not change the exit code."""

    def test_mismatch_warns_without_changing_exit_code(self):
        body = FINDING_S19.replace(
            f'section_title = "{REAL_HEADING_19}"',
            'section_title = "Some old title that no longer matches"')
        self.write_finding("lineup_quality_8bit.toml", body)
        r = self.run_cli()
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertIn("warning", r.stderr)
        self.assertIn("lineup_quality_8bit", r.stderr)
        # the site is still written
        self.assertTrue(self.site().is_file())


if __name__ == "__main__":
    unittest.main()
