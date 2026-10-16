#!/usr/bin/env python3
"""Measure the 4-bit build of this checkpoint on the axes the 8-bit was measured on.

Only 8-bit has ever been measured here, so the quantisation choice is an
assumption, not a result. The hypothesis: `mlx-community/Qwen3.8-27B-4bit`
decodes faster than the shipped 8-bit build and frees roughly 12 GB of resident
RAM, which is exactly the memory the in-memory hot prefix cache wants (FINDINGS
section 8, the largest remaining win for agentic loops), at some accuracy cost.

What would make 4-bit worth switching to: decode beats the shipped 34 t/s at a
16k prompt, peak memory drops by something like 10 GB or more, and the code and
reason suites stay inside the +/-2.2% that 500 samples per suite resolves.

What rules it out: any suite falling several points below the 8-bit numbers in
results/acc_ship_code.json and results/acc_ship_reason.json, or decode failing
to improve. Without the accuracy the only argument for 4-bit is the freed RAM,
and without the RAM there is no argument at all.

    python3 bench/quant_compare.py
    python3 bench/quant_compare.py --skip-download --skip-accuracy
    python3 bench/quant_compare.py --samples 99      # stays fully local

Runs unattended for hours. Every phase records its own outcome into
results/quant_compare.json even when a later phase dies, and the production
8-bit model plus its four profiles are never written to: the only mutation this
script makes is on the 4-bit model, which it unloads again on the way out.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from omlx_client import MODEL, REPO_ROOT, Omlx, OmlxError  # noqa: E402
from set_variant import RESET, VARIANTS  # noqa: E402
from set_variant import apply as apply_variant  # noqa: E402
from throughput import VARIANT_KEYS, print_table, summarize, variant_snapshot  # noqa: E402

HF_ORG = "mlx-community"

# The 8-bit results this run is compared against. Written by bench/overnight.sh.
SHIPPED_TP = "tp_mtp"
SHIPPED_ACC = {"code": "acc_ship_code", "reason": "acc_ship_reason"}

PROMPT_LENGTHS = [1024, 4096, 16384, 65536]
GEN_LENGTH = 256
BATCH_PROMPT = 4096
# oMLX's bench endpoint rejects batch size 1: BenchmarkRequest validates against
# [2, 4, 8]. The single-stream arm is the `single` sweep, which is measured
# separately, so 1 here bought nothing and cost the whole batch phase -- it
# failed validation before running any size at all.
BATCH_SIZES = [2, 4, 8]
ACC_BATCH = 8
UPLOAD_THRESHOLD = 100  # _MIN_UPLOAD_QUESTIONS in omlx/admin/accuracy_upload.py

# Registered into set_variant.VARIANTS at runtime. VARIANTS["mtp"] pins the
# 8-bit drafter through the module-level DRAFT constant, so it cannot be reused
# for a 4-bit target.
VARIANT_NAME = "mtp_4bit"

DOWNLOAD_DONE = ("completed", "complete", "done")
DOWNLOAD_BAD = ("error", "failed", "cancelled")


# ---------------------------------------------------------------- helpers


def stamp(msg: str) -> None:
    print(f"\n\n======== {msg} ========")
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]", flush=True)


def repo_id_for(model_id: str) -> str:
    """oMLX registers a repo under its bare name, so map back to the repo."""
    return model_id if "/" in model_id else f"{HF_ORG}/{model_id}"


def local_id_for(repo_id: str) -> str:
    return repo_id.rsplit("/", 1)[-1]


def model_present(o: Omlx, model_id: str) -> bool:
    try:
        return any(m["id"] == model_id for m in o.models())
    except OmlxError:
        return False


def load_saved(name: str) -> dict | None:
    """Load a previous result, tolerating an absent or half-written file."""
    try:
        return Omlx.load_result(name)
    except (OSError, json.JSONDecodeError) as e:
        print(f"  (no usable results/{name}.json: {e})")
        return None


def single_row(run: dict | None, pp: int) -> dict | None:
    if not run:
        return None
    for r in run.get("summary") or []:
        if r.get("pp") == pp and (r.get("batch_size") in (None, 0, 1)):
            return r
    return None


def suite_scores(run: dict | None) -> dict[str, dict]:
    if not run:
        return {}
    out = {}
    for r in run.get("results") or []:
        name = r.get("benchmark")
        if name and isinstance(r.get("accuracy"), (int, float)):
            out[name] = r
    return out


def cell(value, spec: str = "") -> str:
    if value is None:
        return "-"
    return format(value, spec) if spec else str(value)


def delta(new, old, spec: str = "+.2f") -> str:
    if new is None or old is None:
        return "-"
    return format(new - old, spec)


# ---------------------------------------------------------------- phases


def run_download(o: Omlx, repos: list[str], timeout: float, tasks: list[dict]) -> None:
    """Fetch anything missing, and refuse to continue on a failed task."""
    have = {m["id"] for m in o.models()}
    print(f"  already registered: {sorted(have)}")
    for repo in repos:
        lid = local_id_for(repo)
        if lid in have:
            print(f"  {lid}: present locally, skipping download")
            tasks.append({"repo_id": repo, "status": "already_present"})
            continue
        # No size is printed on purpose: the HF model-info size field reports
        # 101 GB even for the 8-bit repo that occupies ~25 GB on disk.
        print(f"  {repo}: queueing download", flush=True)
        queued = o.hf_download(repo)
        print(f"    queued: {json.dumps(queued)[:400]}", flush=True)
        task = o.wait_for_download(repo, timeout=timeout)
        tasks.append(task)
        status = (task.get("status") or "").lower()
        if status in DOWNLOAD_BAD or status not in DOWNLOAD_DONE:
            raise OmlxError(
                f"download of {repo} ended as {status!r}: {task.get('message')}"
            )
        print(f"  {repo}: {status}")

    still_missing = [
        local_id_for(r) for r in repos if not model_present(o, local_id_for(r))
    ]
    if still_missing:
        raise OmlxError(f"downloaded but not registered by oMLX: {still_missing}")


def run_configure(o: Omlx, model_id: str, draft_id: str) -> tuple[dict, dict]:
    """Give the 4-bit model the shipped engine variant with the 4-bit drafter."""
    VARIANTS[VARIANT_NAME] = {
        "vlm_mtp_enabled": True,
        "vlm_mtp_draft_model": draft_id,
        "vlm_mtp_draft_block_size": 3,
    }
    print(f"  full engine patch: {json.dumps({**RESET, **VARIANTS[VARIANT_NAME]})}")
    # A never-loaded model is listed without a settings block, so oMLX has no
    # defaults to merge a variant patch onto. One load materialises them.
    if not o.settings(model_id):
        print(f"  {model_id} has no settings yet, loading once to materialise them")
        o.load(model_id)
    apply_variant(o, VARIANT_NAME, model_id=model_id, reload=True)

    variant = variant_snapshot(o, model_id)
    m = o.model(model_id)
    size = {
        "actual_size_formatted": m.get("actual_size_formatted"),
        "actual_size_bytes": m.get("actual_size_bytes"),
        "size_formatted": m.get("size_formatted"),
        "loaded": m.get("loaded"),
        "engine_type": m.get("engine_type"),
        "model_context_length": m.get("model_context_length"),
        "mtp_compatible": m.get("mtp_compatible"),
        "mtp_compatibility_reason": (m.get("mtp_compatibility_reason") or "")[:200],
    }
    print(f"  variant: {json.dumps({k: v for k, v in variant.items() if v})}")
    print(f"  reported size: {json.dumps(size)}")
    return variant, size


def sweep(
    o: Omlx,
    label: str,
    model_id: str,
    variant: dict,
    lengths: list[int],
    batch: list[int],
) -> list[dict]:
    """One throughput sweep, saved in bench/throughput.py's payload shape."""
    print(f"  sweep '{label}': lengths={lengths} gen={GEN_LENGTH} batch={batch}")
    res = o.bench_throughput(
        prompt_lengths=lengths,
        generation_length=GEN_LENGTH,
        batch_sizes=batch,
        context_profile="code_python",
        warmup_mode="quick",
        align_prompt_to_ane=False,
        model_id=model_id,
        timeout=14400.0,
    )
    if res.get("status") != "completed":
        raise OmlxError(f"bench {res.get('status')}: {res.get('error_message')}")
    rows = summarize(res)
    Omlx.save(
        f"tp_{label}",
        {
            "label": label,
            "model_id": model_id,
            "request": {
                "prompt_lengths": lengths,
                "generation_length": GEN_LENGTH,
                "batch_sizes": batch,
                "context_profile": "code_python",
                "warmup_mode": "quick",
                "align_prompt_to_ane": False,
            },
            "variant": variant,
            "summary": rows,
            "raw": res,
        },
    )
    print()
    print_table(rows)
    return rows


