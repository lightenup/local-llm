"""dashboard/live.py: rate/ETA state and the rendered page, from fake
snapshots. Nothing here touches the network."""

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dashboard"))

import live  # noqa: E402


def snap(at, n, running=True, bid="b1", total=500):
    return {
        "at": at, "errors": [],
        "accuracy": {
            "running": running, "current_model": "m", "current_bench_id": bid if running else None,
            "queue": [],
            "last_progress": {"benchmark": "livecodebench", "model_id": "m",
                              "bench_current": n, "bench_total": total, "phase": "eval"},
        },
        "omlx": {"loaded_models": ["m"], "active_requests": 8, "waiting_requests": 0,
                 "model_memory_used_formatted": "41GB", "model_memory_max_formatted": "108GB",
                 "version": "0.7.3rc1"},
        "queue": {"depth": 0, "current": "m:qwen-code"},
    }


class StateTests(unittest.TestCase):
    def test_rate_needs_two_snapshots(self):
        st = live.update_state({}, snap(1000, 100))
        self.assertEqual(live.rate_eta(st["current"]), (None, None))

    def test_no_rate_from_a_short_window(self):
        st = live.update_state({}, snap(1000, 100))
        st = live.update_state(st, snap(1060, 101))
        self.assertEqual(live.rate_eta(st["current"]), (None, None))

    def test_rate_and_eta(self):
        st = live.update_state({}, snap(1000, 100))
        st = live.update_state(st, snap(1400, 110))
        spq, eta = live.rate_eta(st["current"])
        self.assertEqual(spq, 40.0)
        self.assertEqual(eta, 1400 + 40.0 * 390)

    def test_finished_run_moves_to_history(self):
        st = live.update_state({}, snap(1000, 490))
        st = live.update_state(st, snap(1100, 0, running=False))
        self.assertIsNone(st["current"])
        self.assertEqual(st["history"][0]["last_n"], 490)

    def test_new_bench_id_resets(self):
        st = live.update_state({}, snap(1000, 400, bid="a"))
        st = live.update_state(st, snap(1100, 3, bid="b"))
        self.assertEqual(st["current"]["id"], "b")
        self.assertEqual(st["current"]["first_n"], 3)
        self.assertEqual(st["history"][0]["benchmark"], "livecodebench")

    def test_unreachable_status_keeps_current(self):
        st = live.update_state({}, snap(1000, 100))
        down = {"at": 1100, "errors": ["x"], "accuracy": None, "omlx": None, "queue": None}
        st = live.update_state(st, down)
        self.assertEqual(st["current"]["last_n"], 100)
        self.assertFalse(st.get("history"))


class RenderTests(unittest.TestCase):
    def page(self, *snaps):
        st = {}
        for s in snaps:
            st = live.update_state(st, s)
        return live.render(snaps[-1], st)

    def test_progress_bar_and_link_to_card(self):
        html = self.page(snap(1000, 100), snap(1400, 110))
        self.assertIn('<progress id="cur-p" max="500" value="110">', html)
        self.assertIn('href="index.html#suite-livecodebench"', html)
        self.assertIn("40 s</b> per question", html)

    def test_refresh_and_no_external_refs(self):
        html = self.page(snap(1000, 100))
        self.assertIn('<meta http-equiv="refresh" content="60">', html)
        for pat in ("http://", "https://", 'src="', "fetch(", "XMLHttpRequest"):
            self.assertNotIn(pat, html)

    def test_idle_and_unavailable_states(self):
        self.assertIn("no accuracy suite running", self.page(snap(1000, 0, running=False)))
        down = {"at": 1, "errors": ["llm_queue: URLError"], "accuracy": None,
                "omlx": None, "queue": None}
        html = live.render(down, {})
        self.assertIn("accuracy queue status unavailable", html)
        self.assertIn("llm_queue: URLError", html)

    def test_publishable(self):
        html = self.page(snap(1000, 100), snap(1400, 110))
        self.assertIsNone(re.search(r"192\.168\.|/Users/", html))

    def test_heading_levels_never_skip(self):
        html = self.page(snap(1000, 100), snap(1400, 110))
        prev = 0
        for h in map(int, re.findall(r"<h([1-6])", html)):
            self.assertLessEqual(h, prev + 1)
            prev = h


if __name__ == "__main__":
    unittest.main()


