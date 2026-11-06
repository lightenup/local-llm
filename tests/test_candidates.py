"""dashboard/candidates.py and dashboard/roles.py on fixture files."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dashboard"))

from candidates import build  # noqa: E402
from roles import load_roles  # noqa: E402

CONFIG = """model_list:
  - model_name: Big-8bit:code
    litellm_params:
      model: openai/Big-8bit:code
  - model_name: coder
    litellm_params:
      model: openai/Big-8bit:code
    model_info:
      max_input_tokens: 131072
  - model_name: embed
    litellm_params:
      model: openai/Emb-0.6B
"""


def lcb(model, n, correct, current=True, capped=0):
    q = "Implement the function, do not read from stdin" if current else "Read input from stdin"
    qs = [{"question": q, "finish_reason": "length" if i < capped else "stop"}
          for i in range(n)]
    return {"results": [{"model_id": model, "benchmark": "livecodebench",
                         "accuracy": correct / n, "total": n, "question_results": qs}]}


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, data):
        (self.d / name).write_text(json.dumps(data))

    def test_stages_from_files(self):
        self.write("gate_new.json", [{"model_id": "New-4bit", "verdict": "PASS -- coexists"}])
        self.write("acc_new.json", lcb("New-4bit", 500, 362, capped=132))
        self.write("acc_new_eta.json", lcb("New-4bit", 16, 15))
        c = {x.model: x for x in build(self.d, [])}["New-4bit"]
        self.assertEqual([c.stage(s) for s in ("gate", "accuracy", "task", "decision")],
                         ["pass", "pass", "none", "none"])
        self.assertEqual(c.decision, "open")
        run = c.suites["livecodebench"]
        self.assertEqual((run.total, run.truncated, run.harness), (500, 132, "current"))
        self.assertEqual(c.samples, 1)

    def test_gate_pass_only_when_replacing_is_partial(self):
        self.write("gate_a.json", [{"model_id": "M", "verdict": "reject: cannot serve"}])
        self.write("gate_b.json", [{"model_id": "M", "verdict": "PASS", "replaces": "Old"}])
        self.assertEqual(build(self.d, [])[0].stage("gate"), "partial")

    def test_current_harness_preferred_and_old_flagged(self):
        self.write("acc_old.json", lcb("M", 500, 300, current=False))
        self.write("acc_new.json", lcb("M", 500, 400))
        self.assertEqual(build(self.d, [])[0].suites["livecodebench"].harness, "current")
        (self.d / "acc_new.json").unlink()
        self.assertEqual(build(self.d, [])[0].suites["livecodebench"].harness, "pre-0.7.1")

    def test_role_and_finding_decide(self):
        self.write("acc_a.json", lcb("Big-8bit", 500, 300))
        self.write("acc_b.json", lcb("Gone-6bit-mlx", 500, 200))
        (self.d / "c.yaml").write_text(CONFIG)
        roles = load_roles(self.d / "c.yaml")
        gone = SimpleNamespace(models=["Gone-6bit"], status="retired", section=1, title="t")
        by = {c.model: c for c in build(self.d, [gone], roles)}
        self.assertEqual(by["Big-8bit"].decision, "in lineup")
        self.assertEqual(by["Big-8bit"].roles, ["coder"])
        self.assertEqual(by["Gone-6bit-mlx"].decision, "retired")
        self.assertEqual(by["Gone-6bit-mlx"].stage("decision"), "fail")

    def test_role_named_task_runs_are_ignored(self):
        self.write("task_x.json", {"model": "coder"})
        self.write("task_y.json", {"model": "New-4bit:prof"})
        self.assertEqual([c.model for c in build(self.d, [])], ["New-4bit"])


class RolesTests(unittest.TestCase):
    def test_parses_aliases_only(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "c.yaml").write_text(CONFIG)
            roles = load_roles(Path(d) / "c.yaml")
        self.assertEqual([(r.role, r.model, r.profile, r.max_input_tokens) for r in roles],
                         [("coder", "Big-8bit", "code", 131072),
                          ("embed", "Emb-0.6B", None, None)])

    def test_real_config_has_the_four_chat_roles(self):
        roles = {r.role for r in load_roles(ROOT / "proxy" / "litellm-config.yaml")}
        self.assertTrue({"coder", "reasoner", "fast", "long"} <= roles)


if __name__ == "__main__":
    unittest.main()
