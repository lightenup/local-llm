#!/usr/bin/env python3
"""Flash-Next gate: does an 80 GB model fit here, and at what alternation cost?

This is phase 0 of the Flash-Next arm and it can close the whole arm on its own.
Nothing about accuracy matters if the checkpoint cannot be served alongside the
lineup, or if serving it costs an eviction on every call — that is exactly how
Coder-Next lost its job (backlog §A2c, ~20 s per alternation on a routing table
that alternates almost every call).

Three questions, in the order that can kill the arm earliest:

  1. Does oMLX offload the 51.2B n-gram table (the PLE) off SSD, or hold it
     resident? Its own estimator says 111.6 GB resident against 78.0 GB with the
     table mmapped, and `force_ssd_offload` only forces the mmap when the
     resident figure exceeds the admission ceiling. It compares against
     `final_ceiling` (115.45 GB here), which 111.6 fits, so NOTHING IS FORCED —
     the offload has to be asked for. The numbers are read back from oMLX rather
     than recomputed here: /admin/api/models carries
     `qwen4_ple_ssd_offload_supported`, `_forced`, `qwen4_ple_resident_bytes`
     and `qwen4_ple_mmap_bytes`.

     MEASURED 2026-09-13: `final_ceiling` is NOT the ceiling that admits a load.
     The binding one is `metal_cap` at 107.52 GB — the Metal wired limit, with
     `iogpu.wired_limit_mb` at its 0/default on this 128 GB machine — and it is
     exposed nowhere in the API except the refusal message. FINDINGS §H's
     107.52 GB was right; an earlier draft of this file called it stale and was
     wrong. The loader also prices the model itself at 72.65 GB mmapped and
     103.94 GB resident, against the estimator's 78.0 and 111.6.

  2. What does each residency choice cost in load time and decode? mmap is not
     free: the table is a random-access lookup at layer 2 of every forward pass,
     so paging it means SSD reads on the critical path. If the offloaded arm
     decodes materially slower, the 72.65 GB figure is not the real one.

  3. Can it alternate with the shipped 8-bit without eviction? 72.65 + 28.2 =
     100.9 GB inside the 107.52 GB metal_cap leaves ~6.6 GB for the KV of both,
     and the guard reserves KV pessimistically up front. Resident is worse than
     tight: 103.94 GB alone leaves 3.6 GB, so the default configuration cannot
     serve this model beside anything at all. The alternation loop below is
     copied deliberately from bench/oq4e_overnight.sh so the eviction counts are
     comparable to the numbers that retired Coder-Next.

  0. And first, the question that had to be added after the first run: the guard
     does not evict to make room, it REFUSES. With the normal three-model lineup
     resident (53.67 GB) both arms were declined outright. So the cost of serving
     this model is not an eviction on alternation, it is unloading the lineup by
     hand — see `make_room`, whose return value is part of the record.

`qwen4_ple_ssd_offload` and `mtp_enabled` are both load-time variant fields
(omlx/engine_pool.py::_variant_signature), so each arm here pays a full reload.
That is the reason this is a separate script from the throughput sweep rather
than a flag on it.

    python3 bench/flashnext_residency.py
    python3 bench/flashnext_residency.py --arms mmap --skip-coexist
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from omlx_client import Omlx, OmlxError  # noqa: E402

FLASH = "Jundot/Qwen3.8-Flash-Next-oQ4e-mtp".split("/")[-1]
SHIPPED = "Qwen3.8-27B-8bit"

# The alternation prompt from oq4e_overnight.sh, unchanged so the eviction counts
# sit on the same scale as the numbers that retired Coder-Next.
ALTERNATION_PROMPT = "word " * 6000

# Which models were loaded before this script touched anything. Set once in main
# and read by measure_arm to attribute evictions, and by the restore path.
_baseline_loaded: list[str] = []


def host_mem_gb() -> float:
    """Active + wired pages, in GB. The same instrument oq4e_overnight.sh used."""
    out = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
    page = int(re.search(r"page size of (\d+)", out).group(1))
    active = int(re.search(r"Pages active:\s+(\d+)", out).group(1))
    wired = int(re.search(r"Pages wired down:\s+(\d+)", out).group(1))
    return round((active + wired) * page / 1e9, 2)


def loaded_ids(o: Omlx) -> list[str]:
    return [m["id"] for m in o.models() if m.get("loaded")]


def pool(o: Omlx) -> dict:
    return o.health()["engine_pool"]


def estimate_record(o: Omlx, model_id: str) -> dict:
    """oMLX's own residency verdict for a qwen4_exp checkpoint."""
    m = o.model(model_id)
    ceiling = pool(o)["final_ceiling"]
    resident = m.get("qwen4_ple_resident_bytes") or 0
    mmap = m.get("qwen4_ple_mmap_bytes") or 0
    return {
        "model_id": model_id,
        "engine_type": m.get("engine_type"),
        "mtp_compatible": m.get("mtp_compatible"),
        "mtp_compatibility_reason": (m.get("mtp_compatibility_reason") or "")[:200],
        "dflash_compatible": m.get("dflash_compatible"),
        "model_context_length": m.get("model_context_length"),
        "ple_offload_supported": m.get("qwen4_ple_ssd_offload_supported"),
        "ple_offload_forced": m.get("qwen4_ple_ssd_offload_forced"),
        "ple_resident_gb": round(resident / 1e9, 2),
        "ple_mmap_gb": round(mmap / 1e9, 2),
        "ple_table_gb": round((resident - mmap) / 1e9, 2),
        "admission_ceiling_gb": round(ceiling / 1e9, 2),
        "resident_fits_ceiling": bool(resident and resident <= ceiling),
        "mmap_fits_ceiling": bool(mmap and mmap <= ceiling),
    }


