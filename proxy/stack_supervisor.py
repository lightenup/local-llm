#!/usr/bin/env python3
"""Keep the LiteLLM containers up, and nudge oMLX, from one LaunchAgent.

Why (FINDINGS §43): after a reboot on 2026-11-09 Docker Desktop came back but
the `litellm` and `litellm-postgres` containers stayed `Exited (0)` despite
`restart: unless-stopped`. Nothing listened on :4000 for 13 hours. launchd, a
Login Item and Docker's restart policy each owned part of the chain and
nothing checked the whole of it.

Every --interval seconds (60):

  1. GET http://127.0.0.1:4000/health/liveliness, 2 s timeout.
  2. If that fails and the Docker socket is missing: `open -ga Docker`, then
     wait up to 120 s for the socket.
  3. Then `docker compose -f proxy/docker-compose.yml up -d`, with an absolute
     docker path (launchd's PATH is /usr/bin:/bin:/usr/sbin:/sbin).
  4. A failed `up` backs off 60 s, doubling to 10 min. A successful one waits
     a grace period for LiteLLM's healthcheck before trying again.
  5. If oMLX's :8000 has been down for more than 5 minutes: `open -ga oMLX`,
     at most once every 10 minutes. oMLX is a GUI Login Item with no other
     supervisor.
  6. Once, 5 minutes after LiteLLM and oMLX first both answer, kick the
     doctor-quick agent (`bench/doctor.py --quick`), so a fresh login gets a
     doctor run without relying on RunAtLoad racing the stack.

It never runs `down`, `restart` or `--force-recreate`. `up -d` does recreate
a container whose compose definition changed; that is the point of it. Forcing
a recreate stays a human action, because the doctor checks that the running
container matches the compose file.

Each action goes to the log (stderr; the LaunchAgent sends it to
$LLM_LOG_DIR/stack_supervisor.log) and to a small JSON state file that
llm_queue's /metrics reads, so this needs no listener of its own.

    python3 proxy/stack_supervisor.py --once        # one check, then exit
    proxy/launchd/install.sh stack-supervisor       # the LaunchAgent
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from inference_awake import api_key  # noqa: E402

log = logging.getLogger("stack_supervisor")
REPO = Path(__file__).resolve().parent.parent
COMPOSE = REPO / "proxy" / "docker-compose.yml"
DOCKER_BINS = ("/usr/local/bin/docker",
               "/Applications/Docker.app/Contents/Resources/bin/docker",
               "/opt/homebrew/bin/docker")
DOCKER_SOCKETS = (Path.home() / ".docker" / "run" / "docker.sock",
                  Path("/var/run/docker.sock"))
ACTIONS = ("up", "open_docker", "open_omlx", "doctor")


# --------------------------------------------------------------------------
# Policy: no I/O, so it can be tested directly
# --------------------------------------------------------------------------

class Supervisor:
    """Decide what to do per tick from what was observed."""

    def __init__(self, backoff_min=60.0, backoff_max=600.0, up_grace=180.0,
                 omlx_down_after=300.0, omlx_reopen=600.0, doctor_after=300.0):
        self.backoff_min, self.backoff_max = backoff_min, backoff_max
        self.up_grace = up_grace
        self.omlx_down_after, self.omlx_reopen = omlx_down_after, omlx_reopen
        self.doctor_after = doctor_after
        self.delay = backoff_min
        self.wait_until = 0.0
        self.omlx_down_since: float | None = None
        self.omlx_opened_at: float | None = None
        self.healthy_since: float | None = None
        self.doctor_done = False

    def decide(self, litellm_ok: bool, socket_ok: bool, omlx_ok: bool,
               now: float) -> list[str]:
        acts: list[str] = []
        if litellm_ok:
            self.delay = self.backoff_min
            self.wait_until = 0.0
        elif now >= self.wait_until:
            if not socket_ok:
                acts.append("open_docker")
            acts.append("up")

        if omlx_ok:
            self.omlx_down_since = None
        else:
            if self.omlx_down_since is None:
                self.omlx_down_since = now
            if (now - self.omlx_down_since >= self.omlx_down_after
                    and (self.omlx_opened_at is None
                         or now - self.omlx_opened_at >= self.omlx_reopen)):
                acts.append("open_omlx")

        if litellm_ok and omlx_ok:
            if self.healthy_since is None:
                self.healthy_since = now
            if (not self.doctor_done
                    and now - self.healthy_since >= self.doctor_after):
                acts.append("doctor")
        else:
            self.healthy_since = None
        return acts

    def record(self, action: str, ok: bool, now: float) -> None:
        """Feed back the outcome of an action decide() asked for."""
        if action == "up":
            if ok:
                self.delay = self.backoff_min
                self.wait_until = now + self.up_grace
            else:
                self.wait_until = now + self.delay
                self.delay = min(self.delay * 2, self.backoff_max)
        elif action == "open_omlx":
            self.omlx_opened_at = now
        elif action == "doctor":
            self.doctor_done = True     # once per supervisor lifetime, even if
                                        # the kick failed: no retry storm


class State:
    """The JSON file llm_queue renders as local_stack_supervisor_* metrics."""

    def __init__(self, path: Path | None):
        self.path = path
        self.data = {"actions_total": {a: 0 for a in ACTIONS},
                     "last_action": None, "heartbeat": None}
        if path and path.exists():
            try:
                old = json.loads(path.read_text())
                self.data["actions_total"].update(old.get("actions_total") or {})
                self.data["last_action"] = old.get("last_action")
            except (OSError, ValueError):
                pass

    def action(self, name: str, ok: bool, detail: str, at: float) -> None:
        self.data["actions_total"][name] = self.data["actions_total"].get(name, 0) + 1
        self.data["last_action"] = {"action": name, "ok": ok, "at": at,
                                    "detail": detail[:200]}

    def save(self, at: float) -> None:
        self.data["heartbeat"] = at
        if not self.path:
            return
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1))
        os.replace(tmp, self.path)


# --------------------------------------------------------------------------
# I/O
# --------------------------------------------------------------------------

def http_ok(url: str, timeout: float, key: str | None = None) -> bool:
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers),
                                    timeout=timeout) as r:
            return 200 <= r.status < 300
    except (OSError, ValueError):
        return False


def docker_bin() -> str | None:
    return next((b for b in DOCKER_BINS if os.access(b, os.X_OK)), None)


def socket_present() -> bool:
    return any(p.exists() for p in DOCKER_SOCKETS)


def _run(argv: list[str], timeout: float) -> tuple[bool, str]:
    env = dict(os.environ)
    # docker compose finds its plugin and credential helper next to the CLI.
    extra = [str(Path(b).resolve().parent) for b in DOCKER_BINS if os.path.exists(b)]
    env["PATH"] = ":".join([*extra, env.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")])
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"{type(e).__name__}: {e}"
    out = (p.stdout + p.stderr).strip().replace("\n", " | ")
    return p.returncode == 0, f"rc={p.returncode} {out[-300:]}"


class Executor:
    def __init__(self, label_prefix: str, socket_wait: float = 120.0):
        self.label_prefix = label_prefix
        self.socket_wait = socket_wait

    def open_docker(self) -> tuple[bool, str]:
        ok, out = _run(["/usr/bin/open", "-ga", "Docker"], 30)
        if not ok:
            return False, out
        t0 = time.monotonic()
        while time.monotonic() - t0 < self.socket_wait:
            if socket_present():
                return True, f"socket after {time.monotonic() - t0:.0f}s"
            time.sleep(2)
        return False, f"no Docker socket after {self.socket_wait:.0f}s"

    def up(self) -> tuple[bool, str]:
        d = docker_bin()
        if not d:
            return False, f"no docker CLI in {', '.join(DOCKER_BINS)}"
        return _run([d, "compose", "-f", str(COMPOSE), "up", "-d"], 300)

    def open_omlx(self) -> tuple[bool, str]:
        return _run(["/usr/bin/open", "-ga", "oMLX"], 30)

    def doctor(self) -> tuple[bool, str]:
        label = f"gui/{os.getuid()}/{self.label_prefix}.doctor-quick"
        return _run(["/bin/launchctl", "kickstart", label], 30)


def tick(sup: Supervisor, ex: Executor, state: State, args) -> list[str]:
    now = time.monotonic()
    litellm_ok = http_ok(f"{args.litellm}/health/liveliness", 2.0)
    omlx_ok = http_ok(f"{args.omlx}/api/status", 2.0, args.key)
    sock = socket_present()
    acts = sup.decide(litellm_ok, sock, omlx_ok, now)
    for a in acts:
        if a == "up" and "open_docker" in acts and not socket_present():
            # open_docker already failed to bring the socket; up would only
            # fail the same way. Count it as a failed up so back-off applies.
            ok, detail = False, "skipped: Docker socket still missing"
        else:
            log.info("action   %s (litellm=%s socket=%s omlx=%s)", a,
                     "ok" if litellm_ok else "DOWN", "yes" if sock else "NO",
                     "ok" if omlx_ok else "DOWN")
            ok, detail = getattr(ex, a)()
        (log.info if ok else log.warning)("%s %s: %s", "done    " if ok else "FAILED  ",
                                          a, detail)
        sup.record(a, ok, time.monotonic())
        state.action(a, ok, detail, time.time())
    state.save(time.time())
    return acts


def _local_env(key: str, default: str) -> str:
    if key in os.environ:
        return os.environ[key]
    env = REPO / ".env.local"
    if env.exists():
        for line in env.read_text().splitlines():
            k, sep, v = line.partition("=")
            if sep and k.strip() == key:
                return os.path.expandvars(v.strip().strip("\"'"))
    return default


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--litellm", default="http://127.0.0.1:4000")
    ap.add_argument("--omlx", default="http://127.0.0.1:8000")
    ap.add_argument("--interval", type=float, default=60.0)
    ap.add_argument("--state", default=None,
                    help="JSON state file (default $LLM_LOG_DIR/stack_supervisor.json)")
    ap.add_argument("--no-omlx", action="store_true", help="never open oMLX")
    ap.add_argument("--once", action="store_true", help="one tick, then exit")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    log_dir = Path(_local_env("LLM_LOG_DIR",
                              str(Path.home() / "Library" / "Logs" / "llm_queue")))
    state = State(Path(args.state) if args.state else log_dir / "stack_supervisor.json")
    args.key = api_key()
    sup = Supervisor()
    if args.no_omlx:
        sup.omlx_down_after = float("inf")
    ex = Executor(_local_env("LAUNCHD_PREFIX", "local"))
    log.info("supervising %s every %.0fs (compose %s, docker %s)", args.litellm,
             args.interval, COMPOSE.name, docker_bin() or "NOT FOUND")
    while True:
        tick(sup, ex, state, args)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
