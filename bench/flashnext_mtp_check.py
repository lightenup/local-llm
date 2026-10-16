#!/usr/bin/env python3
"""Is MTP doing anything on this checkpoint, and does a presence penalty kill it?

FINDINGS §7 established a hard rule for the 27B: MTP speculation is exclusive
with logits processors, so no profile may set `presence_penalty` != 0.0 — the
drafter proposes tokens the penalised distribution then rejects, acceptance
collapses, and decode falls back to one token per forward pass. That rule is why
`profiles/flashnext.py` refuses the vendor's own non-thinking recipe, which asks
for `presence_penalty: 1.5`.

Then bench/flashnext_thinking_ab.py measured the penalty costing 3% of decode at
n=20 per arm, against a per-arm stdev of 7-8 tok/s. That is no cost at all. One
of two things must therefore be true, and they have opposite consequences:

  a. MTP is not actually engaged on this checkpoint, so there is nothing for a
     logits processor to break. Then `mtp_compatible: True` is misleading, the
     profiles' MTP-preserving constraints protect nothing, and the vendor's
     sampling can be adopted as written.

  b. MTP is engaged, and `qwen4_exp`'s in-checkpoint Lightning head is simply not
     subject to §7's exclusivity the way a separate drafter is. Then §7's rule
     needs narrowing to drafter-based speculation, and the vendor's sampling is
     still safe to adopt.

Either way the profiles change; the point of this script is to say which.

`mtp_enabled` is a load-time variant field (omlx/engine_pool.py::
_variant_signature), so each arm pays a full engine reload. Four arms, two
settings crossed with two penalties, so the interaction is visible rather than
inferred from two separate comparisons.

    python3 bench/flashnext_mtp_check.py
    python3 bench/flashnext_mtp_check.py --repeats 5 --max-tokens 400
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from omlx_client import Omlx, OmlxError  # noqa: E402

FN = "Qwen3.8-Flash-Next-oQ4e-mtp"

# Long enough that decode dominates and a per-token speculation win shows up,
# short enough that four arms times N repeats stays inside a coffee break.
PROMPT = (
    "List the integers from 1 to 120, comma separated, on one line, "
    "then stop. No prose."
)


def pool_gb(o: Omlx) -> float:
    return round(o.health()["engine_pool"]["current_model_memory"] / 1e9, 2)


def make_room(o: Omlx, keep: str) -> list[str]:
    """Unload the lineup and wait for the pool to actually drain.

    Both halves matter: the guard refuses rather than evicting, and the release
    is not synchronous — a load attempted straight after an unload was declined
    against a dynamic ceiling while the pool still reported the old figure.
    """
    victims = [m["id"] for m in o.models() if m.get("loaded") and m["id"] != keep]
    for v in victims:
        try:
            o.unload(v)
        except OmlxError as e:
            print(f"  could not unload {v}: {str(e)[:120]}")
    for _ in range(90):
        if pool_gb(o) < 5.0:
            break
        time.sleep(2)
    return victims


def load_with_retry(o: Omlx, model: str, attempts: int = 4) -> None:
    """Reclaimable host RAM moves with whatever else is running; a refusal is
    not necessarily final."""
    for i in range(1, attempts + 1):
        try:
            o.load(model)
            return
        except OmlxError as e:
            print(f"  load attempt {i}: {str(e)[:160]}")
            if i == attempts:
                raise
            time.sleep(20)


def measure(o: Omlx, model: str, penalty: float, repeats: int, max_tokens: int) -> dict:
    rates: list[float] = []
    toks: list[int] = []
    for _ in range(repeats):
        t = o.timed_chat(
            [{"role": "user", "content": PROMPT}],
            model=model,
            max_tokens=max_tokens,
            temperature=0.7,
            top_p=0.80,
            top_k=20,
            presence_penalty=penalty,
            enable_thinking=False,
            chat_template_kwargs={"enable_thinking": False},
        )
        rates.append(t["tok_per_s_overall"])
        toks.append(t["completion_tokens"])
    return {
        "presence_penalty": penalty,
        "n": len(rates),
        "tok_per_s_median": round(statistics.median(rates), 2),
        "tok_per_s_min": min(rates),
        "tok_per_s_max": max(rates),
        "tok_per_s_stdev": round(statistics.stdev(rates), 2) if len(rates) > 1 else 0.0,
        "completion_tokens_median": int(statistics.median(toks)),
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--model", default=FN)
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--max-tokens", type=int, default=400)
    ap.add_argument("--label", default="fn_mtp_check")
    args = ap.parse_args()

    o = Omlx()
    try:
        o.model(args.model)
    except OmlxError:
        print(f"!! {args.model} is not on disk")
        return 2

    baseline = [m["id"] for m in o.models() if m.get("loaded")]
    baseline_mtp = o.settings(args.model).get("mtp_enabled")
    rec: dict[str, Any] = {
        "label": args.label,
        "model_id": args.model,
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "question": "does mtp_enabled change decode, and does presence_penalty 1.5 "
        "interact with it (FINDINGS §7)?",
        "baseline_loaded": baseline,
        "request": {"repeats": args.repeats, "max_tokens": args.max_tokens},
        "arms": [],
    }
    rc = 0
    try:
        make_room(o, args.model)
        for mtp in (True, False):
            try:
                o.unload(args.model)
            except OmlxError:
                pass
            time.sleep(2)
            o.update_settings(
                {"mtp_enabled": mtp, "qwen4_ple_ssd_offload": True},
                model_id=args.model,
            )
            t0 = time.perf_counter()
            load_with_retry(o, args.model)
            load_s = round(time.perf_counter() - t0, 1)
            print(f"\n== mtp_enabled={mtp}  (loaded in {load_s}s)", flush=True)
            for penalty in (0.0, 1.5):
                row = measure(o, args.model, penalty, args.repeats, args.max_tokens)
                row["mtp_enabled"] = mtp
                row["load_s"] = load_s
                rec["arms"].append(row)
                print(
                    f"   presence_penalty {penalty}: "
                    f"{row['tok_per_s_median']} tok/s "
                    f"(n={row['n']}, stdev {row['tok_per_s_stdev']}, "
                    f"{row['completion_tokens_median']} tokens)",
                    flush=True,
                )
    except KeyboardInterrupt:
        rec["interrupted"] = True
        rc = 130
    except (OmlxError, OSError) as e:
        rec["error"] = f"{type(e).__name__}: {e}"
        print(f"\n!! {rec['error']}")
        rc = 1
    finally:
        by = {(a["mtp_enabled"], a["presence_penalty"]): a for a in rec["arms"]}
        hdr = f"  {'mtp':<6} {'penalty':>8} {'tok/s':>8} {'stdev':>7}"
        print("\n" + hdr)
        print("  " + "-" * (len(hdr) - 2))
        for (mtp, pen), a in sorted(by.items(), key=lambda kv: (not kv[0][0], kv[0][1])):
            print(
                f"  {str(mtp):<6} {pen:>8} {a['tok_per_s_median']:>8} "
                f"{a['tok_per_s_stdev']:>7}"
            )

        on0, off0 = by.get((True, 0.0)), by.get((False, 0.0))
        if on0 and off0 and off0["tok_per_s_median"]:
            ratio = on0["tok_per_s_median"] / off0["tok_per_s_median"]
            rec["mtp_speedup"] = round(ratio, 3)
            # The stdevs are large relative to the effect, so the verdict is
            # stated against the noise rather than as a bare ratio.
            noise = max(on0["tok_per_s_stdev"], off0["tok_per_s_stdev"])
            delta = abs(on0["tok_per_s_median"] - off0["tok_per_s_median"])
            rec["verdict"] = (
                f"MTP {'changes' if delta > noise else 'does NOT change'} decode "
                f"({ratio:.2f}x, delta {delta:.2f} tok/s against noise {noise:.2f})"
            )
            print(f"\n  {rec['verdict']}")
        if on0 and by.get((True, 1.5)):
            p = by[(True, 1.5)]["tok_per_s_median"]
            rec["penalty_cost_with_mtp"] = round(1 - p / on0["tok_per_s_median"], 3)
            print(
                f"  with MTP on, presence_penalty 1.5 costs "
                f"{rec['penalty_cost_with_mtp'] * 100:.0f}% of decode"
            )

        print("\n== restoring", flush=True)
        try:
            o.unload(args.model)
        except OmlxError as e:
            print(f"  unload: {str(e)[:120]}")
        if baseline_mtp is not None:
            try:
                o.update_settings({"mtp_enabled": baseline_mtp}, model_id=args.model)
            except OmlxError as e:
                print(f"  restore settings: {str(e)[:120]}")
        for target in baseline:
            try:
                o.load(target)
            except OmlxError as e:
                print(f"  reload {target}: {str(e)[:120]}")
        print(f"  loaded: {[m['id'] for m in o.models() if m.get('loaded')]}")
        rec["exit_code"] = rc
        try:
            Omlx.save(args.label, rec)
        except OSError as e:
            print(f"  !! could not save: {e}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