def parse_refusal(detail: str) -> dict:
    """Pull the guard's arithmetic out of its refusal message.

    The binding ceiling is `metal_cap`, and it is NOT in
    health()["engine_pool"] — that reports `final_ceiling` (115.45 GB here),
    which is a different and larger figure. The number that actually decides
    admission is the Metal wired limit, 107.52 GB on this machine with
    `iogpu.wired_limit_mb` at its 0/default, and the only place it is exposed is
    this message. So it gets parsed rather than looked up.
    """
    out: dict[str, float | str] = {}
    for key, pat in (
        ("projected_gb", r"projected memory ([\d.]+)GB"),
        ("ceiling_gb", r"ceiling ([\d.]+)GB"),
        ("current_gb", r"current: ([\d.]+)GB"),
        ("model_gb", r"model: ([\d.]+)GB"),
    ):
        m = re.search(pat, detail)
        if m:
            out[key] = float(m.group(1))
    m = re.search(r"exceed the (\w+) memory ceiling", detail)
    if m:
        out["ceiling_name"] = m.group(1)
    return out


def make_room(o: Omlx, keep: str) -> list[str]:
    """Unload everything except the candidate, and report what that cost.

    MEASURED, NOT ASSUMED: oMLX's admission guard does not evict to make room,
    it refuses. With the normal lineup resident (53.67 GB) a 72.65 GB Flash-Next
    projects to 126.33 GB against a 107.52 GB metal_cap and is declined outright.
    So "can this model be served" is inseparable from "what has to be unloaded
    first", and the answer is the whole list this returns.
    """
    victims = [m for m in loaded_ids(o) if m != keep]
    for v in victims:
        try:
            o.unload(v)
        except OmlxError as e:
            print(f"     could not unload {v}: {str(e)[:120]}")
    if victims:
        print(f"     made room by unloading {victims}", flush=True)
    return victims


