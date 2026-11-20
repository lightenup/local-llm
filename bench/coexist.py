#!/usr/bin/env python3
"""Does the lineup still coexist when one model's engine changes?

The lineup is three models resident together: the 8-bit behind `coder`,
`reasoner` and `long`, gemma-26B-A4B behind `fast`, and gemma-31B as the
additive tier. Each fits beside the others only because the prefill guard
leaves room for the KV of a realistic prompt (FINDINGS §27, §32). Splash
changes the 8-bit's side of that budget: it runs in its own process, plans
a fixed memory budget at start-up (weights, drafter, KV pool and prefix
cache) and is exempt from oMLX's prefill guard.

So this loads the whole lineup, then sends a realistic prompt to each model
in turn, several rounds, and records after every call what is still loaded,
the pool's accounting and the machine's memory. A refusal, an eviction or a
reload on alternation is the failure; a pass means every call was served and
nothing left the pool.

    python3 bench/coexist.py --label coexist_splash_8bit
    python3 bench/coexist.py --prompt-tokens 16384 --rounds 3

Leaves the loaded set as it found it.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine_info import engine_snapshot, engine_tag  # noqa: E402
from omlx_client import MODEL, Omlx, OmlxError  # noqa: E402

GIB = 2**30

# Served ids, one profile per lineup model, in the order the roles alternate.
LINEUP = (
    f"{MODEL}:qwen-agent",
    "gemma-4-26B-A4B-it-oQ6e-mtp:g4m-agent",
    "gemma-4-31B-it-qat-OptiQ-4bit:g4-agent",
)

FILLER = (
    "The quarterly maintenance log records routine inspection of pumps, "
    "valves, and seals with no anomalies detected. Torque readings stayed "
    "inside the documented tolerance band and no corrective work was raised. "
)
# Characters per token is only for sizing; prompt_tokens is read back.
CHARS_PER_TOKEN = 5.7


def base(model_id: str) -> str:
    return model_id.split(":", 1)[0]


def loaded(o: Omlx) -> list[str]:
    return sorted(m["id"] for m in o.models() if m.get("loaded"))


def machine_gb() -> dict:
    """Active + wired and wired-only, in GB, from vm_stat."""
    out = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
    ps = int(re.search(r"page size of (\d+)", out).group(1))
    act = int(re.search(r"Pages active:\s+(\d+)", out).group(1))
    wired = int(re.search(r"Pages wired down:\s+(\d+)", out).group(1))
    return {"active_wired_gb": round((act + wired) * ps / 1e9, 2),
            "wired_gb": round(wired * ps / 1e9, 2)}


def pool(o: Omlx) -> dict:
    h = o.health().get("engine_pool") or {}
    return {
        "pool_gib": round((h.get("current_model_memory") or 0) / GIB, 2),
        "ceiling_gib": round((h.get("final_ceiling") or 0) / GIB, 2),
    }


def sizes(o: Omlx) -> dict:
    """The pool's per-model accounting, and Splash's live footprint."""
    out = {}
    for m in o.get("/admin/api/models")["models"]:
        if m.get("loaded"):
            out[m["id"]] = {
                "estimated_gib": round((m.get("estimated_size") or 0) / GIB, 2),
                "actual_gib": round((m.get("actual_size") or 0) / GIB, 2)
                if m.get("actual_size") else None,
                "external_engine": m.get("external_engine"),
            }
    return out


def prompt(tokens: int, tag: str) -> str:
    reps = max(1, int(tokens * CHARS_PER_TOKEN) // len(FILLER))
    return (f"[{tag}]\n" + FILLER * reps
            + "\n\nIn two sentences, what kind of document is this?")


def call(o: Omlx, model: str, text: str, max_tokens: int) -> dict:
    t0 = time.perf_counter()
    try:
        r = o.chat([{"role": "user", "content": text}], model=model,
                   max_tokens=max_tokens, timeout=1800)
    except (OmlxError, OSError) as e:
        return {"ok": False, "s": round(time.perf_counter() - t0, 1),
                "error": f"{type(e).__name__}: {str(e)[:400]}"}
    u = r.get("usage") or {}
    return {
        "ok": True,
        "s": round(time.perf_counter() - t0, 1),
        "prompt_tokens": u.get("prompt_tokens"),
        "cached_tokens": (u.get("prompt_tokens_details") or {}).get("cached_tokens"),
        "completion_tokens": u.get("completion_tokens"),
        "finish": (r.get("choices") or [{}])[0].get("finish_reason"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", default=list(LINEUP),
                    help="served ids, alternated in this order")
    ap.add_argument("--prompt-tokens", type=int, default=16384)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--max-tokens", type=int, default=128)
    ap.add_argument("--label", default="coexist")
    args = ap.parse_args()

    o = Omlx()
    before = loaded(o)
    bases = list(dict.fromkeys(base(m) for m in args.models))
    print(f"== coexistence: {args.models}")
    print(f"   prompt ~{args.prompt_tokens} tokens, {args.rounds} rounds, "
          f"loaded before: {before}")

    rec: dict = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "request": vars(args),
        "loaded_before": before,
    }
    try:
        load_rows = []
        for b in bases:
            t0 = time.perf_counter()
            try:
                o.load(b)
                row = {"model": b, "ok": True}
            except (OmlxError, OSError) as e:
                row = {"model": b, "ok": False, "error": str(e)[:400]}
            row["s"] = round(time.perf_counter() - t0, 1)
            row["loaded"] = loaded(o)
            row.update(pool(o))
            load_rows.append(row)
            print(f"   load {b}: {'ok' if row['ok'] else 'FAILED'} {row['s']}s "
                  f"pool {row['pool_gib']} GiB, loaded {len(row['loaded'])}")
        rec["loads"] = load_rows
        rec["after_load"] = {"loaded": loaded(o), "sizes": sizes(o),
                             **pool(o), **machine_gb()}
        rec["engine"] = engine_snapshot(o, MODEL)
        print(f"   engine of {MODEL}: {engine_tag(rec)}")
        print(f"   after load: {rec['after_load']}")

        calls = []
        for rnd in range(args.rounds):
            for m in args.models:
                # A fresh tag per call, so no call is a prefix-cache hit.
                r = call(o, m, prompt(args.prompt_tokens, f"{args.label} r{rnd} {m}"),
                         args.max_tokens)
                r.update({"round": rnd, "model": m, "loaded": loaded(o),
                          **pool(o), **machine_gb()})
                r["evicted"] = [b for b in bases if b not in r["loaded"]]
                calls.append(r)
                status = "ok" if r["ok"] else f"FAILED {r['error'][:160]}"
                print(f"   r{rnd} {m:<45} {r['s']:>6}s {status}  "
                      f"pool {r['pool_gib']} GiB  mem {r['active_wired_gb']} GB"
                      + (f"  EVICTED {r['evicted']}" if r["evicted"] else ""))
        rec["calls"] = calls
        rec["after_calls"] = {"loaded": loaded(o), "sizes": sizes(o),
                              **pool(o), **machine_gb()}
        served = sum(1 for c in calls if c["ok"])
        evictions = sum(1 for c in calls if c["evicted"])
        rec["summary"] = {
            "calls": len(calls),
            "served": served,
            "calls_with_eviction": evictions,
            "peak_pool_gib": max([c["pool_gib"] for c in calls] or [0]),
            "peak_active_wired_gb": max([c["active_wired_gb"] for c in calls] or [0]),
            "coexists": served == len(calls) and evictions == 0
                        and all(r["ok"] for r in load_rows),
        }
        print(f"\n   VERDICT: {'PASS' if rec['summary']['coexists'] else 'FAIL'} "
              f"{rec['summary']}")
    finally:
        for b in bases:
            if b not in before:
                try:
                    o.unload(b)
                except (OmlxError, OSError):
                    pass
        for b in before:
            try:
                o.load(b)
            except (OmlxError, OSError) as e:
                print(f"   restore {b}: {str(e)[:160]}")
        rec["loaded_after_restore"] = loaded(o)
        Omlx.save(args.label, rec)
        print(f"   restored: {rec['loaded_after_restore']}")
    return 0 if rec.get("summary", {}).get("coexists") else 1


if __name__ == "__main__":
    raise SystemExit(main())
