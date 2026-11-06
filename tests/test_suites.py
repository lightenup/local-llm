"""Benchmark suite cards: loader validation, run summaries, rendered view."""

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dashboard"))

import render  # noqa: E402
from suites import load_suites, run_summaries  # noqa: E402

GOOD = """
[[suite]]
id = "humaneval"
name = "HumanEval"
group = "code"
measures = "Writes functions."
size = "164 problems"
how_we_run = "All 164."
links = [{ label = "paper", url = "https://arxiv.org/abs/2107.03374" }]
findings = [41]

[[suite]]
id = "taskbench"
name = "taskbench"
group = "harness"
harness = true
measures = "Our jobs."
size = "five sub-suites"
how_we_run = "Through roles."
links = [{ label = "script", url = "../bench/taskbench.py" }]
"""


class LoaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, body: str):
        (self.dir / "s.toml").write_text(body)
        return load_suites(self.dir)

    def test_absent_dir_is_empty_not_error(self):
        self.assertEqual(load_suites(self.dir / "nope"), ([], []))

    def test_valid_file(self):
        suites, problems = self.load(GOOD)
        self.assertEqual(problems, [])
        self.assertEqual([s.id for s in suites], ["humaneval", "taskbench"])
        self.assertTrue(suites[1].harness)
        self.assertEqual(suites[0].findings, (41,))

    def test_missing_field_names_it(self):
        _, problems = self.load(GOOD.replace('size = "164 problems"\n', ""))
        self.assertEqual([p.field for p in problems], ["suite[0].size"])

    def test_unknown_group(self):
        _, problems = self.load(GOOD.replace('group = "code"', 'group = "vibes"'))
        self.assertIn("unknown group", problems[0].message)

    def test_duplicate_id(self):
        _, problems = self.load(GOOD.replace('id = "taskbench"', 'id = "humaneval"'))
        self.assertIn("duplicate", problems[0].message)

    def test_link_needs_url(self):
        _, problems = self.load(GOOD.replace(', url = "../bench/taskbench.py"', ""))
        self.assertEqual(problems[0].field, "suite[1].links[0]")

    def test_repo_file_is_valid(self):
        suites, problems = load_suites(ROOT / "findings" / "suites")
        self.assertEqual(problems, [])
        ids = {s.id for s in suites}
        for b in ("humaneval", "mbpp", "livecodebench", "mmlu_pro", "gsm8k", "mathqa",
                  "mmlu", "arc_challenge", "hellaswag", "winogrande", "truthfulqa",
                  "safetybench", "bbq"):
            self.assertIn(b, ids)


class SummaryTests(unittest.TestCase):
    def test_counts_runs_and_models(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            for name, model in (("acc_a.json", "m1"), ("acc_b.json", "m2")):
                (d / name).write_text(json.dumps({"results": [
                    {"benchmark": "humaneval", "model_id": model}]}))
            (d / "acc_bad.json").write_text("{not json")
            (d / "tp_x.json").write_text(json.dumps({"results": [
                {"benchmark": "humaneval", "model_id": "ignored"}]}))
            s = run_summaries(d)
            self.assertEqual(s["humaneval"].runs, 2)
            self.assertEqual(s["humaneval"].models, {"m1", "m2"})


class RenderTests(unittest.TestCase):
    def setUp(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "s.toml").write_text(GOOD)
            self.suites, _ = load_suites(Path(d))
        self.html = render.render_page([], {}, {}, 0, "2026-01-01 00:00:00 UTC",
                                       self.suites, {})

    def test_section_and_heading(self):
        self.assertIn('<section id="benchmarks" aria-labelledby="benchmarks-h">', self.html)
        self.assertIn('<h2 id="benchmarks-h" tabindex="-1">Benchmarks</h2>', self.html)

    def test_card_has_anchor_and_links(self):
        card = re.search(r'<article class="suite" id="suite-humaneval".*?</article>',
                         self.html, re.S).group(0)
        self.assertIn('href="https://arxiv.org/abs/2107.03374"', card)
        self.assertIn("FINDINGS §41", card)
        self.assertIn("none yet", card)

    def test_harness_card_has_no_run_count(self):
        card = re.search(r'id="suite-taskbench".*?</article>', self.html, re.S).group(0)
        self.assertNotIn("Stored runs", card)

    def test_empty_state(self):
        html = render.render_page([], {}, {}, 0, "x")
        self.assertIn("no benchmark suites described yet", html)

    def test_heading_levels_never_skip(self):
        prev = 0
        for h in map(int, re.findall(r"<h([1-6])", self.html)):
            self.assertLessEqual(h, prev + 1)
            prev = h


if __name__ == "__main__":
    unittest.main()