def measure_arm(
    o: Omlx,
    model_id: str,
    offload: bool,
    decode_tokens: int,
    room: bool = True,
) -> dict:
    """Load one residency variant cold and price it."""
    label = "mmap" if offload else "resident"
    print(f"\n  -- arm {label}: qwen4_ple_ssd_offload={offload}", flush=True)

    try:
        o.unload(model_id)
    except OmlxError:
        pass
    time.sleep(2)

    row: dict[str, Any] = {"arm": label, "qwen4_ple_ssd_offload": offload}
    if room:
        row["unloaded_to_make_room"] = make_room(o, model_id)
    o.update_settings(
        {"qwen4_ple_ssd_offload": offload, "mtp_enabled": True}, model_id=model_id
    )

    mem_before = host_mem_gb()
    t0 = time.perf_counter()
    try:
        o.load(model_id)
        row["load_s"] = round(time.perf_counter() - t0, 1)
    except OmlxError as e:
        # A refusal here IS the result: the memory guard declining an 80 GB
        # admission is what closes the arm, and its message carries the
        # arithmetic it used.
        row["load_s"] = round(time.perf_counter() - t0, 1)
        row["load_error"] = str(e)[:400]
        row["loaded"] = loaded_ids(o)
        row["refusal"] = parse_refusal(row["load_error"])
        print(f"     load REFUSED after {row['load_s']}s: {row['load_error'][:160]}")
        return row

    info = o.model(model_id)
    row["loaded"] = loaded_ids(o)
    row["actual_size"] = info.get("actual_size_formatted")
    row["host_mem_delta_gb"] = round(host_mem_gb() - mem_before, 2)
    row["pool_model_memory_gb"] = round(pool(o)["current_model_memory"] / 1e9, 2)
    row["evicted_on_load"] = sorted(set(_baseline_loaded) - set(row["loaded"]))
    print(
        f"     loaded in {row['load_s']}s, size={row['actual_size']}, "
        f"pool={row['pool_model_memory_gb']} GB, "
        f"evicted={row['evicted_on_load'] or 'none'}",
        flush=True,
    )

    # Decode rate on a short prompt: this is the number that tells us whether
    # mmapping a random-access table off SSD costs anything on the hot path.
    try:
        t = o.timed_chat(
            [{"role": "user", "content": "Count from 1 to 200, comma separated."}],
            model=model_id,
            max_tokens=decode_tokens,
        )
        row["decode"] = {
            "completion_tokens": t["completion_tokens"],
            "wall_s": t["wall_s"],
            "tok_per_s_overall": t["tok_per_s_overall"],
            "finish_reason": t["finish_reason"],
        }
        print(
            f"     decode {t['tok_per_s_overall']} tok/s "
            f"({t['completion_tokens']} tokens in {t['wall_s']}s)",
            flush=True,
        )
    except OmlxError as e:
        row["decode"] = {"error": str(e)[:300]}
        print(f"     decode failed: {row['decode']['error'][:160]}")
    return row