class PlanTests(unittest.TestCase):
    def plan(self, a_done=True):
        steps = [{"label": "a", "model": "m1", "suites": ["livecodebench"], "samples": 500,
                  "result": {"scores": {"livecodebench": 0.724}, "file": "acc_a.json"}
                  if a_done else None},
                 {"label": "b", "model": "m", "suites": ["livecodebench"], "samples": 500,
                  "result": None},
                 {"label": "c", "model": "m2", "suites": ["mbpp", "humaneval"],
                  "samples": 100, "result": None}]
        return {"name": "flint", "started": 900, "steps": steps}

    def test_states_and_question_counts(self):
        st = live.update_state({}, snap(1000, 120))
        rows = live.plan_status(self.plan(), snap(1000, 120)["accuracy"], st["current"])
        self.assertEqual([r["state"] for r in rows], ["done", "running", "pending"])
        self.assertEqual([r["done_q"] for r in rows], [500, 120, 0])
        self.assertEqual(rows[2]["total_q"], 200)

    def test_step_without_result_before_a_running_one_is_missed(self):
        rows = live.plan_status(self.plan(a_done=False), snap(1000, 5)["accuracy"], None)
        self.assertEqual(rows[0]["state"], "missed")

    def test_page_shows_lineup_with_overall_progress(self):
        s1, s2 = snap(1000, 100), snap(1400, 110)
        st = live.update_state(live.update_state({}, s1), s2)
        s2["plan"] = self.plan()
        html = live.render(s2, st)
        self.assertIn("Lineup: flint", html)
        self.assertIn("step 2 of 3", html)
        self.assertIn('<progress id="plan-p" max="1200" value="610">', html)
        self.assertIn("livecodebench <b>72.4</b>", html)
        self.assertIn("lineup ETA", html)
        self.assertIn("plan-pending", html)

    def test_read_plan_ignores_results_older_than_the_plan(self):
        import json, os, tempfile
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "acc_a.json").write_text(json.dumps({"results": [
                {"benchmark": "livecodebench", "accuracy": 0.5}]}))
            os.utime(d / "acc_a.json", (100, 100))
            (d / "plan.json").write_text(json.dumps(
                {"name": "x", "started": 5000, "steps": [
                    {"label": "a", "model": "m", "suites": ["livecodebench"], "samples": 1}]}))
            self.assertIsNone(live.read_plan(d / "plan.json", d)["steps"][0]["result"])
            os.utime(d / "acc_a.json", (6000, 6000))
            self.assertIsNotNone(live.read_plan(d / "plan.json", d)["steps"][0]["result"])


class PlanReviewRegressionTests(unittest.TestCase):
    def step(self, label, model="m", suites=("livecodebench",), samples=500):
        return {"label": label, "model": model, "suites": list(suites),
                "samples": samples, "result": None}

    def test_samples_zero_uses_the_reported_suite_size(self):
        plan = {"steps": [self.step("a", samples=0)]}
        r = live.plan_status(plan, snap(1000, 120, total=1054)["accuracy"], None)[0]
        self.assertEqual((r["done_q"], r["total_q"]), (120, 1054))

    def test_small_suite_reaches_its_own_total(self):
        plan = {"steps": [self.step("a", suites=("humaneval",), samples=500)]}
        s = snap(1000, 164, total=164)
        s["accuracy"]["last_progress"]["benchmark"] = "humaneval"
        r = live.plan_status(plan, s["accuracy"], None)[0]
        self.assertEqual((r["done_q"], r["total_q"]), (164, 164))

    def test_only_the_first_identical_step_is_running(self):
        plan = {"steps": [self.step("a"), self.step("b")]}
        rows = live.plan_status(plan, snap(1000, 50)["accuracy"], None)
        self.assertEqual([r["state"] for r in rows], ["running", "pending"])

    def test_malformed_plans_do_not_raise(self):
        import json, tempfile
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            for body in ("[1, 2]", '{"steps": "x"}',
                         '{"started": 1, "steps": [{"label": "a"}, 7]}', "{not json"):
                (d / "p.json").write_text(body)
                plan = live.read_plan(d / "p.json", d)
                self.assertTrue(plan is None or plan["steps"] == [])
                if plan:
                    live.render({**snap(1000, 1), "plan": plan}, {})
