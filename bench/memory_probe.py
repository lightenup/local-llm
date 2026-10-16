#!/usr/bin/env python3
"""Why does a 3.4k-token prompt reserve 42.8 GB of KV?

Found by accident during the C9 decision run: with two models resident, the 27B
began refusing prompts it handles trivially in isolation —

    Prefill would require ~115.97 GB peak (current 73.14 GB + KV+SDPA 42.83 GB)
    but metal_cap ceiling is 102.14 GB

42.8 GB of KV for ~3.4k tokens is not prompt-proportional by any reading, so the
allocation is presumably sized to the profile's CONFIGURED context rather than to
the request. If that is right it has three consequences worth knowing:

  - `qwen-long` (262k) should reserve roughly twice `qwen-code` (131k) for the
    same prompt, and the four profiles are not interchangeable memory-wise even
    though FINDINGS §6 treats profile switching as free.
  - §8's verified 262k ceiling was measured with one model resident. Under the
    two-model configuration this repo actually ships, it may not hold.
  - The fix is a ceiling, not a smaller prompt.

METHOD. Provoking the guard by loading a second 64.7 GB model is slow and
destructive. Instead the guard's own ceiling is temporarily lowered, which makes
it refuse and — usefully — print its KV+SDPA estimate in the refusal. The
estimate is a property of the model and request, not of the ceiling, so reading
it this way costs one failed request instead of a model load.

Restores `memory_guard_custom_ceiling_gb` on every exit path, including Ctrl-C.

    python3 bench/memory_probe.py
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from omlx_client import BASE, Omlx, OmlxError  # noqa: E402

RESULTS = Path(__file__).resolve().parent.parent / "results"

# (served id, advertised context) — the four shipped profiles plus the bare
# model, so a profile's reservation can be compared against its own base.
TARGETS = [
    ("Qwen3.8-27B-8bit:qwen-code", 131072),
    ("Qwen3.8-27B-8bit:qwen-agent", 131072),
    ("Qwen3.8-27B-8bit:qwen-reason", 131072),
    ("Qwen3.8-27B-8bit:qwen-long", 262144),
]

GUARD = re.compile(
    r"~([\d.]+)\s*GB peak.*?current\s*([\d.]+)\s*GB.*?KV\+SDPA\s*([\d.]+)\s*GB"
    r".*?ceiling is\s*([\d.]+)\s*GB", re.S)


def probe(o: Omlx, model: str, prompt: str) -> dict | None:
    """Send one request; if the guard refuses, return its arithmetic."""
    body = {"model": model, "max_tokens": 4,
            "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request(
        f"{BASE}/v1/chat/completions", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {o.api_key}"})
    try:
        urllib.request.urlopen(req, timeout=300).read()
        return None  # accepted: the guard did not fire, nothing to read
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        m = GUARD.search(detail)
        if not m:
            return {"unparsed": detail[:300]}
        peak, current, kv, ceiling = (float(x) for x in m.groups())
        return {"peak_gb": peak, "current_gb": current,
                "kv_sdpa_gb": kv, "ceiling_gb": ceiling}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ceiling", type=float, default=20.0,
                    help="temporary guard ceiling, GB; low enough to refuse "
                         "everything so the estimate is always printed")
    args = ap.parse_args()

    o = Omlx()
    before = o.global_settings()["memory"]
    print("memory settings before:", json.dumps(before))
    original = before.get("memory_guard_custom_ceiling_gb", 0.0)

    out: dict = {"before": before, "per_profile": [], "per_length": []}
    try:
        o.update_global_settings({"memory_guard_custom_ceiling_gb": args.ceiling})
        print(f"guard ceiling temporarily set to {args.ceiling} GB\n")

        # --- 1. same tiny prompt, every profile ---------------------------
        # If reservation tracks CONFIGURED context, qwen-long should be ~2x the
        # 131k profiles. If it tracks the prompt, all four should be identical.
        print("=== same ~20-token prompt, each profile ===")
        tiny = "Say ok."
        for model, ctx in TARGETS:
            r = probe(o, model, tiny)
            if r and "kv_sdpa_gb" in r:
                print(f"  {model:32s} ctx={ctx:>7,}  "
                      f"KV+SDPA {r['kv_sdpa_gb']:6.2f} GB")
                out["per_profile"].append({"model": model, "ctx": ctx, **r})
            else:
                print(f"  {model:32s} ctx={ctx:>7,}  (not refused: {r})")

        # --- 2. one profile, growing prompts ------------------------------
        # The control. If reservation is context-sized, these are all equal.
        print("\n=== qwen-code, growing prompt ===")
        for approx in (20, 2000, 8000):
            text = "word " * max(1, approx // 2)
            r = probe(o, "Qwen3.8-27B-8bit:qwen-code", text)
            if r and "kv_sdpa_gb" in r:
                print(f"  ~{approx:>6,} tokens  KV+SDPA {r['kv_sdpa_gb']:6.2f} GB")
                out["per_length"].append({"approx_tokens": approx, **r})
            else:
                print(f"  ~{approx:>6,} tokens  (not refused: {r})")
    finally:
        o.update_global_settings({"memory_guard_custom_ceiling_gb": original})
        print(f"\nrestored memory_guard_custom_ceiling_gb = {original}")
        print("after:", json.dumps(o.global_settings()["memory"]))

    # --- interpretation ---------------------------------------------------
    prof = {r["model"]: r["kv_sdpa_gb"] for r in out["per_profile"]}
    lens = [r["kv_sdpa_gb"] for r in out["per_length"]]
    print("\n=== reading ===")
    if len(set(round(x, 1) for x in lens)) == 1 and lens:
        print(f"  Prompt length does NOT change the reservation "
              f"({lens[0]:.2f} GB at every length).")
        print("  -> KV is sized to the CONFIGURED CONTEXT, not the request.")
    elif lens:
        print(f"  Reservation varies with prompt length: {lens}")
        print("  -> KV is at least partly request-sized.")
    code = prof.get("Qwen3.8-27B-8bit:qwen-code")
    long_ = prof.get("Qwen3.8-27B-8bit:qwen-long")
    if code and long_:
        print(f"  qwen-long / qwen-code = {long_ / code:.2f}x "
              f"(context ratio is 2.00x)")

    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "memory_probe.json").write_text(json.dumps(out, indent=2))
    print("\nsaved -> results/memory_probe.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
