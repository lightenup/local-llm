#!/usr/bin/env python3
"""Hold a macOS sleep assertion while on AC at home, or while oMLX is busy.

FINDINGS §37: every thermal emergency began with the Mac entering Idle or
Clamshell Sleep while oMLX was mid-generation. `pmset -c sleep 0` was the
first fix. It was reverted to the CIS value (15) at 13:16 on two consecutive
days, which is consistent with an Intune compliance script, so a global
setting cannot be relied on. This watcher addresses the cause directly: no
sleep *while inferring*. When idle, the Mac sleeps per policy, like with any
other workload that holds an assertion (a call, a build).

Since §43 the primary rule is explicit: while the Mac is on AC power *and*
its default-route interface has an IPv4 address inside HOME_LAN_SUBNET (from
the environment or .env.local), hold the assertion regardless of traffic. The
homelab gateway depends on this Mac, and the old traffic-driven hold made a
broken stack look like a sleeping Mac: no requests reached oMLX, so nothing
held the Mac awake, so monitoring read the outage as "asleep". The condition
is re-evaluated every poll, so unplugging or leaving home releases the hold.
The SSID is not used; macOS restricts reading it. With HOME_LAN_SUBNET unset
the rule is off.

The busy rule stays as the fallback off the home LAN. Every --interval
seconds it polls oMLX's GET /api/status. When
active_requests + waiting_requests + models_loading > 0 it starts
`caffeinate -i -s` (PreventUserIdleSystemSleep, plus PreventSystemSleep on
AC). The assertion is released --linger seconds after the last active poll,
so the gaps between agent turns do not flap it and the SoC can cool with the
fans running before sleep is allowed. If oMLX is unreachable, that counts as
idle.

Not covered: Clamshell Sleep (closing the lid) overrides these assertions on a
Mac without an external display. `doctor.py --layer host` checks the lid.

    python3 proxy/inference_awake.py                 # defaults: 5 s poll, 180 s linger
    HOME_LAN_SUBNET=<lan-subnet> python3 proxy/inference_awake.py
    pmset -g assertions | grep caffeinate            # see it while a request runs
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import subprocess
import time
import urllib.request
from pathlib import Path

log = logging.getLogger("inference_awake")
OMLX_SETTINGS = Path.home() / ".omlx" / "settings.json"
REPO = Path(__file__).resolve().parent.parent


def home_subnet() -> str:
    """HOME_LAN_SUBNET from the environment, else .env.local; "" when unset."""
    if os.environ.get("HOME_LAN_SUBNET"):
        return os.environ["HOME_LAN_SUBNET"]
    try:
        for line in (REPO / ".env.local").read_text().splitlines():
            k, sep, v = line.partition("=")
            if sep and k.strip() == "HOME_LAN_SUBNET":
                return v.strip().strip("\"'")
    except OSError:
        pass
    return ""


def _out(argv: list[str]) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True,
                              timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def on_ac(batt: str) -> bool:
    """`pmset -g batt` output says the Mac is drawing from AC."""
    return "'AC Power'" in batt


def default_interface(route: str) -> str:
    """The interface named by `route -n get default`, or ""."""
    for line in route.splitlines():
        k, _, v = line.strip().partition(":")
        if k == "interface":
            return v.strip()
    return ""


def in_subnet(addr: str, subnet: str) -> bool:
    try:
        return ipaddress.ip_address(addr.strip()) in ipaddress.ip_network(
            subnet, strict=False)
    except ValueError:
        return False


def on_home_power(subnet: str, run=_out) -> bool:
    """True when on AC power and the default route's IPv4 is inside subnet."""
    if not subnet or not on_ac(run(["/usr/bin/pmset", "-g", "batt"])):
        return False
    iface = default_interface(run(["/sbin/route", "-n", "get", "default"]))
    if not iface:
        return False
    return in_subnet(run(["/usr/sbin/ipconfig", "getifaddr", iface]), subnet)


def api_key() -> str:
    """OMLX_API_KEY, else the key oMLX stores locally (same as omlx_client)."""
    env = os.environ.get("OMLX_API_KEY")
    if env:
        return env
    try:
        return (json.loads(OMLX_SETTINGS.read_text()).get("auth") or {}).get(
            "api_key", "")
    except (OSError, ValueError):
        return ""


def busy_from_status(status: dict | None) -> int:
    """Work units in flight according to /api/status; 0 when unknown."""
    if not status:
        return 0
    return sum(int(status.get(k) or 0)
               for k in ("active_requests", "waiting_requests", "models_loading"))


class Awake:
    """The policy, separated from I/O: decide hold/release per poll."""

    def __init__(self, linger: float):
        self.linger = linger
        self.last_busy: float | None = None

    def want_hold(self, busy: int, now: float) -> bool:
        if busy > 0:
            self.last_busy = now
            return True
        return self.last_busy is not None and now - self.last_busy < self.linger


class Caffeinate:
    """Owns at most one `caffeinate` child process."""

    def __init__(self, argv=("caffeinate", "-i", "-s")):
        self.argv = list(argv)
        self.proc: subprocess.Popen | None = None

    @property
    def holding(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def hold(self) -> bool:
        if self.holding:
            return False
        self.proc = subprocess.Popen(self.argv)
        return True

    def release(self) -> bool:
        if not self.holding:
            self.proc = None
            return False
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc = None
        return True


def fetch_status(base: str, key: str, timeout: float = 3.0) -> dict | None:
    req = urllib.request.Request(f"{base}/api/status",
                                 headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except (OSError, ValueError):
        return None


def step(policy: Awake, caf: Caffeinate, status: dict | None, now: float,
         home: bool = False) -> str | None:
    """One poll. Returns 'hold'/'release' on a transition, else None."""
    busy = busy_from_status(status)
    # Evaluate the busy rule every poll, even at home, so last_busy stays
    # current and the linger still applies after leaving home mid-request.
    busy_hold = policy.want_hold(busy, now)
    if home or busy_hold:
        if caf.hold():
            if home:
                log.info("hold     on AC at home")
            else:
                log.info("hold     busy=%d (active=%s waiting=%s loading=%s)", busy,
                         (status or {}).get("active_requests"),
                         (status or {}).get("waiting_requests"),
                         (status or {}).get("models_loading"))
            return "hold"
    elif caf.release():
        log.info("release  not on AC at home, idle for %.0fs",
                 now - (policy.last_busy or now))
        return "release"
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--omlx", default="http://127.0.0.1:8000")
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--linger", type=float, default=180.0)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    key = api_key()
    subnet = home_subnet()
    policy, caf = Awake(args.linger), Caffeinate()
    log.info("watching %s every %.0fs, linger %.0fs, home rule %s", args.omlx,
             args.interval, args.linger,
             "on" if subnet else "OFF (HOME_LAN_SUBNET unset)")
    was_home = None
    try:
        while True:
            home = on_home_power(subnet)
            if home != was_home:
                log.info("home     %s", "on AC at home" if home else
                         "not on AC at home")
                was_home = home
            step(policy, caf, fetch_status(args.omlx, key), time.monotonic(), home)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        caf.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
