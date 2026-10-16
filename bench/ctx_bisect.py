#!/usr/bin/env python3
"""Bisect the real context ceiling between a known-good and a known-refused length.

FINDINGS §32 verified gemma-4-31B at 65,215 prompt tokens and saw hard refusals
at 131k and 262k. The true limit is somewhere between, and it was never found
because the sweep only tested powers of two. It matters: the `build` routing
decision turns on whether whole-repo contexts are plausible, and 70k and 96k
give different answers.

The probe is the PREFILL GUARD, which is deterministic -- it either admits a
prompt or returns a 400 with its arithmetic -- so a binary search converges in
log2(range/tolerance) probes rather than needing repeats. That is the whole
reason this is cheap.

ADMITTING IS NOT ATTENDING. §8 exists because a model can accept a length and
retrieve nothing from the middle of it. So the found ceiling is then checked
with needles at three depths before being recommended, and if retrieval fails
the recommendation falls back rather than reporting the admitted length.

The profile matters: the guard sizes KV against `max_context_window`, not the
actual prompt (§32), so probing through a profile that declares 262144 measures
the declaration and not the model. This raises the chosen profile's window to
each probe length, and restores it afterwards.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from omlx_client import Omlx, OmlxError  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def corpus_chars(n: int) -> str:
    """Filler large enough for any probe, built from the repo's own sources."""
    buf: list[str] = []
    total = 0
    while total < n:
        for p in sorted((ROOT / "bench").glob("*.py")):
            t = p.read_text()
            buf.append(t)
            total += len(t)
            if total >= n:
                break
    return "".join(buf)[:n]


def calibrate(o: Omlx, model: str) -> tuple[float, float]:
    """chars = slope*tokens + intercept, fitted from two real probes.

    Not assumed: gemma's tokenizer is ~9% denser on code than the Qwen family
    (§A7), so a constant carried over from another model would misplace every
    probe. Two points are enough for a line and the line is what B2 rebuilt its
    own harness around (§8).
    """
    pts = []
    for chars in (60_000, 180_000):
        r = o.chat([{"role": "user", "content": corpus_chars(chars)}],
                   model=model, max_tokens=1)
        pts.append((chars, (r.get("usage") or {}).get("prompt_tokens", 0)))
        print(f"   calib {chars:>7} chars -> {pts[-1][1]:>7} tokens", flush=True)
    (c1, t1), (c2, t2) = pts
    slope = (c2 - c1) / max(t2 - t1, 1)
    intercept = c1 - slope * t1
    print(f"   fit: chars = {slope:.4f} * tokens + {intercept:.0f}", flush=True)
    return slope, intercept


