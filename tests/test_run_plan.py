"""bench/run_plan.py: writing, showing and clearing a chain's plan."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bench"))

import run_plan  # noqa: E402


class RunPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = run_plan.PLAN
        run_plan.PLAN = Path(self.tmp.name) / ".run_plan.json"

    def tearDown(self):
        run_plan.PLAN = self.saved
        self.tmp.cleanup()

    def test_start_writes_steps(self):
        run_plan.main(["start", "x", "--started", "123",
                       "--step", "a|m1|livecodebench|500", "--step", "b|m2|mbpp,humaneval|100"])
        plan = json.loads(run_plan.PLAN.read_text())
        self.assertEqual(plan["started"], 123)
        self.assertEqual(plan["steps"][1], {"label": "b", "model": "m2",
                                            "suites": ["mbpp", "humaneval"], "samples": 100})

    def test_bad_step_is_rejected(self):
        with self.assertRaises(SystemExit):
            run_plan.main(["start", "x", "--step", "a|m1|lcb"])

    def test_clear(self):
        run_plan.main(["start", "x", "--step", "a|m|s|1"])
        run_plan.main(["clear"])
        self.assertFalse(run_plan.PLAN.exists())