def measure_coexistence(o: Omlx, model_id: str, rounds: int) -> dict:
    """Alternate Flash-Next and the shipped 8-bit; count evictions."""
    print(f"\n  -- coexistence: {rounds} rounds against {SHIPPED}", flush=True)
    rec: dict[str, Any] = {
        "question": f"does {model_id} coexist with {SHIPPED} without eviction?",
        "rounds": [],
    }
    for target in (SHIPPED, model_id):
        try:
            o.load(target)
        except OmlxError as e:
            rec.setdefault("load_errors", {})[target] = str(e)[:300]
    rec["both_loaded"] = loaded_ids(o)
    rec["mem_after_load_gb"] = host_mem_gb()
    rec["pool_model_memory_gb"] = round(pool(o)["current_model_memory"] / 1e9, 2)

    for i in range(rounds):
        row: dict[str, Any] = {}
        for label, target in (("27B", SHIPPED), ("flashnext", model_id)):
            t = time.perf_counter()
            cell: dict[str, Any] = {}
            try:
                o.chat(
                    [{"role": "user", "content": ALTERNATION_PROMPT}],
                    model=target,
                    max_tokens=1,
                    timeout=1800,
                )
            except OmlxError as e:
                cell["error"] = str(e)[:200]
            cell["s"] = round(time.perf_counter() - t, 1)
            cell["loaded"] = loaded_ids(o)
            cell["mem_gb"] = host_mem_gb()
            row[label] = cell
        rec["rounds"].append(row)
        print(
            f"     round {i + 1}: 27B {row['27B']['s']}s "
            f"loaded={len(row['27B']['loaded'])}  "
            f"flashnext {row['flashnext']['s']}s "
            f"loaded={len(row['flashnext']['loaded'])}",
            flush=True,
        )

    both = {model_id, SHIPPED}
    evicted = sum(
        1
        for r in rec["rounds"]
        for cell in r.values()
        if not both.issubset(set(cell["loaded"]))
    )
    rec["evictions_observed"] = evicted
    rec["calls"] = 2 * rounds
    if evicted == 0:
        rec["verdict"] = "no eviction — Flash-Next coexists with the shipped 8-bit"
    else:
        rec["verdict"] = (
            f"{evicted} evictions across {rec['calls']} calls — replacement only, "
            f"as Coder-Next was"
        )
    print(f"     VERDICT: {rec['verdict']}", flush=True)
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=FLASH)
    ap.add_argument(
        "--arms",
        default="resident,mmap",
        help="comma-separated residency arms to price: resident, mmap",
    )
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--decode-tokens", type=int, default=200)
    ap.add_argument("--skip-coexist", action="store_true")
    ap.add_argument(
        "--no-make-room",
        action="store_true",
        help="do not unload other models first; the guard will then refuse, which "
        "is itself the measurement of how much room this model demands",
    )
    ap.add_argument("--label", default="flashnext_residency")
    args = ap.parse_args()

    o = Omlx()
    try:
        o.model(args.model)
    except OmlxError:
        print(f"!! {args.model} is not on disk yet — let the download finish first")
        return 2

    global _baseline_loaded
    _baseline_loaded = loaded_ids(o)
    baseline_offload = o.settings(args.model).get("qwen4_ple_ssd_offload")
    baseline_mtp = o.settings(args.model).get("mtp_enabled")

    rec: dict[str, Any] = {
        "model": args.model,
        "shipped": SHIPPED,
        "baseline_loaded": _baseline_loaded,
        "baseline_settings": {
            "qwen4_ple_ssd_offload": baseline_offload,
            "mtp_enabled": baseline_mtp,
        },
        "estimate": estimate_record(o, args.model),
        "arms": [],
    }

    est = rec["estimate"]
    print("== oMLX residency estimate")
    for k in (
        "engine_type",
        "mtp_compatible",
        "model_context_length",
        "ple_offload_supported",
        "ple_offload_forced",
        "ple_resident_gb",
        "ple_mmap_gb",
        "ple_table_gb",
        "admission_ceiling_gb",
        "resident_fits_ceiling",
        "mmap_fits_ceiling",
    ):
        print(f"  {k:24s} {est[k]}")
    if est["ple_offload_supported"] and not est["ple_offload_forced"]:
        print(
            "  NOTE: offload is supported but NOT forced at this ceiling — "
            "holding the PLE resident is the default, and it is the wrong one"
        )

    rc = 0
    try:
        for arm in [a.strip() for a in args.arms.split(",") if a.strip()]:
            if arm not in ("resident", "mmap"):
                print(f"!! unknown arm {arm!r}, expected resident or mmap")
                return 2
            rec["arms"].append(
                measure_arm(
                    o,
                    args.model,
                    arm == "mmap",
                    args.decode_tokens,
                    room=not args.no_make_room,
                )
            )

        if not args.skip_coexist:
            ok = [a for a in rec["arms"] if "load_error" not in a]
            if not ok:
                rec["coexistence"] = {
                    "skipped": "no residency arm loaded, nothing to coexist with"
                }
            else:
                # Coexist on the arm that actually leaves room: the mmap arm if it
                # ran, since that is the only one the arithmetic allows.
                arm = next((a for a in ok if a["arm"] == "mmap"), ok[-1])
                o.update_settings(
                    {
                        "qwen4_ple_ssd_offload": arm["qwen4_ple_ssd_offload"],
                        "mtp_enabled": True,
                    },
                    model_id=args.model,
                )
                rec["coexistence_arm"] = arm["arm"]
                rec["coexistence"] = measure_coexistence(o, args.model, args.rounds)
    except KeyboardInterrupt:
        rec["interrupted"] = True
        rc = 130
    except (OmlxError, OSError) as e:
        # A partial record is worth keeping: a refused admission carries the
        # guard's own arithmetic, which is the finding this script exists for.
        rec["error"] = f"{type(e).__name__}: {e}"
        print(f"\n!! {rec['error']}")
        rc = 1
    finally:
        print("\n== restoring", flush=True)
        try:
            o.unload(args.model)
        except OmlxError as e:
            print(f"  unload {args.model}: {e}")
        patch: dict[str, Any] = {}
        if baseline_offload is not None:
            patch["qwen4_ple_ssd_offload"] = baseline_offload
        if baseline_mtp is not None:
            patch["mtp_enabled"] = baseline_mtp
        if patch:
            try:
                o.update_settings(patch, model_id=args.model)
            except OmlxError as e:
                print(f"  restore settings: {e}")
        for target in _baseline_loaded:
            try:
                o.load(target)
            except OmlxError as e:
                print(f"  reload {target}: {str(e)[:160]}")
        print(f"  loaded: {loaded_ids(o)}", flush=True)
        # Saved from the finally block so a crash or a Ctrl-C still leaves the
        # measurement on disk; an 80 GB reload is too expensive to repeat for
        # want of a write.
        rec["exit_code"] = rc
        try:
            Omlx.save(args.label, rec)
        except OSError as e:
            print(f"  !! could not save results: {e}")

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
