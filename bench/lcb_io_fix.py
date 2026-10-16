#!/usr/bin/env python3
"""Test whether stating the I/O contract recovers Coder-Next's livecodebench losses.

FINDINGS section 14 found that Coder-Next's 16.6-point livecodebench deficit is
mostly not wrong algorithms. It produced *fewer* genuinely wrong answers than the
27B, 71 against 79, and lost on crashes: 137 `invalid literal for int()` against
85, and 44 answers that ran and printed nothing against 8. The suite hands over a
LeetCode-style problem description, says "read stdin and print stdout", and never
states how the inputs are serialised, so the model has to guess.

That claim implies something testable. If the failures are contract-guessing
rather than incompetence, then stating the contract should recover a large share
of them with no change to the model. If barely anything recovers, the section is
wrong and the deficit is real algorithmic weakness wearing a disguise.

Only the I/O-shaped failures are retried. Retrying `wrong_output` would muddy the
result, because those are the ones section 14 already accepts as genuine.

    python3 bench/lcb_io_fix.py acc_codernext_code --limit 60

Writes results/lcb_io_fix.json. Executes model-generated code, same sandbox
limits as bench/lcb_audit.py.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lcb_audit import (  # noqa: E402
    classify,
    extract_last_code_block,
    load_dataset,
    suite,
)
from omlx_client import RESULTS_DIR, Omlx  # noqa: E402

# The buckets section 14 attributes to the unstated contract rather than to
# getting the problem wrong.
IO_BUCKETS = {
    "crash_ValueError",
    "no_output",
    "stdin_format_unpack",
    "stdin_format_eof",
    "crash_IndentationError",
    "case_mismatch",
    "whitespace_mismatch",
    "crash_nonzero_exit",
    "no_code_extracted",
}

# Deliberately only states the contract the suite assumes and never states.
# It gives away nothing about any particular problem, so a recovery here is the
# model doing the same work against clearer instructions -- not a hint.
SYSTEM_PROMPT = (
    "You are solving a competitive programming problem that will be run as a "
    "script.\n"
    "- Read ALL input from stdin. Values may be on separate lines or on one "
    "line; read robustly, for example with sys.stdin.read().split().\n"
    "- The program must execute and print its answer when run. Do not define a "
    "function and leave it uncalled.\n"
    "- Print only the required output, nothing else. Match the expected "
    "formatting exactly, including capitalisation of booleans.\n"
    "- Output one Python code block and no explanation."
)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("label", help="accuracy result label to retry")
    ap.add_argument("--model", default="Qwen3-Coder-Next-6bit")
    ap.add_argument("--limit", type=int, default=60)
    # Must match omlx/eval/livecodebench.py::get_max_tokens(). A lower cap
    # truncates long answers into the no_output bucket and fakes a failure that
    # the suite itself never saw -- which is exactly what the first run of this
    # script did at 4096.
    ap.add_argument("--max-tokens", type=int, default=16384)
    args = ap.parse_args()

    ds = load_dataset()
    res = suite(args.label)
    audit_path = RESULTS_DIR / "lcb_audit.json"
    if not audit_path.exists():
        print("!! run bench/lcb_audit.py --json first")
        return 1
    audits = {a["label"]: a for a in json.loads(audit_path.read_text())}
    arm = audits.get(args.label) or audits.get(f"acc_{args.label}")
    if arm is None:
        print(f"!! {args.label} not in {audit_path}")
        return 1

    targets = [qid for qid, bucket, _ in arm["rows"] if bucket in IO_BUCKETS]
    by_id = {q["id"]: q for q in res["question_results"]}
    targets = [t for t in targets if str(t) in ds and t in by_id][: args.limit]
    print(f"== retrying {len(targets)} I/O-shaped failures on {args.model}")
    print(f"   baseline accuracy {res['accuracy']:.3f} over {res['total']}")

    o = Omlx()
    o.load(args.model)
    rows, recovered = [], 0
    for n, qid in enumerate(targets, 1):
        q = by_id[qid]
        r = o.timed_chat(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": q["question"]},
            ],
            model=args.model,
            max_tokens=args.max_tokens,
            temperature=0.0,
            timeout=900,
        )
        code = extract_last_code_block(r["text"])
        bucket, detail = classify(code, ds[str(qid)])
        ok = bucket == "passes_now"
        recovered += ok
        rows.append({"id": qid, "bucket": bucket, "detail": detail, "passes": ok,
                     "finish_reason": r.get("finish_reason"),
                     "completion_tokens": r.get("completion_tokens")})
        if n % 10 == 0:
            print(f"   {n}/{len(targets)}  recovered so far {recovered}", flush=True)

    print(f"\n  recovered {recovered}/{len(targets)} "
          f"({recovered / len(targets):.0%}) of the I/O-shaped failures")
    print("\n  what the still-failing ones became:")
    for bucket, count in Counter(
        r["bucket"] for r in rows if not r["passes"]
    ).most_common():
        print(f"    {count:>4}  {bucket}")

    # Project the suite score if this recovery rate held across every I/O failure.
    io_total = sum(1 for _, b, _ in arm["rows"] if b in IO_BUCKETS)
    rate = recovered / len(targets) if targets else 0.0
    corrected = res["accuracy"] + (io_total * rate) / res["total"]
    print(f"\n  {io_total} I/O-shaped failures in the full suite; at this rate")
    print(f"  the score would move {res['accuracy']:.3f} -> {corrected:.3f} "
          f"against the 27B's 0.598")

    out = RESULTS_DIR / "lcb_io_fix.json"
    out.write_text(json.dumps({
        "label": args.label, "model": args.model,
        "system_prompt": SYSTEM_PROMPT,
        "retried": len(targets), "recovered": recovered,
        "io_failures_in_suite": io_total,
        "baseline_accuracy": res["accuracy"],
        "projected_accuracy": round(corrected, 4),
        "rows": rows,
    }, indent=2) + "\n")
    print(f"\n  wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
