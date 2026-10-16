#!/usr/bin/env python3
"""Run the oMLX ANE prefill auto-tuner and optionally apply its recommendation.

The tuner sweeps GPU/ANE/CPU prefill split candidates and reports the best
one. It never writes settings itself, so --apply is what makes it stick.

    python3 bench/ane_tune.py --seq 2048 --repeats 2 --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from omlx_client import MODEL, Omlx  # noqa: E402

# Recommendation keys the tuner emits that map onto model settings.
APPLY_KEYS = (
    "qwen35_ane_prefill_enabled",
    "qwen35_ane_prefill_sequence_length",
    "qwen35_ane_prefill_fraction",
    "qwen35_ane_prefill_fused_down",
    "qwen35_ane_prefill_max_layers",
    "qwen35_ane_prefill_dual_ane",
    "qwen35_ane_prefill_gdn",
    "qwen35_ane_prefill_gdn_fraction",
    "qwen35_ane_prefill_gdn_max_layers",
    "qwen35_ane_prefill_cpu_enabled",
    "qwen35_ane_prefill_cpu_fraction",
    "qwen35_ane_prefill_cpu_down_fraction",
    "qwen35_ane_prefill_cpu_gdn_fraction",
    "qwen35_ane_prefill_cpu_threads",
    "qwen35_ane_prefill_cpu_shared_resource",
    "qwen35_ane_prefill_tail_padding_min_tokens",
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seq", type=int, default=2048)
    ap.add_argument("--repeats", type=int, default=2)
    ap.add_argument("--label", default="ane_tune")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--model", default=MODEL)
    args = ap.parse_args()

    o = Omlx()
    print(f"== ANE tuning {args.model} seq={args.seq} repeats={args.repeats}")
    res = o.bench_ane_tune(
        sequence_length=args.seq, repeats=args.repeats, model_id=args.model
    )
    Omlx.save(args.label, res)

    if res.get("status") != "completed":
        print(f"!! tuning {res.get('status')}: {res.get('error_message')}")
        return 1

    rec = res.get("recommendation") or {}
    print("\n-- recommendation --")
    print(json.dumps(rec, indent=2)[:2500])

    cands = res.get("candidates") or res.get("results") or []
    if cands:
        print(f"\n-- candidates ({len(cands)}) --")
        rows = []
        for c in cands:
            rows.append(
                (
                    c.get("label") or c.get("name") or "?",
                    c.get("prefill_tps") or c.get("tps") or c.get("tokens_per_second"),
                    c.get("stage", ""),
                )
            )
        rows.sort(key=lambda r: (r[1] is None, -(r[1] or 0)))
        print(f"  {'candidate':<44} {'prefill t/s':>12}  stage")
        for label, tps, stage in rows:
            print(f"  {str(label):<44} {str(tps):>12}  {stage}")

    if args.apply:
        patch = {k: rec[k] for k in APPLY_KEYS if k in rec}
        if not patch:
            print("\n!! recommendation carries no applicable settings; nothing applied")
            return 1
        print(f"\n-- applying {json.dumps(patch)}")
        o.update_settings(patch, model_id=args.model)
        print("-- reloading engine so the load-time variant rebuilds")
        o.reload_model(args.model)
        after = {k: o.settings(args.model).get(k) for k in patch}
        print(f"-- active: {json.dumps(after)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