def run_suite(group: str, label: str, model_id: str, samples: int) -> None:
    """Shell out to bench/accuracy.py so its progress lands in this log."""
    cmd = [
        sys.executable,
        str(Path(__file__).resolve().parent / "accuracy.py"),
        group,
        "--label",
        label,
        "--samples",
        str(samples),
        "--batch",
        str(ACC_BATCH),
        "--model",
        model_id,
    ]
    print(f"  $ {' '.join(cmd)}", flush=True)
    proc = subprocess.run(cmd, cwd=str(REPO_ROOT))
    if proc.returncode != 0:
        raise OmlxError(f"accuracy.py {group} exited {proc.returncode}")


def compare(payload: dict) -> dict:
    """Side by side against the 8-bit numbers, tolerating missing files."""
    eight_tp = load_saved(SHIPPED_TP)
    four_rows = payload["throughput"]["single"] or payload["throughput"]["batch"]
    four_tp = {"summary": four_rows}

    tp_delta = []
    hdr = (
        f"  {'prompt':>7} {'8bit t/s':>9} {'4bit t/s':>9} {'delta':>8} {'x':>6}   "
        f"{'8bit GB':>8} {'4bit GB':>8} {'delta GB':>9}"
    )
    print("\n  == decode and peak memory by prompt length ==")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for pp in PROMPT_LENGTHS:
        a = single_row(eight_tp, pp)
        b = single_row(four_tp, pp)
        a_tps = a.get("decode_tps") if a else None
        b_tps = b.get("decode_tps") if b else None
        a_gb = a.get("peak_gb") if a else None
        b_gb = b.get("peak_gb") if b else None
        ratio = round(b_tps / a_tps, 3) if a_tps and b_tps else None
        row = {
            "pp": pp,
            "decode_8bit": a_tps,
            "decode_4bit": b_tps,
            "decode_delta": round(b_tps - a_tps, 2) if a_tps and b_tps else None,
            "decode_ratio": ratio,
            "peak_gb_8bit": a_gb,
            "peak_gb_4bit": b_gb,
            "peak_gb_delta": round(b_gb - a_gb, 2)
            if a_gb is not None and b_gb is not None
            else None,
        }
        tp_delta.append(row)
        print(
            f"  {pp:>7} {cell(a_tps):>9} {cell(b_tps):>9} "
            f"{delta(b_tps, a_tps):>8} {cell(ratio, '.2f'):>6}   "
            f"{cell(a_gb):>8} {cell(b_gb):>8} {delta(b_gb, a_gb):>9}"
        )

    acc_delta = []
    print("\n  == accuracy by suite (percentage points) ==")
    hdr = f"  {'suite':<16} {'8bit':>8} {'4bit':>8} {'delta':>8} {'n 8bit':>7} {'n 4bit':>7}"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for group, eight_label in SHIPPED_ACC.items():
        eight = suite_scores(load_saved(eight_label))
        four_label = payload["accuracy"].get(group)
        four = suite_scores(load_saved(four_label) if four_label else None)
        for suite in sorted(set(eight) | set(four)):
            a = eight.get(suite, {}).get("accuracy")
            b = four.get(suite, {}).get("accuracy")
            a_pct = round(a * 100, 1) if a is not None else None
            b_pct = round(b * 100, 1) if b is not None else None
            row = {
                "group": group,
                "suite": suite,
                "acc_8bit_pct": a_pct,
                "acc_4bit_pct": b_pct,
                "acc_delta_pct": round(b_pct - a_pct, 1)
                if a_pct is not None and b_pct is not None
                else None,
                "n_8bit": eight.get(suite, {}).get("total"),
                "n_4bit": four.get(suite, {}).get("total"),
            }
            acc_delta.append(row)
            print(
                f"  {suite:<16} {cell(a_pct, '.1f'):>8} {cell(b_pct, '.1f'):>8} "
                f"{delta(b_pct, a_pct, '+.1f'):>8} "
                f"{cell(row['n_8bit']):>7} {cell(row['n_4bit']):>7}"
            )

    return {
        "shipped_throughput": SHIPPED_TP,
        "shipped_accuracy": SHIPPED_ACC,
        "throughput": tp_delta,
        "accuracy": acc_delta,
    }


