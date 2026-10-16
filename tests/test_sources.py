"""Tests for dashboard/sources.py — the path-expression resolver (research.md R3).

Run: python3 -m unittest discover -s tests -v
These tests are the red half of the T002/T003 pair; they must fail before
sources.py exists and pass once it does (constitution VII).
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))

import sources  # noqa: E402

RESULTS = Path(__file__).resolve().parents[1] / "results"


def res(file_name: str, path: str):
    """Resolve a path against a real file under results/."""
    return sources.resolve(RESULTS / file_name, path)


class DottedKeyTests(unittest.TestCase):
    def test_nested_dotted_keys(self):
        r = res("ctx_mtp.json", "raw.result.verified_tokens")
        self.assertEqual(r.outcome, "value")
        self.assertEqual(r.value, 262144)

    def test_top_level_scalar(self):
        r = res("ctx_mtp.json", "target")
        self.assertEqual(r.outcome, "value")
        self.assertEqual(r.value, 262144)


class IndexTests(unittest.TestCase):
    def test_integer_index(self):
        r = res("tp_mtp.json", "summary[0].pp")
        self.assertEqual(r.outcome, "value")
        self.assertEqual(r.value, 1024)


class PredicateTests(unittest.TestCase):
    def test_numeric_predicate(self):
        r = res("tp_mtp.json", "summary[?pp=16384].decode_tps")
        self.assertEqual(r.outcome, "value")
        self.assertEqual(r.value, 34.0)

    def test_string_predicate(self):
        r = res("acc_gemma4_code.json", "results[?benchmark=livecodebench].accuracy")
        self.assertEqual(r.outcome, "value")
        self.assertEqual(r.value, 0.734)

    def test_last_match_wins(self):
        # older acc_*.json files append the run's own records after the
        # server-side history, so the LAST match is the run's own record
        # (bench/report.py::own_results). Prove the resolver picks the last.
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "dup.json"
            p.write_text(json.dumps(
                {"rows": [{"k": 1, "v": "first"},
                          {"k": 1, "v": "second"},
                          {"k": 1, "v": "last"}]}))
            r = sources.resolve(p, "rows[?k=1].v")
            self.assertEqual(r.outcome, "value")
            self.assertEqual(r.value, "last")

    def test_predicate_no_match_is_stale(self):
        r = res("tp_mtp.json", "summary[?pp=99999].decode_tps")
        self.assertEqual(r.outcome, "stale")
        self.assertIn("pp=99999", r.reason)


class ChainedTests(unittest.TestCase):
    def test_numeric_dict_key(self):
        # by_length is a dict keyed by the string "65536"; a bare numeric
        # segment must be treated as a dict key, not a list index.
        r = res("longctx_gemma4.json", "summary.by_length.65536.verified_prompt_tokens")
        self.assertEqual(r.outcome, "value")
        self.assertEqual(r.value, 65225)


class StaleOutcomeTests(unittest.TestCase):
    def test_missing_file(self):
        r = sources.resolve(RESULTS / "does_not_exist.json", "a.b")
        self.assertEqual(r.outcome, "stale")
        self.assertIn("does_not_exist.json", r.reason)

    def test_unresolvable_path_names_path(self):
        r = res("tp_mtp.json", "summary[?pp=16384].no_such_field")
        self.assertEqual(r.outcome, "stale")
        self.assertIn("no_such_field", r.reason)

    def test_non_scalar_is_stale(self):
        # summary[0] resolves to a dict, not a scalar.
        r = res("tp_mtp.json", "summary[0]")
        self.assertEqual(r.outcome, "stale")

    def test_null_is_stale(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "null.json"
            p.write_text(json.dumps({"a": {"b": None}}))
            r = sources.resolve(p, "a.b")
            self.assertEqual(r.outcome, "stale")


class SyntaxTests(unittest.TestCase):
    def test_unterminated_bracket_raises(self):
        with self.assertRaises(sources.PathSyntaxError):
            sources.parse_path("summary[?pp=16384")

    def test_empty_path_raises(self):
        with self.assertRaises(sources.PathSyntaxError):
            sources.parse_path("")

    def test_bad_predicate_raises(self):
        with self.assertRaises(sources.PathSyntaxError):
            sources.parse_path("summary[?pp]")

    def test_valid_path_parses(self):
        steps = sources.parse_path("summary[?pp=16384].decode_tps")
        self.assertEqual(len(steps), 2)


class FileRefTests(unittest.TestCase):
    def test_dotdot_escape_rejected(self):
        with self.assertRaises(ValueError):
            sources.check_file_ref("../etc/passwd")

    def test_absolute_rejected(self):
        with self.assertRaises(ValueError):
            sources.check_file_ref("/etc/passwd")

    def test_plain_name_ok(self):
        sources.check_file_ref("tp_mtp.json")  # must not raise

    def test_subdir_ok(self):
        sources.check_file_ref("sub/tp_mtp.json")  # inside results/, allowed


if __name__ == "__main__":
    unittest.main()
