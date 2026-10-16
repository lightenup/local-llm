#!/usr/bin/env python3
"""Queue oMLX accuracy suites and wait for them, saving results per label.

Suite groups map to the profiles being validated:

    code      humaneval, mbpp, livecodebench
    reason    mmlu_pro, gsm8k, mathqa
    general   mmlu, arc_challenge, hellaswag, winogrande, truthfulqa
    safety    safetybench, bbq

    python3 bench/accuracy.py code --label code_nothink --samples 0
    python3 bench/accuracy.py reason --label reason_think --thinking --samples 250

NOTE: oMLX uploads any suite result with >=100 questions to omlx.ai community
benchmarks (no opt-out setting exists). Use --samples 99 to stay fully local.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from omlx_client import MODEL, Omlx, OmlxError  # noqa: E402

GROUPS: dict[str, list[str]] = {
    "code": ["humaneval", "mbpp", "livecodebench"],
    "reason": ["mmlu_pro", "gsm8k", "mathqa"],
    "general": ["mmlu", "arc_challenge", "hellaswag", "winogrande", "truthfulqa"],
    "safety": ["safetybench", "bbq"],
}
UPLOAD_THRESHOLD = 100  # _MIN_UPLOAD_QUESTIONS in omlx/admin/accuracy_upload.py


def wait_for_idle(o: Omlx, timeout: float = 36000.0, poll: float = 20.0) -> dict:
    """Block until the accuracy queue drains.

    Transient polling failures must NOT abort the wait. The status endpoint is
    served by the same process that is running the benchmark, so it becomes
    slow to answer exactly when the benchmark is heaviest — a `livecodebench`
    pass saturated it enough to blow the client's 60 s request timeout on
    2026-09-11, and the resulting exception threw away a run that went on to
    complete server-side 3.7 h later. Losing the poll is not losing the work.
    """
    deadline = time.time() + timeout
    last = ""
    consecutive_errors = 0
    while time.time() < deadline:
        try:
            st = o.accuracy_status()
            consecutive_errors = 0
        except Exception as e:
            # Back off and keep waiting. Only give up if the server has been
            # unreachable long enough that it is plainly not coming back.
            consecutive_errors += 1
            if consecutive_errors >= 30:
                raise OmlxError(
                    f"accuracy status unreachable for {consecutive_errors} "
                    f"consecutive polls: {type(e).__name__}: {e}"
                ) from None
            print(f"    [accuracy] poll failed ({type(e).__name__}), "
                  f"retry {consecutive_errors}/30 — the run is probably still "
                  f"going", flush=True)
            time.sleep(min(poll * consecutive_errors, 120))
            continue
        if not st.get("running") and not st.get("queue"):
            return st
        msg = (
            f"running={st.get('running')} model={st.get('current_model')} "
            f"bench={str(st.get('current_bench_id'))[:8]} "
            f"queued={len(st.get('queue') or [])}"
        )
        if msg != last:
            print(f"    [accuracy] {msg}", flush=True)
            last = msg
        time.sleep(poll)
    raise TimeoutError("accuracy queue did not drain in time")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("group", choices=[*GROUPS, "all"])
    ap.add_argument("--label", required=True)
    ap.add_argument(
        "--samples",
        type=int,
        default=0,
        help="per-suite sample size; 0 = full dataset. <100 avoids the omlx.ai upload.",
    )
    ap.add_argument("--batch", type=int, default=8, choices=[1, 2, 4, 8, 16, 32])
    ap.add_argument("--thinking", action="store_true")
    ap.add_argument(
        "--sampling",
        default="deterministic",
        choices=["deterministic", "model_settings"],
    )
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--no-wait", action="store_true")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve and print the suites, then stop. --no-wait still queues a "
             "real run on the server, which is not what you want when you are "
             "only checking that the arguments parse.",
    )
    ap.add_argument(
        "--suites",
        nargs="+",
        metavar="SUITE",
        help="restrict to a subset of the group's suites. Settling one suite at "
             "full dataset costs hours, and paying for its whole group as well "
             "is usually the difference between fitting a night and not.",
    )
    args = ap.parse_args()

    suites = (
        [s for g in GROUPS.values() for s in g]
        if args.group == "all"
        else GROUPS[args.group]
    )
    if args.suites:
        unknown = [s for s in args.suites if s not in suites]
        if unknown:
            print(f"!! not in group '{args.group}': {unknown}; available: {suites}")
            return 2
        suites = list(args.suites)
    benchmarks = {s: args.samples for s in suites}

    o = Omlx()
    uploads = args.samples == 0 or args.samples >= UPLOAD_THRESHOLD
    print(f"== accuracy '{args.label}' group={args.group} on {args.model}")
    print(f"   suites={suites}")
    print(
        f"   samples={'full' if args.samples == 0 else args.samples} "
        f"batch={args.batch} thinking={args.thinking} sampling={args.sampling}"
    )
    print(f"   omlx.ai upload: {'YES' if uploads else 'no (under 100 samples)'}")

    if args.dry_run:
        print("   dry run: nothing queued")
        return 0

    # The results endpoint returns an append-ordered history and its records
    # carry no id or timestamp, so identity cannot tell a re-run of a suite from
    # the earlier run of the same suite on the same model. Position can.
    before_count = len(o.accuracy_results()["results"])
    o.bench_accuracy(
        benchmarks=benchmarks,
        batch_size=args.batch,
        enable_thinking=args.thinking,
        sampling_profile=args.sampling,
        model_id=args.model,
    )
    if args.no_wait:
        print("   queued; not waiting")
        return 0

    wait_for_idle(o)
    allres = o.accuracy_results()["results"]
    fresh = allres[before_count:]
    if not fresh:
        # History was rotated or trimmed while the run was in flight; the
        # trailing window is the best remaining guess.
        fresh = allres[-len(benchmarks):]

    payload = {
        "label": args.label,
        "group": args.group,
        "model_id": args.model,
        "request": {
            "benchmarks": benchmarks,
            "batch_size": args.batch,
            "enable_thinking": args.thinking,
            "sampling_profile": args.sampling,
        },
        "variant": {
            k: v
            for k, v in o.settings(args.model).items()
            # `qwen4_` catches qwen4_ple_ssd_offload, which no other prefix
            # matches and which decides a Flash-Next run's whole memory shape.
            if k.startswith(
                ("qwen35_ane", "qwen4_", "turboquant", "vlm_mtp", "mtp_", "dflash")
            )
            and v not in (None, False)
        },
        "results": fresh or allres,
    }
    Omlx.save(f"acc_{args.label}", payload)

    print(f"\n  {'suite':<16} {'score':>8} {'correct':>9} {'total':>7} {'time s':>8}  upload")
    print("  " + "-" * 62)
    for r in payload["results"]:
        acc = r.get("accuracy")
        score = f"{acc * 100:.1f}%" if isinstance(acc, (int, float)) else str(acc)
        up = (r.get("upload") or {})
        upmsg = up.get("skipped") or ("ok" if up.get("id") or up.get("ok") else "-")
        print(
            f"  {str(r.get('benchmark')):<16} {score:>8} "
            f"{str(r.get('correct')):>9} {str(r.get('total')):>7} "
            f"{str(round(r.get('duration', 0) or 0, 1)):>8}  {upmsg}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
