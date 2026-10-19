#!/usr/bin/env python3
"""Assert the local stack is doing what it is configured to do.

The reason this exists is FINDINGS §24: the dominant failure mode of this stack
is **configured and inert**. Five instances so far, none of which raised an
error — profile temperature silently overridden by the client (§7),
`context_window_fallbacks` inert without `enable_pre_call_checks` (§16), a
DFlash2 drafter that loaded and registered and then decoded at dense speed
(§23), Spec Kit commands running on whichever agent happened to be current, and
an env var in `zshrc` that non-interactive shells never see. Every one presented
as success. Every one was found by measuring.

So the rule this tool enforces: **enabling something and seeing no error is not
evidence that it is on.** Wherever a check can observe behaviour it does, and
where it can only read configuration it says so in the output, because those two
kinds of evidence are not worth the same.

It is organised by layer, because the layers change at different rates:

    harness   opencode today, something else later    swappable
    proxy     LiteLLM role aliases                    the constant
    queue     llm_queue between proxy and engine (§38)  ordering
    engine    oMLX today, perhaps Ollama alongside    swappable
    host      macOS power/thermal state (§37)         the floor

Engine checks are registered rather than hardcoded so a second engine is an
addition, not a rewrite. The proxy layer is checked hardest because it is the
contract every harness and every engine is supposed to meet.

    python3 bench/doctor.py               # all layers
    python3 bench/doctor.py --layer proxy
    python3 bench/doctor.py --quick       # skip timed behavioural probes

Manual by decision: nothing schedules this or watches its result. Full
reference (layers, checks, reading failures, adding checks): docs/DOCTOR.md.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from omlx_client import BASE, Omlx, OmlxError  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PROXY = os.environ.get("PROXY_BASE", "http://127.0.0.1:4000")


def _local_env(key: str, default: str) -> str:
    """The environment first, then the untracked .env.local, then the default."""
    if key in os.environ:
        return os.environ[key]
    env = ROOT / ".env.local"
    if env.exists():
        for line in env.read_text().splitlines():
            k, sep, v = line.partition("=")
            if sep and k.strip() == key:
                return v.strip().strip("\"'")
    return default


# Prefix of the LaunchAgent labels; proxy/launchd/install.sh uses the same one.
LAUNCHD_PREFIX = _local_env("LAUNCHD_PREFIX", "local")

# Decode floors that distinguish "speculative decoding is working" from "it
# silently fell back to dense". The 8-bit is not here since §39: its vlm_mtp
# path decoded greedily, so it now ships the `plain` variant (no MTP, ~17 tok/s). Measured: the 8-bit runs ~31-34 tok/s with MTP
# and ~17.3 without; oQ4e ~58-64 with and ~30.2 without. Floors sit roughly
# midway, low enough to tolerate the ~23%/3min thermal drift in §22.
MTP_FLOORS = {"Qwen3.8-27B-oQ4e-mtp": 44.0}

# The engine variant (bench/set_variant.py) the 8-bit base model must carry.
# "plain", not "mtp", since §39: oMLX 0.7.0's vlm_mtp path ignores the sampler.
SHIPPED_VARIANT = "plain"

# Served chat profiles whose sampling must be live (§39). Thinking-off profiles
# are the ones a speculative decode path takes, so they are the ones at risk.
SAMPLING_PROFILES = ["Qwen3.8-27B-8bit:qwen-code", "Qwen3.8-27B-8bit:qwen-agent",
                     "Qwen3.8-27B-oQ4e-mtp:oq-agent"]

ROLE_ALIASES = {"coder": "qwen-code", "reasoner": "qwen-reason",
                "fast": "oq-agent"}


@dataclass
class Result:
    name: str
    ok: bool
    kind: str          # "behaviour" | "config" — how strong the evidence is
    detail: str = ""
    layer: str = ""


@dataclass
class Doctor:
    quick: bool = False
    results: list[Result] = field(default_factory=list)

    def check(self, layer, name, kind, fn):
        try:
            ok, detail = fn()
        except Exception as e:                    # a check that crashes is a fail
            ok, detail = False, f"{type(e).__name__}: {str(e)[:150]}"
        self.results.append(Result(name, ok, kind, detail, layer))
        mark = "PASS" if ok else "FAIL"
        tag = "obs " if kind == "behaviour" else "cfg "
        print(f"  [{mark}] {tag} {name:46s} {detail[:70]}")
        return ok


def _key_file(p: Path) -> str:
    try:
        return p.read_text().strip()
    except OSError:
        return ""


def _post(base, key, body, timeout=120):
    req = urllib.request.Request(
        f"{base}/v1/chat/completions", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# --------------------------------------------------------------------------
# Host layer — the machine underneath every engine (FINDINGS §37)
# --------------------------------------------------------------------------

def _pmset_ac() -> dict[str, str]:
    """`pmset -g custom`, AC Power section only, as {key: value}."""
    out = subprocess.run(["pmset", "-g", "custom"], capture_output=True,
                         text=True).stdout
    section, ac = None, {}
    for line in out.splitlines():
        if line.rstrip().endswith(":") and not line.startswith(" "):
            section = line.strip().rstrip(":")
            continue
        if section == "AC Power":
            parts = line.split()
            if len(parts) >= 2:
                ac[" ".join(parts[:-1])] = parts[-1]
    return ac


def check_host(d: Doctor) -> None:
    print("\nHOST — macOS power and thermal (§37)")

    # §37: every thermal emergency began with the Mac entering Idle or
    # Clamshell Sleep while oMLX was mid-inference. Attempt 2 (`pmset -c sleep
    # 0`) was reverted to 15, the CIS value, at 13:16 two days running
    # (Intune suspected), so the protection is now attempt 3:
    # proxy/inference_awake.py holds a sleep assertion only while oMLX is
    # busy. Pass when that agent is running, or when AC sleep is 0 anyway.
    def sleep_prevented_during_inference():
        v = _pmset_ac().get("sleep", "?")
        out = subprocess.run(["launchctl", "print",
                              f"gui/{os.getuid()}/{LAUNCHD_PREFIX}.inference-awake"],
                             capture_output=True, text=True).stdout
        agent = "state = running" in out
        ok = agent or v == "0"
        return ok, (f"inference_awake {'running' if agent else 'NOT RUNNING'}, "
                    f"AC sleep={v}" + ("" if ok else
                    " — run proxy/launchd/install.sh inference-awake"))
    d.check("host", "sleep prevented during inference", "config",
            sleep_prevented_during_inference)

    # Wake-on-network turned each sleep into a DarkWake that kept serving
    # requests until the SMC raised a thermal emergency, 133 times in one cycle.
    def ac_womp_off():
        v = _pmset_ac().get("womp", "?")
        return v == "0", (f"AC womp={v}" + ("" if v == "0" else
                          " — run: sudo pmset -c womp 0 (§37)"))
    d.check("host", "AC wake-on-network disabled", "config", ac_womp_off)

    def on_ac():
        out = subprocess.run(["pmset", "-g", "ps"], capture_output=True,
                             text=True).stdout
        ok = "'AC Power'" in out
        return ok, "on AC" if ok else "ON BATTERY — no sleep setting protects this"
    d.check("host", "on AC power", "behaviour", on_ac)

    def lid_open():
        # Clamshell Sleep ignores `pmset sleep 0`; 2026-10-05 was lid-closed.
        out = subprocess.run(["ioreg", "-r", "-k", "AppleClamshellState", "-d", "1"],
                             capture_output=True, text=True).stdout
        if '"AppleClamshellState"' not in out:   # absent key must not pass
            return False, "AppleClamshellState not found in ioreg — cannot tell"
        closed = '"AppleClamshellState" = Yes' in out
        return not closed, ("lid open" if not closed else
                            "LID CLOSED — clamshell sleep overrides pmset sleep 0")
    d.check("host", "lid open", "behaviour", lid_open)

    def no_recent_thermal():
        out = subprocess.run(["pmset", "-g", "log"], capture_output=True,
                             text=True, timeout=90).stdout
        cutoff = time.strftime("%Y-%m-%d %H:%M:%S",
                               time.localtime(time.time() - 86400))
        hits = [ln[:19] for ln in out.splitlines()
                if "Thermal" in ln and ln[:19] >= cutoff]
        return not hits, ("none in 24h" if not hits else
                          f"{len(hits)} events, first {hits[0]}, last {hits[-1]}")
    d.check("host", "no thermal-emergency sleep in last 24h", "behaviour",
            no_recent_thermal)


# --------------------------------------------------------------------------
# Engine layer
# --------------------------------------------------------------------------

def check_engine_omlx(d: Doctor) -> None:
    print("\nENGINE — oMLX")
    o = Omlx()

    d.check("engine", "server responds", "behaviour",
            lambda: (o.health().get("status") == "healthy",
                     f"status={o.health().get('status')}"))

    def profiles_match():
        # apply.py compares stored settings against definitions.py. A drifted
        # profile is the §7 failure: it serves, it answers, it is wrong.
        out = subprocess.run([sys.executable, "profiles/apply.py", "--verify-only"],
                             cwd=ROOT, capture_output=True, text=True)
        ok = "all 4 profiles stored and served" in out.stdout
        out2 = subprocess.run([sys.executable, "profiles/apply.py", "--set", "oq4e",
                               "--verify-only"], cwd=ROOT, capture_output=True, text=True)
        ok2 = "all 3 profiles stored and served" in out2.stdout
        return ok and ok2, "27B 4/4 and oQ4e 3/3 match definitions" if ok and ok2 \
            else "profile drift — run profiles/apply.py"
    d.check("engine", "profiles match their definitions", "config", profiles_match)

    def base_variant():
        # §38: the base model drifted to qwen35_ane_prefill_enabled=True while
        # every profile check passed or went unrun. Engine fields on the base
        # decide the load-time signature of code/agent/longctx, so a drift here
        # both slows prefill (§3) and splits the engine from any profile that
        # pins the old value. Expect exactly set_variant.py's shipped variant.
        import set_variant as sv
        want = {**sv.RESET, **sv.VARIANTS[SHIPPED_VARIANT]}
        s = o.settings("Qwen3.8-27B-8bit")
        diff = {k: (want[k], s.get(k)) for k in want
                if (s.get(k) or None) != (want[k] or None)}
        return not diff, (f"base == set_variant '{SHIPPED_VARIANT}'" if not diff
                          else f"DRIFT (want, have): {diff}")
    d.check("engine", "8-bit base engine variant is the shipped one", "config",
            base_variant)

    def no_stale_profiles():
        served = [m["id"] for m in o.get("/v1/models", admin=False)["data"]
                  if ":" in m["id"]]
        stale = [s for s in served if "Coder-Next" in s]
        return not stale, f"{len(served)} served" + (f", STALE: {stale}" if stale else "")
    d.check("engine", "no retired profiles still served", "behaviour", no_stale_profiles)

    if d.quick:
        return

    # §39: oMLX 0.7.0's vlm_mtp path decoded the 8-bit greedily, ignoring
    # temperature, top_p and seed. Identical requests gave byte-identical text,
    # and agents looped for hundreds of turns. Every profile pins sampling via
    # force_sampling, so 4 identical open-ended requests must not all agree.
    # This catches any decode path that silently drops the sampler.
    for served in SAMPLING_PROFILES:
        def samples(served=served):
            msgs = [{"role": "user", "content":
                     "Invent a name for a fictional river and describe it in "
                     "one sentence."}]
            outs = {(o.chat(msgs, model=served, max_tokens=40).get("choices")
                     or [{}])[0].get("message", {}).get("content", "")
                    for _ in range(4)}
            return len(outs) >= 2, (f"{len(outs)}/4 distinct" + ("" if len(outs) >= 2
                                    else " — DETERMINISTIC: sampler ignored (§39)"))
        d.check("engine", f"profile samples: {served}", "behaviour", samples)

    # The §23 check. A drafter can load, register and accelerate nothing.
    for model, floor in MTP_FLOORS.items():
        def probe(model=model, floor=floor):
            try:
                o.load(model)
            except OmlxError as e:
                return False, f"will not load: {str(e)[:60]}"
            msgs = [{"role": "user", "content":
                     "Write a Python function that merges two sorted lists, "
                     "with a docstring."}]
            o.chat(msgs, model=model, max_tokens=32)          # warm
            r = o.timed_chat(msgs, model=model, max_tokens=220, temperature=0.0)
            tps = r["completion_tokens"] / r["wall_s"]
            return tps >= floor, (f"{tps:.1f} tok/s (floor {floor}; below means "
                                  f"speculative decoding is NOT engaging)")
        d.check("engine", f"speculative decode active: {model}", "behaviour", probe)


# --------------------------------------------------------------------------
# Proxy layer — the constant, so checked hardest
# --------------------------------------------------------------------------

def check_proxy(d: Doctor) -> None:
    print("\nPROXY — LiteLLM (the layer harnesses and engines both depend on)")
    env = ROOT / "proxy" / ".env"
    master = ""
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("LITELLM_MASTER_KEY="):
                master = line.split("=", 1)[1].strip()
    local_key = _key_file(Path.home() / ".omlx" / "litellm_local_key")

    def reachable():
        with urllib.request.urlopen(f"{PROXY}/health/liveliness", timeout=15) as r:
            return r.status == 200, f"HTTP {r.status}"
    if not d.check("proxy", "proxy reachable", "behaviour", reachable):
        print("       (proxy down — is Docker running? remaining proxy checks skipped)")
        return

    for alias, expect in ROLE_ALIASES.items():
        def resolves(alias=alias, expect=expect):
            r = _post(PROXY, local_key,
                      {"model": alias, "max_tokens": 2000,
                       "messages": [{"role": "user", "content": "one word: ok"}]},
                      timeout=300)
            served = r.get("model", "")
            txt = (r["choices"][0]["message"].get("content") or "").strip()
            return bool(txt), f"answers; served as {served!r}"
        d.check("proxy", f"role alias answers: {alias}", "behaviour", resolves)

    def group_enforced():
        # §C2's guardrail. Must refuse before any router logic runs.
        try:
            _post(PROXY, local_key,
                  {"model": "gpt-4o", "max_tokens": 8,
                   "messages": [{"role": "user", "content": "hi"}]}, timeout=60)
            return False, "a model outside the local group was ACCEPTED"
        except urllib.error.HTTPError as e:
            return e.code in (401, 403), f"refused with HTTP {e.code}"
    d.check("proxy", "access group refuses out-of-group models", "behaviour",
            group_enforced)

    def cfg(pred, label):
        text = (ROOT / "proxy" / "litellm-config.yaml").read_text()
        return pred(text), label
    d.check("proxy", "enable_pre_call_checks is on", "config",
            lambda: cfg(lambda t: "enable_pre_call_checks: true" in t,
                        "without it max_input_tokens is inert metadata (§16)"))
    d.check("proxy", "router fallbacks are empty", "config",
            lambda: cfg(lambda t: "fallbacks: []" in t,
                        "a fallback fires even for keys barred from the target (§C2)"))


# --------------------------------------------------------------------------
# Queue layer — proxy/llm_queue.py between LiteLLM and oMLX (FINDINGS §38)
# --------------------------------------------------------------------------

QUEUE = os.environ.get("LLM_QUEUE_BASE", "http://127.0.0.1:8001")


def _queue_status() -> dict:
    with urllib.request.urlopen(f"{QUEUE}/queue/status", timeout=5) as r:
        return json.loads(r.read())


def check_queue(d: Doctor) -> None:
    print("\nQUEUE — llm_queue (one-at-a-time, group-by-model; §38)")

    if not d.check("queue", "llm_queue reachable", "behaviour",
                   lambda: (True, "depth={depth} running={running} granted={g}".format(
                       g=(s := _queue_status())["stats"]["granted"], **s))):
        return

    def routing():
        # The compose file says where LiteLLM SHOULD send oMLX traffic; the
        # running container says where it DOES. They disagree after an edit
        # without a recreate, which is exactly the silent-bypass case.
        compose = (ROOT / "proxy" / "docker-compose.yml").read_text()
        want = next((ln.split(":", 1)[1].strip() for ln in compose.splitlines()
                     if ln.strip().startswith("OMLX_BASE_URL:")), "?")
        out = subprocess.run(["docker", "inspect", "litellm", "--format",
                              "{{range .Config.Env}}{{println .}}{{end}}"],
                             capture_output=True, text=True)
        have = next((ln.split("=", 1)[1] for ln in out.stdout.splitlines()
                     if ln.startswith("OMLX_BASE_URL=")), "?")
        if want != have:
            return False, f"compose wants {want}, container runs {have} — recreate"
        mode = "LIVE (queued)" if ":8001" in have else "BYPASSED (rollback, LiteLLM skips the queue)"
        return True, f"{mode}: {have}"
    d.check("queue", "LiteLLM upstream matches compose", "config", routing)

    if d.quick:
        return

    def proves_path():
        # Send one request through LiteLLM and require the queue to grant it.
        # The queued `-q` twins are gone (2026-10-18), so with OMLX_BASE_URL
        # rolled back to :8000 no alias reaches the queue and this fails.
        alias = "fast"
        before = _queue_status()["stats"]["granted"]
        r = _post(PROXY, _key_file(Path.home() / ".omlx" / "litellm_local_key"),
                  {"model": alias, "max_tokens": 8,
                   "messages": [{"role": "user", "content": "one word: ok"}]},
                  timeout=600)
        after = _queue_status()["stats"]["granted"]
        ok = after > before and bool(r.get("choices"))
        return ok, f"{alias}: queue granted {before} -> {after}"
    d.check("queue", "a LiteLLM request passes through the queue", "behaviour",
            proves_path)


# --------------------------------------------------------------------------
# Harness layer
# --------------------------------------------------------------------------

def check_harness(d: Doctor) -> None:
    print("\nHARNESS — opencode")

    def env_in_noninteractive():
        # §24: zshrc is sourced only by INTERACTIVE shells, so a key defined
        # there is invisible to anything the harness spawns. Check the way the
        # harness sees it, not the way a terminal does.
        missing = []
        for var in ("OMLX_API_KEY", "LITELLM_LOCAL_KEY"):
            # `env -u` unsets the variable for the child, so the shell MUST
            # re-derive it from a startup file. Without this the check inherits
            # whatever the calling process happens to hold and can never fail --
            # which it did, on the first attempt to fault-inject it. The tool
            # built to catch "configured and inert" was itself inert.
            out = subprocess.run(["env", f"-u{var}", "zsh", "-c",
                                  f'printf %s "${{{var}}}"'],
                                 capture_output=True, text=True)
            if not out.stdout.strip():
                missing.append(var)
        return not missing, ("all set in a non-interactive shell" if not missing
                             else f"MISSING: {missing} — defined in zshrc rather "
                                  f"than zshenv/env?")
    d.check("harness", "provider env vars visible non-interactively", "behaviour",
            env_in_noninteractive)

    def providers_resolve():
        p = Path.home() / "Development/private/dotfiles/opencode/opencode.json"
        cfg = json.loads(p.read_text())
        provs = cfg.get("provider", {})
        names = {m for pv in provs.values() for m in (pv.get("models") or {})}
        agents = {a: v.get("model", "") for a, v in (cfg.get("agent") or {}).items()}
        bad = [f"{a}->{m}" for a, m in agents.items()
               if m and m.split("/", 1)[-1] not in names]
        return not bad, (f"{len(provs)} providers, {len(agents)} agents all resolve"
                         if not bad else f"agents point at undeclared models: {bad}")
    d.check("harness", "every agent's model is declared", "config", providers_resolve)

    def speckit_stock():
        # The routing contract lives in the role aliases, not in Spec Kit's
        # files, so that `specify upgrade` cannot silently undo it.
        hits = []
        for base in (Path.home() / "Development/private",):
            for f in base.glob("*/.opencode/commands/speckit.*.md"):
                head = f.read_text().split("---")[1] if "---" in f.read_text() else ""
                if "\nagent:" in head or "\nmodel:" in head:
                    hits.append(str(f.relative_to(base)))
        return not hits, ("stock" if not hits
                          else f"customised, will be lost on upgrade: {hits[:3]}")
    d.check("harness", "Spec Kit commands are unmodified", "config", speckit_stock)


LAYERS = {"host": check_host, "engine": check_engine_omlx, "queue": check_queue,
          "proxy": check_proxy, "harness": check_harness}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--layer", choices=sorted(LAYERS), action="append")
    ap.add_argument("--quick", action="store_true",
                    help="skip timed behavioural probes (decode-rate checks)")
    args = ap.parse_args()

    d = Doctor(quick=args.quick)
    print("=" * 78)
    print("local stack doctor — 'obs' checks observed behaviour, 'cfg' reads config")
    print("=" * 78)
    t0 = time.perf_counter()
    for name in (args.layer or list(LAYERS)):
        LAYERS[name](d)

    fails = [r for r in d.results if not r.ok]
    obs = sum(1 for r in d.results if r.kind == "behaviour")
    print("\n" + "=" * 78)
    print(f"  {len(d.results) - len(fails)}/{len(d.results)} checks pass "
          f"({obs} observed, {len(d.results) - obs} config-only) "
          f"in {time.perf_counter() - t0:.0f}s")
    if fails:
        print("\n  FAILURES:")
        for r in fails:
            print(f"    [{r.layer}] {r.name}\n        {r.detail}")
    (ROOT / "results" / "doctor.json").write_text(json.dumps(
        {"at": time.strftime("%Y-%m-%dT%H:%M:%S"),
         "results": [r.__dict__ for r in d.results]}, indent=2))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