def admits(o: Omlx, model: str, base_model: str, tokens: int,
           slope: float, intercept: float,
           profile_name: str, base_settings: dict) -> tuple[bool, str, int]:
    """Does the guard admit a prompt of roughly this many tokens?"""
    # The window has to be raised to at least the probe or the guard refuses on
    # the declaration rather than on the model.
    #
    # update_profile REPLACES the entire settings dict (oMLX
    # model_settings.py:1352), it does not merge. Sending only the window here
    # would strip temperature/top_k/force_sampling/etc off the profile -- the
    # exact bug that collapsed g4code to a single field. Always send the full
    # captured settings with only the window overridden, so even a crash
    # mid-probe leaves every sampling field intact.
    o.update_profile(profile_name, model_id=base_model,
                     settings={**base_settings,
                               "max_context_window": min(tokens + 4096, 262144)})
    chars = max(int(slope * tokens + intercept), 1000)
    try:
        r = o.chat([{"role": "user", "content": corpus_chars(chars)}],
                   model=model, max_tokens=8)
        return True, "", (r.get("usage") or {}).get("prompt_tokens", 0)
    except OmlxError as e:
        return False, str(e)[:240], 0
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {str(e)[:200]}", 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help="base model id")
    ap.add_argument("--profile", default="g4-code", help="api_name to probe through")
    ap.add_argument("--profile-name", default="g4code")
    ap.add_argument("--low", type=int, default=65536, help="known admitted")
    ap.add_argument("--high", type=int, default=131072, help="known refused")
    ap.add_argument("--tolerance", type=int, default=4096)
    ap.add_argument("--label", default="ctx_bisect")
    args = ap.parse_args()

    o = Omlx()
    served = f"{args.model}:{args.profile}"
    # Capture the FULL settings dict, not just the window. update_profile
    # replaces settings wholesale, so every raise-and-restore below must carry
    # the complete dict or the profile loses its sampling fields. Capturing only
    # the window here is what stripped g4code down to a single field.
    original_settings: dict = {}
    for p in o.list_profiles(args.model):
        if p.get("name") == args.profile_name:
            original_settings = dict(p.get("settings") or {})
    original = original_settings.get("max_context_window")
    if not original_settings:
        print(f"!! profile {args.profile_name} not found on {args.model}; "
              f"refusing to probe (a raise would create a one-field profile)")
        return 1
    print(f"== bisecting {served} in ({args.low}, {args.high}]")
    print(f"   profile {args.profile_name} window will be restored to {original} "
          f"(full settings: {sorted(original_settings)})")

    out: dict = {"model": args.model, "profile": served, "low": args.low,
                 "high": args.high, "probes": [], "original_window": original}
    try:
        # Room first: a coexistence question would confound a capacity question.
        base = [x["id"] for x in o.models() if x.get("loaded") and x["id"] != args.model]
        for m in base:
            try:
                o.unload(m)
            except Exception:
                pass
        cur = o.health()["engine_pool"]["current_model_memory"]
        for _ in range(90):
            if cur < 5e9:
                break
            time.sleep(2)
            cur = o.health()["engine_pool"]["current_model_memory"]
        o.load(args.model)
        print(f"   {args.model} alone, pool "
              f"{o.health()['engine_pool']['current_model_memory']/2**30:.2f} GiB\n")

        print("== calibrating chars -> tokens")
        slope, intercept = calibrate(o, served)

        lo, hi = args.low, args.high
        best_tokens = 0
        print(f"\n== binary search, tolerance {args.tolerance}")
        while hi - lo > args.tolerance:
            mid = (lo + hi) // 2
            ok, err, got = admits(o, served, args.model, mid, slope, intercept,
                                   args.profile_name, original_settings)
            out["probes"].append({"target": mid, "admitted": ok,
                                  "prompt_tokens": got, "error": err})
            print(f"   {mid:>7} -> {'ADMIT' if ok else 'REFUSE'}"
                  + (f"  ({got} actual tokens)" if ok else f"  {err[:100]}"),
                  flush=True)
            if ok:
                lo = mid
                best_tokens = max(best_tokens, got)
            else:
                hi = mid
        out["max_admitted"] = lo
        out["min_refused"] = hi
        print(f"\n   admitted up to {lo}, refused at {hi}")

        # Admitting is not attending.
        #
        # Two mistakes were made here on the first run and both mattered. The
        # window was set to lo+4096, which is the length already KNOWN to be
        # refused, and the prompt was sized at the boundary and then made
        # LONGER by inserting the needle -- so all three retrievals were
        # refused by the guard and reported as 0/3 retrieval failure. A
        # measurement taken at the exact edge of a cliff measures the cliff.
        # Window is now set to the admitted length, and the prompt is sized
        # with 8k of headroom under the largest prompt actually admitted.
        print(f"\n== retrieval check below the found ceiling ({lo})")
        o.update_profile(args.profile_name, model_id=args.model,
                         settings={**original_settings,
                                   "max_context_window": lo})
        # Also drop and reload so the probe's KV is not still resident; the
        # guard counts it as `current` and refuses on memory that is about to
        # be freed anyway.
        try:
            o.unload(args.model)
            time.sleep(3)
            o.load(args.model)
        except Exception as e:  # noqa: BLE001
            print(f"   reload before retrieval: {str(e)[:100]}")
        import random
        rng = random.Random(20260917)
        target = max(best_tokens - 8192, args.low)
        print(f"   probing at ~{target} tokens "
              f"(largest admitted was {best_tokens})")
        chars = max(int(slope * target + intercept), 1000)
        filler = corpus_chars(chars)
        correct = asked = 0
        for depth in (0.05, 0.5, 0.95):
            secret = rng.randint(10_000, 99_999)
            at = int(len(filler) * depth)
            doc = (filler[:at] + f"\n\nThe access code is {secret}.\n\n" + filler[at:])
            prompt = (doc + "\n\nWhat is the access code? Reply with the number "
                             "and nothing else.")
            asked += 1
            try:
                r = o.chat([{"role": "user", "content": prompt}],
                           model=served, max_tokens=16)
                got = (r["choices"][0]["message"].get("content") or "").strip()
                hit = str(secret) in got
                correct += hit
                print(f"   depth {depth:<5} {'PASS' if hit else 'FAIL'} "
                      f"expected {secret} got {got[:30]!r}", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"   depth {depth:<5} ERROR {str(e)[:110]}", flush=True)
        out["retrieval"] = {"tokens": target, "asked": asked, "correct": correct}

        # Recommend only what was both admitted AND retrieved from.
        if asked and correct == asked:
            out["recommended_cap"] = target
            out["recommendation"] = f"raise the cap to {target} (verified by retrieval)"
        else:
            out["recommended_cap"] = args.low
            out["recommendation"] = (
                f"keep {args.low}: ~{target} is admitted but retrieval was "
                f"{correct}/{asked}, and admitting is not attending")
        print(f"\n   {out['recommendation']}")
    finally:
        if original_settings:
            try:
                # Restore the WHOLE captured dict, not just the window. A
                # window-only restore is what corrupted g4code.
                o.update_profile(args.profile_name, model_id=args.model,
                                 settings=original_settings)
                print(f"   restored {args.profile_name} to full settings "
                      f"(window {original}, {len(original_settings)} fields)")
            except Exception as e:  # noqa: BLE001
                print(f"   !! could not restore settings: {str(e)[:120]}")
        (ROOT / f"results/{args.label}.json").write_text(json.dumps(out, indent=2))
        print(f"   saved -> results/{args.label}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
