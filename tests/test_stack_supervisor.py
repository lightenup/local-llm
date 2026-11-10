"""Tests for proxy/stack_supervisor.py (FINDINGS §43): the policy, no I/O.

- healthy: no action
- LiteLLM down, Docker socket present: `up`
- socket missing: `open_docker`, then `up`
- a failed `up` backs off 60 s, doubling to 10 min; success resets it
- a successful `up` waits out a grace period before the next one
- oMLX down for more than 5 min: `open_omlx`, at most every 10 min
- doctor kicked once, 5 min after LiteLLM and oMLX first both answer
- the state file keeps counters across restarts
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "proxy"))
import stack_supervisor as ss  # noqa: E402


class PolicyTests(unittest.TestCase):

    def test_healthy_no_action(self):
        s = ss.Supervisor()
        for t in range(0, 290, 60):
            self.assertEqual(s.decide(True, True, True, t), [])

    def test_unhealthy_socket_present_runs_up(self):
        self.assertEqual(ss.Supervisor().decide(False, True, True, 0), ["up"])

    def test_socket_missing_opens_docker_then_up(self):
        self.assertEqual(ss.Supervisor().decide(False, False, True, 0),
                         ["open_docker", "up"])

    def test_backoff_after_failed_up(self):
        s = ss.Supervisor(backoff_min=60, backoff_max=600)
        delays, t = [], 0.0
        for _ in range(6):
            self.assertEqual(s.decide(False, True, True, t), ["up"])
            s.record("up", False, t)
            nxt = t
            while not s.decide(False, True, True, nxt):
                nxt += 1
            delays.append(nxt - t)
            t = nxt
        self.assertEqual(delays, [60, 120, 240, 480, 600, 600])

    def test_success_resets_backoff_and_waits_grace(self):
        s = ss.Supervisor(backoff_min=60, up_grace=180)
        s.record("up", False, 0)
        s.record("up", False, 60)          # delay is now 240
        s.record("up", True, 200)
        self.assertEqual(s.delay, 60)
        self.assertEqual(s.decide(False, True, True, 379), [])   # in grace
        self.assertEqual(s.decide(False, True, True, 380), ["up"])

    def test_healthy_clears_backoff(self):
        s = ss.Supervisor()
        s.record("up", False, 0)
        s.decide(True, True, True, 10)
        self.assertEqual(s.decide(False, True, True, 20), ["up"])

    def test_omlx_open_after_five_minutes_then_throttled(self):
        s = ss.Supervisor(omlx_down_after=300, omlx_reopen=600, doctor_after=1e9)
        self.assertEqual(s.decide(True, True, False, 0), [])
        self.assertEqual(s.decide(True, True, False, 299), [])
        self.assertEqual(s.decide(True, True, False, 300), ["open_omlx"])
        s.record("open_omlx", True, 300)
        self.assertEqual(s.decide(True, True, False, 899), [])
        self.assertEqual(s.decide(True, True, False, 900), ["open_omlx"])

    def test_omlx_recovery_resets_timer(self):
        s = ss.Supervisor(omlx_down_after=300, doctor_after=1e9)
        s.decide(True, True, False, 0)
        s.decide(True, True, True, 200)
        self.assertEqual(s.decide(True, True, False, 400), [])

    def test_doctor_once_after_healthy_for_five_minutes(self):
        s = ss.Supervisor(doctor_after=300)
        self.assertEqual(s.decide(True, True, True, 0), [])
        self.assertEqual(s.decide(False, True, True, 120), ["up"])  # resets
        s.record("up", True, 120)
        self.assertEqual(s.decide(True, True, True, 180), [])
        self.assertEqual(s.decide(True, True, True, 479), [])
        self.assertEqual(s.decide(True, True, True, 480), ["doctor"])
        s.record("doctor", True, 480)
        self.assertEqual(s.decide(True, True, True, 10_000), [])


class StateTests(unittest.TestCase):

    def test_counters_survive_restart(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            st = ss.State(p)
            st.action("up", True, "rc=0", 100.0)
            st.save(101.0)
            st2 = ss.State(p)
            st2.action("up", False, "rc=1", 200.0)
            st2.save(201.0)
            data = json.loads(p.read_text())
            self.assertEqual(data["actions_total"]["up"], 2)
            self.assertEqual(data["actions_total"]["open_docker"], 0)
            self.assertEqual(data["last_action"]["at"], 200.0)
            self.assertFalse(data["last_action"]["ok"])
            self.assertEqual(data["heartbeat"], 201.0)


if __name__ == "__main__":
    unittest.main()
