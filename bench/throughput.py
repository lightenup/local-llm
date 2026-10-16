#!/usr/bin/env python3
"""Run an oMLX throughput benchmark and save a labelled result.

    python3 bench/throughput.py baseline --lengths 1024 4096 16384 65536

The label becomes results/tp_<label>.json so variants stay comparable.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from omlx_client import MODEL, Omlx  # noqa: E402

# Which engine-level settings a result was measured under. Recorded with every
# run because a throughput number is meaningless without its engine variant.
VARIANT_KEYS = (
    "qwen35_ane_prefill_enabled",
    "qwen35_ane_prefill_sequence_length",
    "qwen35_ane_prefill_fraction",
    "qwen35_ane_prefill_gdn",
    "qwen35_ane_prefill_gdn_fraction",
    "qwen35_ane_prefill_cpu_enabled",
    "qwen35_ane_prefill_cpu_fraction",
    "turboquant_kv_enabled",
    "turboquant_kv_bits",
    "mtp_enabled",
    "vlm_mtp_enabled",
    "vlm_mtp_draft_model",
    "vlm_mtp_draft_block_size",
    "dflash_enabled",
    "dflash_draft_model",
    "specprefill_enabled",
    # qwen4_exp only, and load-bearing for exactly one model: it decides whether
    # the 51.2B n-gram table is resident (111.6 GB) or mmapped off SSD (80.1 GB).
    # Without it a Flash-Next peak_gb figure cannot be interpreted at all.
    "qwen4_ple_ssd_offload",
)


def variant_snapshot(o: Omlx, model_id: str) -> dict:
    s = o.settings(model_id)
    return {k: s.get(k) for k in VARIANT_KEYS}


def summarize(res: dict) -> list[dict]:
    """Flatten the bench payload into comparable per-test rows."""
    rows = []
    for r in res.get("results", []) or []:
        sm = r.get("system_metrics") or {}
        mem = sm.get("memory") or {}
        gpu = sm.get("gpu") or {}
        # Batch tests report the same quantities under different names than
        # single tests (pp_tps/tg_tps/avg_ttft_ms) and omit tpot and
        # total_throughput. Reading only the single-test names silently loses
        # every batch measurement.
        batch = r.get("test_type") == "batch"
        bs = r.get("batch_size")
        decode = r.get("tg_tps") if batch else r.get("gen_tps")
        requested = r.get("tg")
        produced = r.get("total_gen_tokens") or r.get("completion_tokens")
        rows.append(
            {
                "test": r.get("test_type"),
                "pp": r.get("pp"),
                "tg": requested,
                "batch_size": bs,
                "ttft_ms": r.get("avg_ttft_ms") if batch else r.get("ttft_ms"),
                "tpot_ms": r.get("tpot_ms"),
                "prefill_tps": r.get("pp_tps") if batch else r.get("processing_tps"),
                "decode_tps": decode,
                # Batch decode_tps is the aggregate across streams. Per-stream is
                # what compares against a single-stream run.
                "decode_tps_per_stream": (
                    round(decode / bs, 2) if batch and decode and bs else decode
                ),
                "total_tps": r.get("total_throughput"),
                "e2e_s": r.get("e2e_latency_s"),
                "gen_tokens": produced,
                "gen_short": _gen_short(requested, produced),
                "peak_gb": round((r.get("peak_memory_bytes") or 0) / 2**30, 2),
                "mlx_active_peak_gb": mem.get("mlx_active_peak"),
                "gpu_util_avg": gpu.get("util_avg"),
            }
        )
    return rows


def _gen_short(requested, produced) -> bool:
    """Did the model return too few tokens for decode_tps to mean anything?

    Qwen3.8-Flash-Next at pp=16384 returned 1, 2, 3, 47, 48, 50 or 128 tokens
    against a requested 128 (results/tp_fnw_p*.json, 2026-09-16). The short runs
    still reported a decode_tps -- 18.8 to 21.3 -- because a rate over two tokens
    is really just 1/TPOT with the first-token settling folded in. Read as a
    measurement it looks like decode collapsing at long context, which is what it
    was mistaken for. The runs that actually generated read 36.3 to 52.5.

    So the threshold is an absolute floor, not a fraction of the request: 47 of
    128 is only 37% but tracks the full-length runs closely, while anything in
    the single digits is noise. max(16, tg/8) keeps the former and rejects the
    latter. Every other model on this machine returns the full 128, so this
    flags nothing outside the pathological case it was written for.
    """
    if not requested or produced is None:
        return False
    return produced < max(16, requested / 8)


def print_table(rows: list[dict]) -> None:
    hdr = (
        f"  {'test':<10} {'pp':>7} {'bs':>3} {'ttft ms':>9} {'prefill t/s':>12} "
        f"{'decode t/s':>11} {'per stream':>11} {'gen tok':>8} {'peak GB':>8} {'gpu%':>5}"
    )
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for r in rows:
        # The marker goes on decode_tps specifically: prefill_tps and ttft_ms are
        # unaffected by a short generation and stay trustworthy on these rows.
        mark = " !" if r.get("gen_short") else ""
        print(
            f"  {str(r['test']):<10} {str(r['pp']):>7} {str(r['batch_size'] or '-'):>3} "
            f"{str(r['ttft_ms']):>9} {str(r['prefill_tps']):>12} "
            f"{str(r['decode_tps']) + mark:>11} {str(r['decode_tps_per_stream']):>11} "
            f"{str(r['gen_tokens']):>8} {str(r['peak_gb']):>8} {str(r['gpu_util_avg']):>5}"
        )
    short = [r for r in rows if r.get("gen_short")]
    if short:
        print()
        print("  !! SHORT GENERATION -- the marked decode t/s are NOT measurements.")
        for r in short:
            print(
                f"     pp={r['pp']} asked for {r['tg']} tokens, got {r['gen_tokens']}; "
                f"decode_tps={r['decode_tps']} is 1/TPOT on that sample."
            )
        print("     prefill t/s and ttft ms on those rows are still valid.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("label")
    ap.add_argument(
        "--lengths", type=int, nargs="+", default=[1024, 4096, 16384, 65536]
    )
    ap.add_argument("--gen", type=int, default=128)
    ap.add_argument("--batch", type=int, nargs="*", default=[])
    ap.add_argument("--corpus", default="code_python")
    ap.add_argument("--warmup", default="quick", choices=["quick", "ane_2048"])
    ap.add_argument("--align-ane", action="store_true")
    ap.add_argument("--model", default=MODEL)
    args = ap.parse_args()

    o = Omlx()
    variant = variant_snapshot(o, args.model)
    print(f"== throughput '{args.label}' on {args.model}")
    print(f"   lengths={args.lengths} gen={args.gen} batch={args.batch} "
          f"corpus={args.corpus} warmup={args.warmup} align_ane={args.align_ane}")
    print(f"   variant={json.dumps({k: v for k, v in variant.items() if v})}")

    res = o.bench_throughput(
        prompt_lengths=args.lengths,
        generation_length=args.gen,
        batch_sizes=args.batch,
        context_profile=args.corpus,
        warmup_mode=args.warmup,
        align_prompt_to_ane=args.align_ane,
        model_id=args.model,
    )
    if res.get("status") != "completed":
        print(f"!! bench {res.get('status')}: {res.get('error_message')}")
        return 1

    payload = {
        "label": args.label,
        "model_id": args.model,
        "request": {
            "prompt_lengths": args.lengths,
            "generation_length": args.gen,
            "batch_sizes": args.batch,
            "context_profile": args.corpus,
            "warmup_mode": args.warmup,
            "align_prompt_to_ane": args.align_ane,
        },
        "variant": variant,
        "summary": summarize(res),
        "raw": res,
    }
    Omlx.save(f"tp_{args.label}", payload)
    print()
    print_table(payload["summary"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
