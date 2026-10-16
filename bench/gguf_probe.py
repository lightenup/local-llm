#!/usr/bin/env python3
"""The GGUF probe: does a 122B MoE at 5 bits beat a dense 27B at 8?

Backlog B5 and A4, re-specified. The original probe was DeepSeek V4-Flash at
IQ2, and it is not runnable here: 110-125 GB of weights against a 107.52 GB
Metal cap, exceeded before any KV. Making it fit means raising
`iogpu.wired_limit_mb` to ~120 GB and leaving macOS ~8 GB, which FINDINGS §H
already documents as thrashing and kernel-panic territory. Worse, at 284B
parameters anything that *would* fit is ~2.5 bits/param — the degraded regime
the rule already predicts loses, so the probe could only have confirmed its own
premise.

`unsloth/Qwen3.5-122B-A10B` fixes all of that. At Q5_K_S it is 86.4 GB (~5.7
bits/param) and at Q4_K_M 76.5 GB, both comfortably under the cap with room for
KV, and both well clear of the degraded regime. It is 10B active against
Coder-Next's 3B and the 27B's 27B dense, so it probes the axis that actually
lost last time. It is the same family as the shipped model, so size is not
confounded with architecture. And it is an Unsloth dynamic quant, which backlog
B5 named as the only real reason to build this lane at all.

WHY IT REPLAYS RATHER THAN RE-SCORES. The shipped 8-bit scored 59.8 on
livecodebench over a specific 500 questions, recorded question by question in
results/acc_ship_code.json. This sends *those same questions* to the GGUF server
and grades with the *same* extract-and-execute path (lcb_audit), then writes its
output in the accuracy-harness shape so bench/paired_compare.py reads it
directly. Comparing two independently-sampled suite scores would resolve nothing
under ~2 points (§8); comparing the same questions pairwise resolves much less.

Executes model-generated code; same sandbox caveat as bench/lcb_audit.py.

    llama-server -m <model.gguf> --port 8080 -c 8192 -ngl 999
    python3 bench/gguf_probe.py --label gguf_q5 --limit 150
    python3 bench/paired_compare.py acc_ship_code acc_gguf_q5
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lcb_audit import classify, extract_last_code_block, load_dataset, suite  # noqa: E402
from omlx_client import RESULTS_DIR  # noqa: E402

ENDPOINT = os.environ.get("GGUF_BASE", "http://127.0.0.1:8080")


def complete(prompt: str, max_tokens: int, temperature: float,
             timeout: float = 1800.0) -> tuple[str, float, dict]:
    # enable_thinking=False is NOT optional: the reference arm ran with
    # `enable_thinking: false`, and Qwen3.5 defaults it ON in llama.cpp's chat
    # template. Left on, reasoning lands in `reasoning_content` and the answer
    # frequently never arrives before max_tokens -- a first probe returned an
    # EMPTY content field after 200 tokens of thinking. Every question would
    # have scored zero for a reason that has nothing to do with the model.
    body = {"model": "local", "temperature": temperature,
            "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request(
        f"{ENDPOINT}/v1/chat/completions", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    msg = d["choices"][0]["message"]
    usage = dict(d.get("usage") or {})
    if msg.get("reasoning_content"):
        usage["reasoning_chars"] = len(msg["reasoning_content"])
    return (msg.get("content") or "", time.perf_counter() - t0, usage)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label", required=True, help="written to results/acc_<label>.json")
    ap.add_argument("--reference", default="acc_ship_code",
                    help="arm whose livecodebench questions are replayed")
    ap.add_argument("--limit", type=int, default=150,
                    help="questions to replay; 0 = all 500")
    ap.add_argument("--max-tokens", type=int, default=3072)
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--model-id", default="Qwen3.5-122B-A10B-GGUF")
    args = ap.parse_args()

    ds = load_dataset()
    ref = suite(args.reference)
    qs = [q for q in ref["question_results"] if str(q["id"]) in ds]
    if args.limit:
        qs = qs[:args.limit]
    ref_correct = sum(1 for q in qs if q["correct"])
    print(f"replaying {len(qs)} livecodebench questions from {args.reference}")
    print(f"reference scored {ref_correct}/{len(qs)} = {ref_correct/len(qs):.1%} "
          f"on this subset")
    print(f"endpoint: {ENDPOINT}\n")

    rows, ok, t_start = [], 0, time.perf_counter()
    for i, q in enumerate(qs, 1):
        item = ds[str(q["id"])]
        try:
            text, wall, usage = complete(q["question"], args.max_tokens,
                                         args.temperature)
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"  {i:3d}/{len(qs)} {q['id']:<10} ERROR {type(e).__name__}",
                  flush=True)
            rows.append({"id": q["id"], "correct": False, "question": q["question"],
                         "predicted": "", "bucket": f"error_{type(e).__name__}",
                         "time_s": 0.0})
            continue
        bucket = classify(extract_last_code_block(text), item)[0]
        good = bucket == "passes_now"
        ok += good
        rows.append({"id": q["id"], "correct": good, "question": q["question"],
                     "predicted": text[:2000], "bucket": bucket,
                     "time_s": round(wall, 1),
                     "completion_tokens": usage.get("completion_tokens", 0)})
        if i % 10 == 0 or i == len(qs):
            el = time.perf_counter() - t_start
            print(f"  {i:3d}/{len(qs)}  {ok} correct ({ok/i:.1%})  "
                  f"{el/i:.1f}s/q  eta {(len(qs)-i)*el/i/60:.0f} min", flush=True)

    # Shaped like the accuracy harness so paired_compare.py reads it unchanged.
    out = {
        "label": args.label, "group": "code", "model_id": args.model_id,
        "request": {"benchmarks": {"livecodebench": len(qs)}, "batch_size": 1,
                    "enable_thinking": False, "sampling_profile": "deterministic"},
        "variant": {"served_by": "llama.cpp", "endpoint": ENDPOINT,
                    "temperature": args.temperature},
        "results": [{
            "model_id": args.model_id, "benchmark": "livecodebench",
            "accuracy": ok / len(qs) if qs else 0.0,
            "total": len(qs), "correct": ok,
            "time_s": round(time.perf_counter() - t_start, 1),
            "question_results": rows,
        }],
        "note": ("Replay of the same livecodebench questions the reference arm "
                 "answered, graded by the same extract-and-execute path, served "
                 "by llama.cpp instead of oMLX."),
    }
    path = RESULTS_DIR / f"acc_{args.label}.json"
    path.write_text(json.dumps(out, indent=2))

    print(f"\n  {args.model_id}: {ok}/{len(qs)} = {ok/len(qs):.1%}")
    print(f"  {args.reference}:  {ref_correct}/{len(qs)} = {ref_correct/len(qs):.1%}")
    print(f"  delta: {(ok - ref_correct)/len(qs):+.1%}")
    print(f"\n  saved -> {path.name}")
    print(f"  now: python3 bench/paired_compare.py {args.reference} acc_{args.label}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
