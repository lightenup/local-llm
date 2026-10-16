"""Tests for dashboard/findings.py — findings loading and validation (FR-007).

Run: python3 -m unittest discover -s tests -v
Red half of the T004/T005 pair (constitution VII): these must fail before
findings.py exists and pass once it does.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))

import findings  # noqa: E402


def write_toml(d: Path, name: str, body: str) -> Path:
    p = d / name
    p.write_text(body)
    return p


VALID = """\
id      = "lineup_quality_8bit"
title   = "Qwen3.8-27B-8bit ships the quality tier"
status  = "shipped"
models  = ["Qwen3.8-27B-8bit"]
section = 19

[[metrics]]
name      = "decode @ 16k prompt"
unit      = "tok/s"
direction = "higher"
headline  = true
tooltip_what     = "Sustained decode throughput at a 16,384-token prompt."
tooltip_how      = "bench/throughput.py, code_python context, 128-token generations."
tooltip_evidence = "Deterministic sampling; 4 prompt lengths per run."
source_file = "tp_baseline.json"
source_path  = "summary[?pp=16384].decode_tps"
"""


def load(d: Path):
    return findings.load_findings(d)


class ParseErrorTests(unittest.TestCase):
    def test_toml_syntax_error_reported_with_line_number(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            write_toml(d, "bad.toml", 'id = "bad"\ntitle = "unterminated\n')
            _, problems = load(d)
            self.assertTrue(problems)
            text = " | ".join(str(p) for p in problems)
            self.assertIn("bad.toml", text)
            self.assertIn("line 2", text)


class RequiredFieldTests(unittest.TestCase):
    def test_missing_required_field_is_a_problem(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            body = VALID.replace('title   = "Qwen3.8-27B-8bit ships the quality tier"\n', "")
            write_toml(d, "lineup_quality_8bit.toml", body)
            _, problems = load(d)
            text = " | ".join(str(p) for p in problems)
            self.assertIn("title", text)

    def test_wrong_type_is_a_problem(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            body = VALID.replace('section = 19', 'section = "19"')
            write_toml(d, "lineup_quality_8bit.toml", body)
            _, problems = load(d)
            text = " | ".join(str(p) for p in problems)
            self.assertIn("section", text)

    def test_absent_metrics_list_is_valid(self):
        # data-model.md: metrics is "yes (may be empty)" — a finding closed by
        # argument or by the measurement named in the rationale carries no
        # [[metrics]] block and renders with a "no measurement" marker
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            body = VALID.replace("[[metrics]]", "# no metrics at all")
            write_toml(d, "lineup_quality_8bit.toml", body)
            findings, problems = load(d)
            self.assertEqual(problems, [])
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].metrics, [])

    def test_metrics_wrong_type_is_a_problem(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            body = VALID.replace("[[metrics]]", 'metrics = "nope"')
            write_toml(d, "lineup_quality_8bit.toml", body)
            _, problems = load(d)
            text = " | ".join(str(p) for p in problems)
            self.assertIn("metrics", text)


class EnumTests(unittest.TestCase):
    def test_bad_status_is_a_problem(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            body = VALID.replace('status  = "shipped"', 'status  = "bogus"')
            write_toml(d, "lineup_quality_8bit.toml", body)
            _, problems = load(d)
            text = " | ".join(str(p) for p in problems)
            self.assertIn("status", text)

    def test_bad_direction_is_a_problem(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            body = VALID.replace('direction = "higher"', 'direction = "sideways"')
            write_toml(d, "lineup_quality_8bit.toml", body)
            _, problems = load(d)
            text = " | ".join(str(p) for p in problems)
            self.assertIn("direction", text)


class IdTests(unittest.TestCase):
    def test_bad_id_shape_is_a_problem(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            body = VALID.replace('id      = "lineup_quality_8bit"', 'id      = "Bad Id"')
            write_toml(d, "lineup_quality_8bit.toml", body)
            _, problems = load(d)
            self.assertTrue(problems)

    def test_id_must_equal_filename_stem(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            write_toml(d, "other_name.toml", VALID)
            _, problems = load(d)
            text = " | ".join(str(p) for p in problems)
            self.assertIn("id", text)

    def test_duplicate_id_layer_wide_is_a_problem(self):
        # two files, identical content incl. id; the second file's stem
        # mismatch is reported first, but the id clash must also surface
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            write_toml(d, "lineup_quality_8bit.toml", VALID)
            write_toml(d, "lineup_fast_oq4e.toml", VALID)
            _, problems = load(d)
            self.assertGreaterEqual(len(problems), 2)
            text = " | ".join(str(p) for p in problems)
            self.assertIn("duplicate", text.lower())


class SectionTests(unittest.TestCase):
    def test_section_below_one_is_a_problem(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            body = VALID.replace("section = 19", "section = 0")
            write_toml(d, "lineup_quality_8bit.toml", body)
            _, problems = load(d)
            text = " | ".join(str(p) for p in problems)
            self.assertIn("section", text)


class SourceRefTests(unittest.TestCase):
    def test_source_file_escaping_results_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            body = VALID.replace('source_file = "tp_baseline.json"',
                                 'source_file = "../etc/passwd"')
            write_toml(d, "lineup_quality_8bit.toml", body)
            _, problems = load(d)
            self.assertTrue(problems)

    def test_unparsable_source_path_is_a_problem(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            body = VALID.replace('source_path  = "summary[?pp=16384].decode_tps"',
                                 'source_path  = "summary[?pp=16384"')
            write_toml(d, "lineup_quality_8bit.toml", body)
            _, problems = load(d)
            self.assertTrue(problems)

    def test_unresolvable_but_wellformed_path_is_NOT_a_problem(self):
        # stale is a runtime condition (exit 2), not a validation failure
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            body = VALID.replace('source_path  = "summary[?pp=16384].decode_tps"',
                                 'source_path  = "no_such_key.deep"')
            write_toml(d, "lineup_quality_8bit.toml", body)
            _, problems = load(d)
            self.assertEqual(problems, [])


class MetricTests(unittest.TestCase):
    def test_duplicate_metric_names_within_finding_is_a_problem(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            body = VALID + "\n[[metrics]]\n" + VALID[VALID.index("[[metrics]]"):]
            write_toml(d, "lineup_quality_8bit.toml", body)
            _, problems = load(d)
            text = " | ".join(str(p) for p in problems)
            self.assertIn("decode @ 16k prompt", text)

    def test_missing_tooltip_field_is_a_problem(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            body = VALID.replace('tooltip_how      = "bench/throughput.py, code_python context, 128-token generations."\n', "")
            write_toml(d, "lineup_quality_8bit.toml", body)
            _, problems = load(d)
            text = " | ".join(str(p) for p in problems)
            self.assertIn("tooltip_how", text)


class AggregationTests(unittest.TestCase):
    def test_empty_findings_dir_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            d.mkdir(exist_ok=True)
            f, problems = load(d)
            self.assertEqual(f, [])
            self.assertEqual(problems, [])

    def test_single_run_aggregates_all_problems(self):
        # one file with two distinct problems -> both reported, not just the first
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            body = VALID.replace('status  = "shipped"', 'status  = "bogus"')
            body = body.replace("section = 19", "section = 0")
            write_toml(d, "lineup_quality_8bit.toml", body)
            _, problems = load(d)
            self.assertGreaterEqual(len(problems), 2)
            text = " | ".join(str(p) for p in problems)
            self.assertIn("status", text)
            self.assertIn("section", text)


class LoadSectionsTests(unittest.TestCase):
    def test_parses_numbered_headings_to_slug_anchors(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            p = d / "FINDINGS.md"
            p.write_text(
                "# Findings\n\n"
                "## 1. Starting state was untuned\n\n"
                "text\n\n"
                "## 4. MTP speculative decode — the big win, shipped\n\n"
                "text\n\n"
                "## 19. The lineup: 8-bit for quality, oQ4e for speed\n\n"
                "text\n")
            sections = findings.load_sections(p)
            self.assertEqual(sections[1][1], "1-starting-state-was-untuned")
            self.assertEqual(sections[4][0], "MTP speculative decode — the big win, shipped")
            self.assertEqual(sections[19][1], "19-the-lineup-8-bit-for-quality-oq4e-for-speed")

    def test_real_findings_md_has_expected_sections(self):
        real = Path(__file__).resolve().parents[1] / "docs" / "FINDINGS.md"
        sections = findings.load_sections(real)
        self.assertIn(4, sections)
        self.assertIn(19, sections)
        self.assertIn(30, sections)
        self.assertTrue(sections[19][1].startswith("19-"))


if __name__ == "__main__":
    unittest.main()
