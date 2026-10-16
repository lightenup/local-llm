#!/usr/bin/env python3
"""Hold a macOS sleep assertion while oMLX is doing inference, and only then.

FINDINGS §37: every thermal emergency began with the Mac entering Idle or
Clamshell Sleep while oMLX was mid-generation. `pmset -c sleep 0` was the
first fix. It was reverted to the CIS value (15) at 13:16 on two consecutive
days, which is consistent with an Intune compliance script, so a global
setting cannot be relied on. This watcher addresses the cause directly: no
sleep *while inferring*. When idle, the Mac sleeps per policy, like with any
other workload that holds an assertion (a call, a build).

Every --interval seconds it polls oMLX's GET /api/status. When
active_requests + waiting_requests + models_loading > 0 it starts
`caffeinate -i -s` (PreventUserIdleSystemSleep, plus PreventSystemSleep on
AC). The assertion is released --linger seconds after the last active poll,
so the gaps between agent turns do not flap it and the SoC can cool with the
fans running before sleep is allowed. If oMLX is unreachable, that counts as
idle.

Not covered: Clamshell Sleep (closing the lid) overrides these assertions on a
Mac without an external display. `doctor.py --layer host` checks the lid.

    python3 proxy/inference_awake.py                 # defaults: 5 s poll, 180 s linger
    pmset -g assertions | grep caffeinate            # see it while a request runs
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import time
import urllib.request
from pathlib import Path

log = logging.getLogger("inference_awake")
OMLX_SETTINGS = Path.home() / ".omlx" / "settings.json"


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


def step(policy: Awake, caf: Caffeinate, status: dict | None, now: float) -> str | None:
    """One poll. Returns 'hold'/'release' on a transition, else None."""
    busy = busy_from_status(status)
    if policy.want_hold(busy, now):
        if caf.hold():
            log.info("hold     busy=%d (active=%s waiting=%s loading=%s)", busy,
                     (status or {}).get("active_requests"),
                     (status or {}).get("waiting_requests"),
                     (status or {}).get("models_loading"))
            return "hold"
    elif caf.release():
        log.info("release  idle for %.0fs", now - (policy.last_busy or now))
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
    policy, caf = Awake(args.linger), Caffeinate()
    log.info("watching %s every %.0fs, linger %.0fs", args.omlx, args.interval,
             args.linger)
    try:
        while True:
            step(policy, caf, fetch_status(args.omlx, key), time.monotonic())
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        caf.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