def cleanup(o: Omlx, model_id: str) -> None:
    """Free the 4-bit weights and prove the production config is untouched."""
    try:
        if model_present(o, model_id):
            o.unload(model_id)
            print(f"  unloaded {model_id}")
        else:
            print(f"  {model_id} is not registered; nothing to unload")
    except OmlxError as e:
        print(f"  !! could not unload {model_id}: {e}")

    try:
        loaded = [m["id"] for m in o.models() if m.get("loaded")]
        print(f"  loaded now: {loaded if loaded else '(none)'}")
        print(f"  production model {MODEL}, never written to by this script:")
        snap = variant_snapshot(o, MODEL)
        for k in VARIANT_KEYS:
            print(f"     {k:<38} {snap.get(k)}")
        print(f"  profiles on {MODEL}: {[p.get('name') for p in o.list_profiles(MODEL)]}")
    except OmlxError as e:
        print(f"  !! could not read back {MODEL}: {e}")


# ---------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Measure the 4-bit sibling against the shipped 8-bit results."
    )
    ap.add_argument("--samples", type=int, default=500)
    ap.add_argument("--skip-download", action="store_true")
    ap.add_argument("--skip-throughput", action="store_true")
    ap.add_argument("--skip-accuracy", action="store_true")
    ap.add_argument("--model-4bit", default="Qwen3.8-27B-4bit")
    ap.add_argument("--draft-4bit", default="Qwen3.8-27B-MTP-4bit")
    ap.add_argument("--download-timeout", type=float, default=14400.0)
    args = ap.parse_args()

    model_id = args.model_4bit
    draft_id = args.draft_4bit
    repos = [repo_id_for(model_id), repo_id_for(draft_id)]

    phases: dict[str, str] = {}
    payload: dict = {
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "shipped_model": MODEL,
        "model_4bit": model_id,
        "draft_4bit": draft_id,
        "request": {
            "samples": args.samples,
            "prompt_lengths": PROMPT_LENGTHS,
            "generation_length": GEN_LENGTH,
            "batch_prompt": BATCH_PROMPT,
            "batch_sizes": BATCH_SIZES,
            "accuracy_batch": ACC_BATCH,
            "repos": repos,
            "skip_download": args.skip_download,
            "skip_throughput": args.skip_throughput,
            "skip_accuracy": args.skip_accuracy,
        },
        "phases": phases,
        "download_tasks": [],
        "variant": None,
        "reported_size": None,
        "throughput": {"single": [], "batch": []},
        "accuracy": {},
        "comparison": {},
    }

    o = Omlx()
    rc = 0
    abort = False

    print(f"== quant compare: {MODEL} (production, 8-bit) vs {model_id}")
    print(f"   drafter {draft_id}, repos {repos}")
    print(f"   accuracy samples={args.samples} per suite, batch={ACC_BATCH}")
    print(
        f"   notice: oMLX uploads any suite of >={UPLOAD_THRESHOLD} questions to "
        f"omlx.ai with no opt-out; approved for this run"
    )
    print("   the 8-bit model and its profiles are read-only here")

    try:
        # -- 1. download -------------------------------------------------
        if args.skip_download:
            stamp("1. download (skipped)")
            phases["download"] = "skipped"
        else:
            stamp("1. download the 4-bit checkpoint and its MTP drafter")
            try:
                run_download(o, repos, args.download_timeout, payload["download_tasks"])
                phases["download"] = "ok"
            except (OmlxError, OSError) as e:
                phases["download"] = f"failed: {e}"
                print(f"!! download failed: {e}")
                print("!! there is nothing to measure; skipping the benchmark phases")
                abort = True
                rc = 1

        # -- 2. configure ------------------------------------------------
        if abort:
            stamp("2. configure (skipped, download failed)")
            phases["configure"] = "skipped: download failed"
        else:
            stamp(f"2. configure {model_id} with the 4-bit MTP drafter")
            try:
                variant, size = run_configure(o, model_id, draft_id)
                payload["variant"] = variant
                payload["reported_size"] = size
                phases["configure"] = "ok"
            except (OmlxError, OSError) as e:
                phases["configure"] = f"failed: {e}"
                print(f"!! configure failed: {e}")
                if not model_present(o, model_id):
                    print(f"!! {model_id} is not registered; skipping the benchmarks")
                    abort = True
                    rc = 1
                else:
                    print("   the model exists, so measure it as it currently stands")

        # -- 3. throughput -----------------------------------------------
        if abort or args.skip_throughput:
            why = "download failed" if abort else "flag"
            stamp(f"3. throughput (skipped, {why})")
            phases["throughput_single"] = f"skipped: {why}"
            phases["throughput_batch"] = f"skipped: {why}"
        else:
            stamp("3. throughput on the 4-bit model")
            variant = payload["variant"] or {}
            try:
                payload["throughput"]["single"] = sweep(
                    o, "quant4", model_id, variant, PROMPT_LENGTHS, []
                )
                phases["throughput_single"] = "ok"
            except (OmlxError, OSError) as e:
                phases["throughput_single"] = f"failed: {e}"
                print(f"!! throughput sweep failed: {e}")
            try:
                payload["throughput"]["batch"] = sweep(
                    o, "quant4_batch", model_id, variant, [BATCH_PROMPT], BATCH_SIZES
                )
                phases["throughput_batch"] = "ok"
            except (OmlxError, OSError) as e:
                phases["throughput_batch"] = f"failed: {e}"
                print(f"!! batch sweep failed: {e}")

        # -- 4. accuracy -------------------------------------------------
        if abort or args.skip_accuracy:
            why = "download failed" if abort else "flag"
            stamp(f"4. accuracy (skipped, {why})")
            for group in SHIPPED_ACC:
                phases[f"accuracy_{group}"] = f"skipped: {why}"
        else:
            stamp(f"4. accuracy: code and reason at {args.samples} samples per suite")
            for group in SHIPPED_ACC:
                label = f"quant4_{group}"
                try:
                    run_suite(group, label, model_id, args.samples)
                    payload["accuracy"][group] = f"acc_{label}"
                    phases[f"accuracy_{group}"] = "ok"
                except (OmlxError, OSError) as e:
                    phases[f"accuracy_{group}"] = f"failed: {e}"
                    print(f"!! accuracy group {group} failed: {e}")

        # -- 5. comparison -----------------------------------------------
        stamp("5. 8-bit versus 4-bit")
        try:
            payload["comparison"] = compare(payload)
            phases["comparison"] = "ok"
        except (OmlxError, OSError, KeyError, TypeError, ValueError) as e:
            phases["comparison"] = f"failed: {e}"
            print(f"!! comparison failed: {e}")

    except KeyboardInterrupt:
        print("\n!! interrupted")
        phases["interrupted"] = "run cancelled by KeyboardInterrupt"
        rc = 130
    finally:
        # -- 6. cleanup --------------------------------------------------
        stamp("6. cleanup and proof the production setup is intact")
        try:
            cleanup(o, model_id)
            phases["cleanup"] = "ok"
        except (OmlxError, OSError) as e:
            phases["cleanup"] = f"failed: {e}"
            print(f"!! cleanup failed: {e}")

        payload["exit_code"] = rc
        payload["finished"] = datetime.now().astimezone().isoformat(timespec="seconds")
        print()
        for name, status in phases.items():
            print(f"  {name:<20} {status}")
        try:
            Omlx.save("quant_compare", payload)
        except OSError as e:
            print(f"!! could not save the combined payload: {e}")

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
