"""Tests for proxy/inference_awake.py (FINDINGS §37, attempt 3).

- busy counts active + waiting + loading; missing/unknown status is idle
- hold on first busy poll, keep through gaps shorter than linger, release after
- no assertion ever without a busy poll or the home rule
- home rule (§43): AC + default route in HOME_LAN_SUBNET holds regardless of
  traffic, and leaving home releases (after the linger if it was busy)
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


class HomeRuleTests(unittest.TestCase):

    BATT_AC = "Now drawing from 'AC Power'\n -InternalBattery-0 100%; charged;"
    BATT_BAT = "Now drawing from 'Battery Power'\n -InternalBattery-0 80%;"
    ROUTE = "   route to: default\n  gateway: 10.1.2.1\n  interface: en0\n"

    def fake(self, batt, route=ROUTE, addr="10.1.2.50\n"):
        def run(argv):
            if "pmset" in argv[0]:
                return batt
            if "route" in argv[0]:
                return route
            if argv[-1] == "en0":
                return addr
            return ""
        return run

    def test_on_home_power(self):
        net = "10.1.2.0/24"
        self.assertTrue(ia.on_home_power(net, self.fake(self.BATT_AC)))
        self.assertFalse(ia.on_home_power(net, self.fake(self.BATT_BAT)))
        self.assertFalse(ia.on_home_power(net, self.fake(self.BATT_AC,
                                                         addr="172.20.0.4\n")))
        self.assertFalse(ia.on_home_power(net, self.fake(self.BATT_AC, route="")))
        self.assertFalse(ia.on_home_power(net, self.fake(self.BATT_AC, addr="")))
        self.assertFalse(ia.on_home_power("", self.fake(self.BATT_AC)))

    def test_home_holds_without_traffic_and_releases_on_leaving(self):
        p, c = ia.Awake(linger=180), FakeCaf()
        idle = {"active_requests": 0}
        self.assertEqual(ia.step(p, c, idle, 0, home=True), "hold")
        for t in range(5, 3600, 5):
            self.assertIsNone(ia.step(p, c, idle, t, home=True))
        self.assertTrue(c.holding)
        self.assertEqual(ia.step(p, c, idle, 3600, home=False), "release")

    def test_leaving_home_mid_request_keeps_linger(self):
        p, c = ia.Awake(linger=180), FakeCaf()
        ia.step(p, c, {"active_requests": 1}, 0, home=True)
        self.assertIsNone(ia.step(p, c, {"active_requests": 0}, 100, home=False))
        self.assertEqual(ia.step(p, c, {"active_requests": 0}, 181, home=False),
                         "release")


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
