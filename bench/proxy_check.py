#!/usr/bin/env python3
"""C4 / C5 — does the LiteLLM hop preserve behaviour, and what does it cost?

Two subcommands, because the wave sequences them separately:

`toolcall` is the **C4 slice**: the gate that runs before C3 and C9 are built
on top of the proxy. It sends a file-edit-shaped tool definition and checks
that a usable tool call comes back — name present, arguments parse as JSON,
required keys present. It runs the same check DIRECT and THROUGH the proxy and
compares, because the failure this is looking for is not "the proxy is down"
but "the proxy quietly reshaped a tool call into prose". Coder-Next has its own
function-call format (backlog A1/C4), so it is tested explicitly rather than
assumed to behave like the 27B.

`overhead` is **C5**: per-request wall-clock, direct vs hop, at a fixed tiny
generation so the difference is dominated by the hop rather than by decode.
Reported as a median delta in ms, not a ratio - a ratio on a 1 s request tells
you nothing about a 20 min one, and `bench/throughput.py` resolves
single-digit-percent differences that a fat hop would contaminate.

Usage:
    python3 bench/proxy_check.py toolcall
    python3 bench/proxy_check.py overhead --repeats 7
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from omlx_client import BASE, Omlx  # noqa: E402

PROXY = os.environ.get("PROXY_BASE", "http://127.0.0.1:4000")
RESULTS = Path(__file__).resolve().parent.parent / "results"
ENV_FILE = Path(__file__).resolve().parent.parent / "proxy" / ".env"

# The two models that matter: the quality tier, and the one with its own
# function-call format that C4 flags as most likely to fail quietly.
MODELS = ("Qwen3.8-27B-8bit:qwen-agent", "Qwen3-Coder-Next-6bit:cn-agent")

# Deliberately shaped like a real opencode edit rather than a toy `get_weather`:
# a path plus two strings, which is the payload that actually has to survive.
EDIT_TOOL = {
    "type": "function",
    "function": {
        "name": "edit_file",
        "description": "Replace an exact string in a file on disk.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute file path"},
                "old_string": {"type": "string", "description": "Exact text to replace"},
                "new_string": {"type": "string", "description": "Replacement text"},
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
}

EDIT_PROMPT = (
    "In the file /tmp/demo/app.py, the line `timeout = 30` must become "
    "`timeout = 120`. Use the edit_file tool to make that change. "
    "Do not explain, just call the tool."
)


def _env(name: str) -> str:
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip()
    return os.environ.get(name, "")


def _post(base: str, key: str, body: dict, timeout: float = 600.0) -> tuple[dict, float]:
    req = urllib.request.Request(
        f"{base.rstrip('/')}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"},
        method="POST",
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code,
                "_detail": e.read().decode(errors="replace")[:400]}, \
               time.perf_counter() - t0
    return payload, time.perf_counter() - t0


def _grade_toolcall(resp: dict) -> dict:
    """Reduce a completion to the four things C4 actually needs to be true."""
    if "_http_error" in resp:
        return {"ok": False, "why": f"HTTP {resp['_http_error']}: {resp['_detail'][:160]}"}
    try:
        msg = resp["choices"][0]["message"]
    except (KeyError, IndexError):
        return {"ok": False, "why": f"malformed response: {str(resp)[:160]}"}

    calls = msg.get("tool_calls") or []
    if not calls:
        # This is the quiet failure C4 exists to catch: a mangled call degrades
        # into plausible-looking prose rather than an error.
        content = (msg.get("content") or "")[:200]
        return {"ok": False, "why": "NO tool_calls - degraded to prose",
                "content": content}

    fn = calls[0].get("function", {})
    name = fn.get("name")
    raw = fn.get("arguments")
    try:
        args = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return {"ok": False, "why": "arguments are not valid JSON",
                "name": name, "raw": str(raw)[:200]}

    missing = [k for k in ("path", "old_string", "new_string") if k not in (args or {})]
    return {
        "ok": name == "edit_file" and not missing,
        "why": "ok" if name == "edit_file" and not missing
               else f"name={name!r} missing={missing}",
        "name": name,
        "args": args,
        "n_calls": len(calls),
    }


def cmd_toolcall(args) -> int:
    key_direct = Omlx().api_key
    key_proxy = _env("LITELLM_MASTER_KEY")
    if not key_proxy:
        print("no LITELLM_MASTER_KEY in proxy/.env", file=sys.stderr)
        return 2

    body = {
        "messages": [{"role": "user", "content": EDIT_PROMPT}],
        "tools": [EDIT_TOOL],
        "tool_choice": "auto",
        "max_tokens": 400,
    }

    out = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "results": []}
    verdict = 0
    for model in MODELS:
        print(f"\n=== {model} ===")
        row = {"model": model}
        for where, base, key in (("direct", BASE, key_direct),
                                 ("proxy", PROXY, key_proxy)):
            resp, wall = _post(base, key, {**body, "model": model})
            g = _grade_toolcall(resp)
            row[where] = {**g, "wall_s": round(wall, 2)}
            flag = "PASS" if g["ok"] else "FAIL"
            print(f"  {where:7s} {flag}  {wall:6.2f}s  {g['why']}")
            if g.get("args"):
                print(f"          args: {json.dumps(g['args'])[:150]}")
            if g.get("content"):
                print(f"          prose: {g['content'][:120]!r}")

        # The comparison is the point: a proxy that breaks tool calls shows up
        # as direct-pass / proxy-fail. Both failing is a model problem, not a
        # hop problem, and must not be read as a C4 result.
        d, p = row["direct"]["ok"], row["proxy"]["ok"]
        if d and p:
            row["verdict"] = "hop preserves tool calls"
        elif d and not p:
            row["verdict"] = "HOP BREAKS TOOL CALLS"
            verdict = 1
        elif not d and not p:
            row["verdict"] = "model fails tool call in BOTH - not a proxy fault"
        else:
            row["verdict"] = "passes only through proxy - investigate"
        print(f"  -> {row['verdict']}")
        out["results"].append(row)

    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "proxy_toolcall.json").write_text(json.dumps(out, indent=2))
    print(f"\nsaved -> results/proxy_toolcall.json")
    return verdict


def cmd_overhead(args) -> int:
    key_direct = Omlx().api_key
    key_proxy = _env("LITELLM_MASTER_KEY")
    model = args.model

    # Tiny prompt, tiny generation: the hop cost is a fixed per-request tax, so
    # it is measured where decode cannot drown it. Interleaved direct/proxy so
    # engine-state drift hits both arms equally.
    body = {"model": model, "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"}]}

    print(f"model={model}  repeats={args.repeats}  (1 warmup discarded)")
    _post(BASE, key_direct, body)
    _post(PROXY, key_proxy, body)

    direct, proxy = [], []
    for i in range(args.repeats):
        _, d = _post(BASE, key_direct, body)
        _, p = _post(PROXY, key_proxy, body)
        direct.append(d * 1000)
        proxy.append(p * 1000)
        print(f"  {i+1:2d}  direct {d*1000:7.1f} ms   proxy {p*1000:7.1f} ms   "
              f"delta {(p-d)*1000:+7.1f} ms")

    md, mp = statistics.median(direct), statistics.median(proxy)
    out = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": model, "repeats": args.repeats,
        "direct_ms": {"median": round(md, 1), "min": round(min(direct), 1),
                      "max": round(max(direct), 1)},
        "proxy_ms": {"median": round(mp, 1), "min": round(min(proxy), 1),
                     "max": round(max(proxy), 1)},
        "median_overhead_ms": round(mp - md, 1),
    }
    print(f"\n  direct median {md:7.1f} ms")
    print(f"  proxy  median {mp:7.1f} ms")
    print(f"  OVERHEAD      {mp - md:+7.1f} ms per request")
    # C6 is gated on this: bench/throughput.py resolves single-digit-percent
    # differences, and its shortest prompt takes ~1 s.
    print(f"\n  as a share of a 1 s request: {(mp - md) / 1000:.2%}")
    print(f"  as a share of the 16k cold prefill (12.6 s): "
          f"{(mp - md) / 12600:.3%}")

    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "proxy_overhead.json").write_text(json.dumps(out, indent=2))
    print("\nsaved -> results/proxy_overhead.json")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("toolcall", help="C4 slice: tool calls survive the hop")
    t.set_defaults(func=cmd_toolcall)

    o = sub.add_parser("overhead", help="C5: per-request hop cost")
    o.add_argument("--repeats", type=int, default=7)
    o.add_argument("--model", default="Qwen3.8-27B-8bit:qwen-agent")
    o.set_defaults(func=cmd_overhead)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
