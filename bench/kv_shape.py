#!/usr/bin/env python3
"""What is the 27B's ~43 GB "KV+SDPA" reservation a function of?

The guard reports it in refusals:

    Prefill would require ~116.98 GB peak (current 73.92 GB + KV+SDPA 43.05 GB)

Two readings were tried and both are wrong:

  "sized to the configured context"  falsified — a ~240k-token prompt on
                                     qwen-long (262k) was ACCEPTED and completed,
                                     where this reading predicts ~86 GB and a
                                     refusal.
  "sized to the prompt"              falsified — the figure sits at 42–43.5 GB
                                     across prompts from 3.4k to 8k tokens.

This settles it by OBSERVATION rather than by the guard's arithmetic, because
the guard only prints during a load/eviction transition and is therefore racy to
provoke. Memory is sampled from /health while a prefill-only request is in
flight, and peak growth above baseline is attributed to that request.

The answer decides whether "reduce a profile's context" is a real lever. It is
the only cheap mitigation for the two-model thrash in A3b if oQ4e disappoints,
and right now it rests on a premise already shown false once.

    python3 bench/kv_shape.py
"""

from __future__ import annotations

import argparse
import json
import random
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from omlx_client import Omlx, OmlxError  # noqa: E402

RESULTS = Path(__file__).resolve().parent.parent / "results"


def host_memory_gb() -> float:
    """Active + wired pages, in GB.

    Not the admin API's `current_model_memory`: that reports model WEIGHTS and
    sat at exactly 30.98 GB through every prompt size, KV included or not.
    Not process RSS either — Metal allocations do not appear there (the server
    shows 0.6 GB while holding a 31 GB model). They land in ACTIVE pages, which
    is what this reads.
    """
    out = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
    ps = int(re.search(r"page size of (\d+)", out).group(1))
    active = int(re.search(r"Pages active:\s+(\d+)", out).group(1))
    wired = int(re.search(r"Pages wired down:\s+(\d+)", out).group(1))
    return (active + wired) * ps / 1e9


class MemorySampler(threading.Thread):
    """Poll host memory until told to stop; keep the peak."""

    def __init__(self, o: Omlx, interval: float = 0.4):
        super().__init__(daemon=True)
        self.o, self.interval = o, interval
        self._stop = threading.Event()
        self.samples: list[float] = []

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.samples.append(host_memory_gb())
            except Exception:
                pass
            self._stop.wait(self.interval)

    def stop(self) -> tuple[float, float]:
        self._stop.set()
        self.join(timeout=5)
        if not self.samples:
            return 0.0, 0.0
        return max(self.samples), min(self.samples)


# A fixed vocabulary sampled without repetition gives prompts that are unique
# per call, so the prefix cache cannot serve them. With repeated filler ("word
# word word...") a 35,567-token prompt completed in 2.4 s — ~15,000 tok/s
# against a measured prefill rate of ~533 — i.e. it was never prefilled at all,
# and the first version of this script measured nothing.
_WORDS = [f"tok{i:05d}" for i in range(60000)]


def unique_prompt(tokens: int) -> str:
    n = max(1, int(tokens / 1.35))
    return " ".join(random.choice(_WORDS) for _ in range(n))


def measure(o: Omlx, model: str, tokens: int) -> dict:
    """Prefill-only request; return peak memory growth over the idle baseline."""
    time.sleep(2)
    baseline = host_memory_gb()
    sampler = MemorySampler(o)
    sampler.start()
    t0 = time.perf_counter()
    err = None
    pt = 0
    try:
        # max_tokens=1 so the cost is prefill, which is where the KV+SDPA peak is.
        r = o.chat([{"role": "user", "content": unique_prompt(tokens)}],
                   model=model, max_tokens=1, temperature=0.0, timeout=2400)
        pt = (r.get("usage") or {}).get("prompt_tokens", 0)
    except OmlxError as e:
        err = str(e)[:200]
    wall = time.perf_counter() - t0
    peak, low = sampler.stop()
    return {"model": model, "requested_tokens": tokens, "prompt_tokens": pt,
            "baseline_gb": round(baseline, 2), "peak_gb": round(peak, 2),
            "growth_gb": round(peak - baseline, 2), "wall_s": round(wall, 1),
            "error": err}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lengths", type=int, nargs="+", default=[2000, 16000, 48000])
    ap.add_argument("--models", nargs="+",
                    default=["Qwen3.8-27B-8bit:qwen-code",
                             "Qwen3.8-27B-8bit:qwen-long"])
    args = ap.parse_args()

    o = Omlx()
    loaded = [m["id"] for m in o.models() if m.get("loaded")]
    print(f"resident: {loaded}")
    if len(loaded) > 1:
        print("  NOTE: more than one model is resident; eviction during the run "
              "will corrupt the baseline. Unload the others first.")
    print()

    rows = []
    for model in args.models:
        print(f"=== {model} ===")
        for n in args.lengths:
            r = measure(o, model, n)
            rows.append(r)
            if r["error"]:
                print(f"  ~{n:>6,} tok  ERROR {r['error'][:90]}")
            else:
                print(f"  {r['prompt_tokens']:>7,} tok  baseline {r['baseline_gb']:6.2f}"
                      f"  peak {r['peak_gb']:6.2f}  growth {r['growth_gb']:+6.2f} GB"
                      f"  [{r['wall_s']:6.1f}s]")
        print()

    # --- reading ----------------------------------------------------------
    ok = [r for r in rows if not r["error"] and r["prompt_tokens"]]
    print("=== reading ===")
    by_model: dict[str, list] = {}
    for r in ok:
        by_model.setdefault(r["model"], []).append(r)

    for model, rs in by_model.items():
        growths = [r["growth_gb"] for r in rs]
        spread = max(growths) - min(growths)
        print(f"  {model}")
        print(f"    growth across {len(rs)} prompt sizes: {growths}  "
              f"(spread {spread:.2f} GB)")
        if spread < 1.0:
            print("    -> flat: the reservation does NOT track prompt length")
        else:
            per_1k = [(r["growth_gb"] / (r["prompt_tokens"] / 1000)) for r in rs]
            print(f"    -> scales with prompt; GB per 1k tokens: "
                  f"{[round(x, 4) for x in per_1k]}")

    if len(by_model) == 2:
        a, b = list(by_model.values())
        pa = {r["requested_tokens"]: r["growth_gb"] for r in a}
        pb = {r["requested_tokens"]: r["growth_gb"] for r in b}
        common = sorted(set(pa) & set(pb))
        print("\n  same prompt, 131k profile vs 262k profile:")
        for n in common:
            d = pb[n] - pa[n]
            print(f"    ~{n:>6,} tok   {pa[n]:+6.2f} vs {pb[n]:+6.2f} GB   "
                  f"delta {d:+.2f}")
        print("    -> if the deltas are ~0, configured CONTEXT does not drive it;")
        print("       reducing a profile's context would NOT free memory.")

    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "kv_shape.json").write_text(json.dumps(rows, indent=2))
    print("\nsaved -> results/kv_shape.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
