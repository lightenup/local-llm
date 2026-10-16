#!/usr/bin/env python3
"""Cheap decode + MTP A/B for a coexisting MLX model (qwen3_5_moe et al).

Phase 0 of the overnight MoE run. Answers, in ~10 min per model:
  - what does this candidate actually decode at, single-stream, on this box?
  - if it carries an MTP head (Novaeon does; Tiel does not), does enabling it
    change decode -- or is it a no-op, as CyberTiel's card warns for 3B-active
    MoEs where decode is compute-bound rather than memory-bound?

Unlike bench/flashnext_mtp_check.py this sets NO Flash-Next-only fields
(no qwen4_ple_ssd_offload). It leaves the pinned lineup resident -- these
candidates coexist -- and restores the model's mtp_enabled setting on exit.

    python3 bench/moe_decode_ab.py --model Tiel-Coder-35B-A3B-MLX-oQ4e --no-mtp
    python3 bench/moe_decode_ab.py --model Qwen3.8-35B-A3B-Distill-oQ8-fp16-mtp
"""
from __future__ import annotations
import argparse, statistics, sys, time
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from omlx_client import Omlx, OmlxError  # noqa: E402

PROMPT = ("List the integers from 1 to 120, comma separated, on one line, "
          "then stop. No prose.")


def measure(o: Omlx, model: str, repeats: int, max_tokens: int) -> dict:
    rates, toks = [], []
    for _ in range(repeats):
        t = o.timed_chat([{"role": "user", "content": PROMPT}], model=model,
                         max_tokens=max_tokens, temperature=0.7, top_p=0.8,
                         top_k=20, enable_thinking=False,
                         chat_template_kwargs={"enable_thinking": False})
        rates.append(t["tok_per_s_overall"]); toks.append(t["completion_tokens"])
    return {"n": len(rates), "tok_per_s_median": round(statistics.median(rates), 2),
            "tok_per_s_stdev": round(statistics.stdev(rates), 2) if len(rates) > 1 else 0.0,
            "completion_tokens_median": int(statistics.median(toks))}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--max-tokens", type=int, default=400)
    ap.add_argument("--no-mtp", action="store_true", help="model has no MTP head; skip the toggle")
    ap.add_argument("--label", default=None)
    args = ap.parse_args()
    label = args.label or f"decode_{args.model.split('/')[-1].replace('.', '_')}"

    o = Omlx()
    try:
        o.model(args.model)
    except OmlxError:
        print(f"!! {args.model} not on disk"); return 2
    baseline_mtp = o.settings(args.model).get("mtp_enabled")
    rec = {"label": label, "model_id": args.model,
           "at": datetime.now().astimezone().isoformat(timespec="seconds"),
           "baseline_mtp": baseline_mtp, "arms": []}
    arms = [None] if args.no_mtp else [True, False]
    rc = 0
    try:
        for mtp in arms:
            if mtp is not None:
                try:
                    o.unload(args.model)
                except OmlxError:
                    pass
                time.sleep(2)
                o.update_settings({"mtp_enabled": mtp}, model_id=args.model)
                t0 = time.perf_counter(); o.load(args.model)
                print(f"== mtp={mtp} loaded {round(time.perf_counter()-t0,1)}s", flush=True)
            row = measure(o, args.model, args.repeats, args.max_tokens)
            row["mtp_enabled"] = mtp
            rec["arms"].append(row)
            print(f"   {row['tok_per_s_median']} tok/s (n={row['n']}, "
                  f"stdev {row['tok_per_s_stdev']}, {row['completion_tokens_median']} tok)", flush=True)
        on = next((a for a in rec["arms"] if a["mtp_enabled"] is True), None)
        off = next((a for a in rec["arms"] if a["mtp_enabled"] is False), None)
        if on and off and off["tok_per_s_median"]:
            ratio = on["tok_per_s_median"] / off["tok_per_s_median"]
            noise = max(on["tok_per_s_stdev"], off["tok_per_s_stdev"])
            delta = abs(on["tok_per_s_median"] - off["tok_per_s_median"])
            rec["mtp_speedup"] = round(ratio, 3)
            rec["verdict"] = (f"MTP {'changes' if delta > noise else 'is a NO-OP on'} decode "
                              f"({ratio:.2f}x, delta {delta:.1f} vs noise {noise:.1f} tok/s)")
            print(f"\n  {rec['verdict']}")
    except (OmlxError, OSError, KeyError) as e:
        rec["error"] = f"{type(e).__name__}: {e}"; print("!!", rec["error"]); rc = 1
    finally:
        try:
            o.unload(args.model)
        except OmlxError:
            pass
        if baseline_mtp is not None:
            try:
                o.update_settings({"mtp_enabled": baseline_mtp}, model_id=args.model)
            except OmlxError:
                pass
        rec["exit_code"] = rc
        Omlx.save(label, rec)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
