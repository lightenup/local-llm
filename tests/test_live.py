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
