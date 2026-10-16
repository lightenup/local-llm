#!/usr/bin/env python3
"""End-to-end behavioural check of each exposed profile.

Verifies the thing that actually matters: that requesting a profile by model
name produces the behaviour that profile was defined for, and that switching
between profiles does not trigger an engine reload.

    python3 bench/smoke.py
    python3 bench/smoke.py --only qwen-agent
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from omlx_client import Omlx  # noqa: E402

THINK_RE = re.compile(r"<think>|</think>|◁think▷", re.I)

WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "City name"},
                "unit": {"type": "string", "enum": ["c", "f"]},
            },
            "required": ["city"],
        },
    },
}


def check_code(o: Omlx, model: str) -> tuple[bool, str]:
    r = o.timed_chat(
        [
            {
                "role": "user",
                "content": (
                    "Write a Python function `dedupe_stable(xs)` that removes "
                    "duplicates while preserving first-seen order. Code only."
                ),
            }
        ],
        model=model,
        max_tokens=400,
    )
    text = r["text"]
    ok = "def dedupe_stable" in text and not THINK_RE.search(text)
    detail = f"{r['completion_tokens']}tok {r['wall_s']}s {r['tok_per_s_overall']}t/s"
    if THINK_RE.search(text):
        detail += " | UNEXPECTED thinking block"
    if "def dedupe_stable" not in text:
        detail += " | function not found"
    return ok, detail


def check_agent(o: Omlx, model: str) -> tuple[bool, str]:
    t0 = time.perf_counter()
    resp = o.chat(
        [{"role": "user", "content": "What is the weather in Oslo? Use the tool."}],
        model=model,
        tools=[WEATHER_TOOL],
        max_tokens=300,
    )
    wall = round(time.perf_counter() - t0, 2)
    msg = resp["choices"][0]["message"]
    calls = msg.get("tool_calls") or []
    finish = resp["choices"][0].get("finish_reason")
    ok = bool(calls) and calls[0]["function"]["name"] == "get_weather"
    args = calls[0]["function"]["arguments"] if calls else ""
    detail = f"finish={finish} calls={len(calls)} args={args} {wall}s"
    if ok and "oslo" not in str(args).lower():
        ok = False
        detail += " | city not extracted"
    return ok, detail


def check_reason(o: Omlx, model: str) -> tuple[bool, str]:
    r = o.timed_chat(
        [
            {
                "role": "user",
                "content": (
                    "A train leaves A at 60 km/h. Two hours later a second leaves A "
                    "at 90 km/h on the same track. How far from A do they meet? "
                    "Reason it through."
                ),
            }
        ],
        model=model,
        max_tokens=4000,
    )
    text = r["text"]
    reasoning_present = bool(THINK_RE.search(text)) or r["completion_tokens"] > 150
    ok = reasoning_present and "360" in text.replace(",", "")
    detail = f"{r['completion_tokens']}tok {r['wall_s']}s {r['tok_per_s_overall']}t/s"
    if "360" not in text.replace(",", ""):
        detail += " | expected 360 km not found"
    return ok, detail


def check_long(o: Omlx, model: str) -> tuple[bool, str]:
    """Needle-in-a-haystack at ~40k tokens: proves large prompts are admitted
    and that the model attends to the middle, not just the ends."""
    filler = (
        "The quarterly maintenance log records routine inspection of pumps, "
        "valves, and seals with no anomalies detected. "
    )
    needle = "The calibration constant for sensor array K7 is 84391."
    blocks = [filler * 40 for _ in range(60)]
    blocks[len(blocks) // 2] = needle + " " + (filler * 39)
    haystack = "\n\n".join(f"[section {i}]\n{b}" for i, b in enumerate(blocks))

    r = o.timed_chat(
        [
            {
                "role": "user",
                "content": (
                    f"{haystack}\n\nWhat is the calibration constant for sensor "
                    f"array K7? Answer with the number only."
                ),
            }
        ],
        model=model,
        max_tokens=64,
    )
    ok = "84391" in r["text"]
    detail = (
        f"prompt={r['prompt_tokens']}tok cached={r['cached_tokens']} "
        f"{r['wall_s']}s -> {r['text'].strip()[:40]!r}"
    )
    return ok, detail


# Keyed by profile name; oMLX serves each as "<base model id>:<api_name>".
CHECKS = {
    "code": check_code,
    "agent": check_agent,
    "reason": check_reason,
    "longctx": check_long,
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="profile name, e.g. agent")
    args = ap.parse_args()

    o = Omlx()
    served = {m["id"] for m in o.get("/v1/models", admin=False)["data"]}
    served_id = {p["name"]: p.get("model_id") for p in o.list_profiles()}
    targets = [args.only] if args.only else list(CHECKS)

    reloads_before = _reload_count()
    failures = 0
    print(f"== smoke test {len(targets)} profile(s)\n")
    for name in targets:
        model = served_id.get(name)
        if not model or model not in served:
            print(f"  {name:<8} NOT SERVED ({model})")
            failures += 1
            continue
        try:
            ok, detail = CHECKS[name](o, model)
        except Exception as e:  # noqa: BLE001
            ok, detail = False, f"exception: {type(e).__name__}: {str(e)[:200]}"
        print(f"  {name:<8} {'PASS' if ok else 'FAIL'}  {detail}")
        if not ok:
            failures += 1

    reloads_after = _reload_count()
    switched = reloads_after - reloads_before
    print(
        f"\n  engine loads during run: {switched} "
        f"({'zero-cost switching confirmed' if switched <= 1 else 'PROFILES ARE RELOADING'})"
    )
    if switched > 1:
        failures += 1

    print()
    print("all profiles OK" if not failures else f"!! {failures} check(s) failed")
    return 1 if failures else 0


def _reload_count() -> int:
    """Count engine load events in the server log, to detect variant thrash."""
    log = Path.home() / ".omlx" / "logs" / "server.log"
    if not log.exists():
        return 0
    try:
        text = log.read_text(errors="replace")
    except OSError:
        return 0
    return text.count("VLMBatchedEngine loaded")


if __name__ == "__main__":
    raise SystemExit(main())
