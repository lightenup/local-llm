#!/usr/bin/env python3
"""Best-of-N: does Coder-Next's speed buy back its accuracy?

Backlog D2. The working notes argued that continuous batching makes best-of-N
nearly free locally, so wherever a hard verifier exists -- compiles, tests pass,
diagram renders -- you sample N and keep what passes, and the only cost is watts.
FINDINGS section 8 argued the opposite from measurement: 8 concurrent requests
bought 1.38x aggregate throughput for 4.9x p95 latency, and batch 2 was worse
than batch 1, because those requests were prefill-bound.

Neither settles it. That concurrency test used *different* prompts; best-of-N
sends one prompt N times, which is the batching best case rather than the worst,
because the shared prefix is prefilled once and only decode is duplicated.

This matters more after FINDINGS section 14. Coder-Next decodes ~2x faster than
the 27B and produces materially worse end-to-end programs, and a system prompt
does not fix that. Best-of-N against a verifier is the remaining route by which
its speed could be converted into quality.

Two things are measured, because either one alone is useless:

  cost   Wall-clock to all-N-complete at N = 1, 2, 4, 8 on one identical prompt.
         Aggregate tokens/sec is the wrong statistic -- nobody waits for the
         average, they wait for the last one to finish.
  value  pass@N on livecodebench problems the model failed greedily, verified by
         the same execute-and-compare path the suite uses. Sampling needs
         temperature above zero to produce diversity, so the greedy baseline is
         re-measured here rather than assumed from the suite.

    python3 bench/best_of_n.py --cost
    python3 bench/best_of_n.py --value --limit 40 --n 4

Executes model-generated code, same sandbox limits as bench/lcb_audit.py.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lcb_audit import classify, extract_last_code_block, load_dataset, suite  # noqa: E402
from omlx_client import RESULTS_DIR, Omlx  # noqa: E402

COST_PROMPT = (
    "Write a Python function that finds the longest palindromic substring of a "
    "string. Include a short docstring and handle the empty-string case."
)


def cost(o: Omlx, model: str, ns: list[int], max_tokens: int) -> list[dict]:
    """Wall-clock to all-N-complete, one identical prompt fanned out N ways."""
    rows = []
    msgs = [{"role": "user", "content": COST_PROMPT}]
    for n in ns:
        def one(_):
            return o.timed_chat(msgs, model=model, max_tokens=max_tokens,
                                temperature=0.7, timeout=900)
        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=n) as pool:
            rs = list(pool.map(one, range(n)))
        wall = time.perf_counter() - t0
        toks = sum(r["completion_tokens"] for r in rs)
        slowest = max(r["wall_s"] for r in rs)
        cached = [r["cached_tokens"] for r in rs]
        rows.append({
            "n": n,
            "wall_to_all_s": round(wall, 2),
            "slowest_stream_s": round(slowest, 2),
            "completion_tokens": toks,
            "aggregate_tps": round(toks / wall, 1),
            "per_stream_tps": round(toks / wall / n, 1),
            "cached_tokens": cached,
        })
        print(f"  N={n:<2} wall_to_all={wall:6.2f}s  aggregate={toks / wall:6.1f} t/s "
              f"per-stream={toks / wall / n:5.1f} t/s  prefix_cached={cached}")
    base = rows[0]["wall_to_all_s"]
    print(f"\n  cost of N relative to N=1 (the number that decides best-of-N):")
    for r in rows:
        print(f"    N={r['n']:<2} {r['wall_to_all_s'] / base:4.2f}x wall-clock")
    return rows


def value(o: Omlx, model: str, label: str, n: int, limit: int,
          temperature: float, max_tokens: int) -> dict:
    """pass@1 greedy vs pass@N sampled, on problems the suite scored wrong."""
    ds = load_dataset()
    res = suite(label)
    failed = [q for q in res["question_results"]
              if not q["correct"] and str(q["id"]) in ds][:limit]
    print(f"== pass@{n} on {len(failed)} problems the suite got wrong "
          f"(temperature {temperature})")

    recovered, greedy_ok, rows = 0, 0, []
    for i, q in enumerate(failed, 1):
        item = ds[str(q["id"])]
        msgs = [{"role": "user", "content": q["question"]}]

        # Re-measure greedy rather than trusting the suite's verdict: this path
        # differs from the harness in batching and template, and section 14
        # found one case where the two disagreed.
        g = o.timed_chat(msgs, model=model, max_tokens=max_tokens,
                         temperature=0.0, timeout=900)
        g_ok = classify(extract_last_code_block(g["text"]), item)[0] == "passes_now"
        greedy_ok += g_ok

        def one(_):
            return o.timed_chat(msgs, model=model, max_tokens=max_tokens,
                                temperature=temperature, timeout=900)
        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=n) as pool:
            cands = list(pool.map(one, range(n)))
        wall = time.perf_counter() - t0
        buckets = [classify(extract_last_code_block(c["text"]), item)[0] for c in cands]
        any_ok = "passes_now" in buckets
        recovered += any_ok
        rows.append({"id": q["id"], "greedy_passes": g_ok, "pass_at_n": any_ok,
                     "buckets": buckets, "wall_s": round(wall, 1)})
        if i % 10 == 0:
            print(f"   {i}/{len(failed)}  greedy {greedy_ok}  pass@{n} {recovered}",
                  flush=True)

    walls = [r["wall_s"] for r in rows]
    print(f"\n  greedy re-check recovered {greedy_ok}/{len(failed)} "
          f"(the suite scored all of these wrong)")
    print(f"  pass@{n} recovered {recovered}/{len(failed)} "
          f"({recovered / len(failed):.0%})" if failed else "  no problems")
    print(f"  median wall per problem for N={n}: {statistics.median(walls):.1f}s")
    return {"n": n, "temperature": temperature, "attempted": len(failed),
            "greedy_recovered": greedy_ok, "pass_at_n_recovered": recovered,
            "median_wall_s": round(statistics.median(walls), 1), "rows": rows}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="Qwen3-Coder-Next-6bit")
    ap.add_argument("--cost", action="store_true")
    ap.add_argument("--value", action="store_true")
    ap.add_argument("--label", default="acc_codernext_code")
    ap.add_argument("--n", type=int, default=4)
    ap.add_argument("--ns", type=int, nargs="+", default=[1, 2, 4, 8])
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--temperature", type=float, default=0.7)
    ap.add_argument("--max-tokens", type=int, default=16384)
    args = ap.parse_args()
    if not (args.cost or args.value):
        args.cost = args.value = True

    o = Omlx()
    o.load(args.model)
    out: dict = {"model": args.model}

    if args.cost:
        print(f"== cost: wall-clock to all-N-complete on {args.model}")
        out["cost"] = cost(o, args.model, args.ns, 512)
    if args.value:
        out["value"] = value(o, args.model, args.label, args.n, args.limit,
                             args.temperature, args.max_tokens)

    path = RESULTS_DIR / "best_of_n.json"
    path.write_text(json.dumps(out, indent=2) + "\n")
    print(f"\n  wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
