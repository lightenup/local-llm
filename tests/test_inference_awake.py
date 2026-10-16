"""Tests for proxy/inference_awake.py (FINDINGS §37, attempt 3).

- busy counts active + waiting + loading; missing/unknown status is idle
- hold on first busy poll, keep through gaps shorter than linger, release after
- no assertion ever without a busy poll (an idle Mac must sleep per policy)
- Caffeinate really creates and removes a PreventUserIdleSystemSleep assertion
"""

import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "proxy"))
import inference_awake as ia  # noqa: E402


class FakeCaf:
    def __init__(self):
        self.holding = False

    def hold(self):
        if self.holding:
            return False
        self.holding = True
        return True

    def release(self):
        was, self.holding = self.holding, False
        return was


class PolicyTests(unittest.TestCase):

    def test_busy_from_status(self):
        self.assertEqual(ia.busy_from_status(None), 0)
        self.assertEqual(ia.busy_from_status({}), 0)
        self.assertEqual(ia.busy_from_status(
            {"active_requests": 1, "waiting_requests": 2, "models_loading": 1}), 4)

    def test_hold_linger_release(self):
        p, c = ia.Awake(linger=180), FakeCaf()
        busy, idle = {"active_requests": 1}, {"active_requests": 0}
        self.assertEqual(ia.step(p, c, busy, 0), "hold")
        self.assertIsNone(ia.step(p, c, idle, 60))     # gap between agent turns
        self.assertIsNone(ia.step(p, c, busy, 100))    # busy again, still held
        self.assertIsNone(ia.step(p, c, idle, 279))    # 179 s after last busy
        self.assertEqual(ia.step(p, c, idle, 281), "release")
        self.assertFalse(c.holding)

    def test_never_holds_when_idle(self):
        p, c = ia.Awake(linger=180), FakeCaf()
        for t in range(0, 1000, 5):
            ia.step(p, c, {"active_requests": 0}, t)
            ia.step(p, c, None, t)                     # oMLX unreachable
        self.assertFalse(c.holding)

    def test_loading_counts_as_busy(self):
        p, c = ia.Awake(linger=10), FakeCaf()
        self.assertEqual(ia.step(p, c, {"models_loading": 1}, 0), "hold")


def _assertion_present(pid: int) -> bool:
    out = subprocess.run(["pmset", "-g", "assertions"], capture_output=True,
                         text=True).stdout
    return any(f"pid {pid}(caffeinate)" in ln and "PreventUserIdleSystemSleep" in ln
               for ln in out.splitlines())


@unittest.skipUnless(sys.platform == "darwin", "macOS only")
class CaffeinateTests(unittest.TestCase):

    def test_real_assertion_created_and_removed(self):
        c = ia.Caffeinate()
        self.assertTrue(c.hold())
        pid = c.proc.pid
        time.sleep(0.5)
        self.assertTrue(_assertion_present(pid), "caffeinate held no assertion")
        self.assertFalse(c.hold(), "a second hold must not spawn a second child")
        self.assertTrue(c.release())
        time.sleep(0.3)
        self.assertFalse(_assertion_present(pid), "assertion survived release")
        self.assertFalse(c.release())


if __name__ == "__main__":
    unittest.main()
