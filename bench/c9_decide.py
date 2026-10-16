#!/usr/bin/env python3
"""The measurement that decides C9: Coder-Next at N=4 vs the 27B at N=1.

Why this replaces the ratio arithmetic. Every cost number so far — §15's 1.60x,
this repo's 1.75x / 2.40x / 4.84x — is a ratio of a model against ITSELF at a
different N. The decision is not that. The decision is between two different
things a request could be sent to:

    arm A   `fast` (Coder-Next) sampled N=4, gated by a real verifier
    arm B   `coder` (the 27B) once, greedy

Composing ratios to compare those two smuggles in an assumption about the
Coder-Next/27B speed ratio that was itself measured in a different regime. So
both arms are run here head to head, same problems, same verifier, same warm
shared prefix, and the answer is read off wall-clock and pass count directly.

THE REGIME IS THE POINT. Earlier cost runs used ~30-token prompts, where the
prefix cache (2,048-token blocks, whole blocks only) can never engage, and short
uniform generations, where the straggler effect that early exit fixes barely
exists. Both choices flattered one side. This uses:

  - a ~4k-token system prefix, shared and pre-warmed, like an agent's
  - livecodebench problems Coder-Next actually failed, so the task can
    discriminate (the earlier cost prompts passed 4/4 at N=1 and measured nothing)
  - generation lengths left free, so the straggler is real

Executes model-generated code; same sandbox caveat as bench/lcb_audit.py.

    python3 bench/c9_decide.py --limit 12
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import statistics
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from lcb_audit import classify, extract_last_code_block, load_dataset, suite  # noqa: E402

PROXY = os.environ.get("PROXY_BASE", "http://127.0.0.1:4000")
ROOT = pathlib.Path(__file__).resolve().parent.parent

# Stands in for an agent's system prompt: large enough to cross several 2,048
# token cache blocks, identical across every call so it is warm after the first.
AGENT_PREFIX = (
    "You are a coding agent operating in a repository. Follow existing "
    "conventions. Prefer standard library. Read input from stdin and write to "
    "stdout exactly as specified. Do not print prompts or extra text. "
) * 90


def _key() -> str:
    for line in (ROOT / "proxy" / ".env").read_text().splitlines():
        if line.startswith("LITELLM_MASTER_KEY="):
            return line.split("=", 1)[1].strip()
    return ""


def complete(model: str, question: str, temperature: float, max_tokens: int,
             retries: int = 2):
    body = {"model": model, "temperature": temperature, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": AGENT_PREFIX},
                         {"role": "user", "content": question}]}
    last = ""
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            f"{PROXY}/v1/chat/completions", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {_key()}"})
        t0 = time.perf_counter()
        try:
            d = json.loads(urllib.request.urlopen(req, timeout=1800).read())
        except urllib.error.HTTPError as e:
            # Surface the body. A bare "HTTP 400" from a proxy in front of a
            # local server is unactionable, and these have been transient.
            last = f"{e.code}: {e.read().decode(errors='replace')[:300]}"
            print(f"      [retry {attempt + 1}/{retries}] {model}: {last}",
                  flush=True)
            time.sleep(2 * (attempt + 1))
            continue
        u = d.get("usage") or {}
        return (d["choices"][0]["message"].get("content") or "",
                time.perf_counter() - t0,
                (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0))
    raise RuntimeError(f"{model} failed after {retries} retries: {last}")


def arm_bon(model, question, item, n, temperature, max_tokens):
    """Sample N, verify on arrival, stop at the first pass."""
    t0 = time.perf_counter()
    checked, passed = 0, False
    pool = ThreadPoolExecutor(max_workers=n)
    try:
        futs = [pool.submit(complete, model, question, temperature, max_tokens)
                for _ in range(n)]
        for f in as_completed(futs):
            text, _, _ = f.result()
            checked += 1
            if classify(extract_last_code_block(text), item)[0] == "passes_now":
                passed = True
                break
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return passed, time.perf_counter() - t0, checked


def arm_single(model, question, item, max_tokens):
    t0 = time.perf_counter()
    text, _, _ = complete(model, question, 0.0, max_tokens)
    ok = classify(extract_last_code_block(text), item)[0] == "passes_now"
    return ok, time.perf_counter() - t0, 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=12)
    ap.add_argument("--n", type=int, default=4)
    ap.add_argument("--temperature", type=float, default=0.7)
    ap.add_argument("--max-tokens", type=int, default=3072)
    ap.add_argument("--sampler", default="fast")
    ap.add_argument("--strong", default="coder")
    ap.add_argument("--label", default="acc_codernext_code")
    args = ap.parse_args()

    ds = load_dataset()
    res = suite(args.label)
    problems = [q for q in res["question_results"]
                if not q["correct"] and str(q["id"]) in ds][:args.limit]
    print(f"{len(problems)} problems {args.sampler!r} failed in {args.label}\n")

    # Warm the shared prefix so neither arm pays for the other's cold prefill.
    print("warming the shared prefix...")
    _, _, c = complete(args.sampler, "say ok", 0.0, 8)
    _, _, c2 = complete(args.strong, "say ok", 0.0, 8)
    _, _, c = complete(args.sampler, "say ok", 0.0, 8)
    _, _, c2 = complete(args.strong, "say ok", 0.0, 8)
    print(f"  cached tokens: {args.sampler}={c}  {args.strong}={c2}\n")

    rows = []
    for i, q in enumerate(problems, 1):
        item = ds[str(q["id"])]
        a_ok, a_s, a_n = arm_bon(args.sampler, q["question"], item, args.n,
                                 args.temperature, args.max_tokens)
        b_ok, b_s, _ = arm_single(args.strong, q["question"], item,
                                  args.max_tokens)
        rows.append({"id": q["id"], "bon_pass": a_ok, "bon_s": round(a_s, 1),
                     "bon_checked": a_n, "strong_pass": b_ok,
                     "strong_s": round(b_s, 1)})
        print(f"  {i:2d}/{len(problems)} id={q['id']:<6} "
              f"BoN({args.n}) {'PASS' if a_ok else 'fail'} {a_s:6.1f}s "
              f"({a_n} checked)   27B {'PASS' if b_ok else 'fail'} {b_s:6.1f}s",
              flush=True)

    a_pass = sum(r["bon_pass"] for r in rows)
    b_pass = sum(r["strong_pass"] for r in rows)
    a_tot = sum(r["bon_s"] for r in rows)
    b_tot = sum(r["strong_s"] for r in rows)
    n = len(rows)

    print(f"\n{'='*62}\n  arm A: {args.sampler} best-of-{args.n} + verifier")
    print(f"     recovered {a_pass}/{n}   total {a_tot:7.1f}s   "
          f"median {statistics.median(r['bon_s'] for r in rows):6.1f}s")
    print(f"  arm B: {args.strong} greedy, N=1")
    print(f"     recovered {b_pass}/{n}   total {b_tot:7.1f}s   "
          f"median {statistics.median(r['strong_s'] for r in rows):6.1f}s")
    print(f"\n  wall-clock ratio A/B : {a_tot / b_tot:.2f}x"
          f"   ({'BoN slower' if a_tot > b_tot else 'BoN faster'})")
    if a_tot and b_tot:
        print(f"  passes per minute    : A {a_pass / (a_tot/60):.2f}   "
              f"B {b_pass / (b_tot/60):.2f}")
    print(f"\n  VERDICT: ", end="")
    if a_pass > b_pass and a_tot < b_tot:
        print("best-of-N wins on BOTH quality and speed -> wire C9")
    elif a_pass <= b_pass and a_tot >= b_tot:
        print("best-of-N is worse on BOTH -> close C9")
    else:
        print("mixed - it is a trade, not a win; decide on which axis matters")

    out = {"sampler": args.sampler, "strong": args.strong, "n": args.n,
           "problems": n, "bon_recovered": a_pass, "strong_recovered": b_pass,
           "bon_total_s": round(a_tot, 1), "strong_total_s": round(b_tot, 1),
           "ratio_A_over_B": round(a_tot / b_tot, 3) if b_tot else None,
           "rows": rows}
    (ROOT / "results" / "c9_decide.json").write_text(json.dumps(out, indent=2))
    print("\nsaved -> results/c9_decide.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
