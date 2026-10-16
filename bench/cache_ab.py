#!/usr/bin/env python3
"""Measure the prefix cache, then A/B the in-memory hot cache tier.

WHY THIS EXISTS
---------------
docs/FINDINGS.md section 8 lists the in-memory hot cache as "the largest
remaining win for agentic loops" and then declares it unreachable: "It is not
exposed by the admin API; it needs settings.json plus a server restart, so it
was left alone."

That is wrong for this oMLX build. `hot_cache_max_size` is a field on the admin
GlobalSettingsRequest (omlx/admin/routes.py, Cache settings block) and the
handler applies it through `_apply_cache_settings_runtime`, which updates the
engine pool's scheduler config and unloads loaded models so they pick it up on
the next load. Only `initial_cache_blocks`, host/port and the MCP config path
are annotated as needing a restart. So this is a pure API change plus a model
reload, which makes it scriptable and testable.

FINDINGS also asserts that "the prefix cache is what makes repeat queries
usable" without ever measuring it. This script measures that too, because the
hot cache decision depends on it: if repeat prefills are already being served
from the SSD tier at acceptable speed, a 20 GB RAM tier is buying much less than
it appears to.

WHAT IT MEASURES
----------------
The workload is the agentic pattern the win was claimed for: one large stable
prefix (a system prompt plus fixed repo-like context) reused across many short
questions.

  1. cold      the first request, which must prefill the whole prefix
  2. repeats   further questions behind the same prefix, which should hit cache
  3. churn     N large unrelated prompts, to push the prefix out of RAM
  4. post      the original question again, after the churn

Step 4 is the one that separates the arms. Without a RAM tier the prefix has to
come back from SSD; with one it should still be resident. A hot cache that only
helps in step 2 is not worth 20 GB, because step 2 is already cheap.

Every request is preceded by POST /admin/api/cache/probe, which classifies the
prompt's blocks as hot-SSD, disk-SSD or cold. That is direct evidence of where
the blocks live, rather than an inference from timing.

FAIRNESS
--------
The SSD cache persists across arms, so a second arm would find the first arm's
blocks already on disk and its "cold" measurement would be a lie. Each arm
therefore uses a unique nonce in the prefix, making its prefix genuinely
uncached.

The shipped value is restored on exit, including on exception or Ctrl-C.

    python3 bench/cache_ab.py
    python3 bench/cache_ab.py --hot-size 24GB --repeats 12
    python3 bench/cache_ab.py --arms baseline      # measure only, change nothing
"""

from __future__ import annotations

import argparse
import random
import statistics
import string
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from omlx_client import MODEL, Omlx, OmlxError  # noqa: E402

VARIANT_PREFIXES = (
    "vlm_mtp",
    "mtp_",
    "turboquant",
    "dflash",
    "specprefill",
    "qwen35_ane",
)

# Roughly 3.1 characters per token was measured on this checkpoint (see
# bench/thinking_budget_ab.py), so target sizes are converted with that.
CHARS_PER_TOKEN = 3.1

FILLER = (
    "The service module records routine inspection of pumps, valves and seals, "
    "with torque values logged against each fastener and no anomalies detected "
    "during the shift. "
)

QUESTIONS = [
    "Which component is described as having torque values logged?",
    "In one sentence, what kind of document is the context above?",
    "Name one thing the inspection found.",
    "Was any anomaly detected? Answer yes or no.",
    "What is logged against each fastener?",
    "Summarise the context in under ten words.",
    "Is this a maintenance record or a sales contract?",
    "What is inspected besides pumps and valves?",
]


def nonce(n: int = 10) -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=n))


def build_prefix(target_tokens: int, tag: str) -> str:
    """A stable, arm-unique block of context of approximately target_tokens."""
    header = (
        f"MAINTENANCE ARCHIVE {tag}\n"
        f"Document set identifier: {tag}\n"
        "The following sections are the authoritative record.\n\n"
    )
    body_chars = max(0, int(target_tokens * CHARS_PER_TOKEN) - len(header))
    per_section = len(FILLER) * 12
    sections = max(1, body_chars // per_section)
    parts = [
        f"[section {i} of set {tag}]\n{FILLER * 12}" for i in range(sections)
    ]
    return header + "\n\n".join(parts)


def probe(o: Omlx, messages: list[dict], model_id: str) -> dict:
    try:
        r = o.cache_probe(messages, model_id=model_id)
        return {
            "total_blocks": r.get("total_blocks"),
            "hot": r.get("blocks_ssd_hot"),
            "disk": r.get("blocks_ssd_disk"),
            "cold": r.get("blocks_cold"),
            "ssd_hit_tokens": r.get("ssd_hit_tokens"),
        }
    except (OmlxError, OSError) as e:
        return {"error": f"{type(e).__name__}: {str(e)[:160]}"}


def ask(
    o: Omlx, prefix: str, question: str, model_id: str, max_tokens: int
) -> dict:
    """One request behind the shared prefix, with timing and cache accounting."""
    messages = [
        {"role": "user", "content": f"{prefix}\n\n---\n\nQuestion: {question}"}
    ]
    before = probe(o, messages, model_id)
    t0 = time.perf_counter()
    try:
        resp = o.chat(messages, model=model_id, max_tokens=max_tokens, timeout=2400.0)
    except (OmlxError, OSError) as e:
        return {
            "question": question[:48],
            "error": f"{type(e).__name__}: {str(e)[:200]}",
            "probe_before": before,
        }
    wall = time.perf_counter() - t0
    usage = resp.get("usage") or {}
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
    pt = usage.get("prompt_tokens", 0)
    return {
        "question": question[:48],
        "wall_s": round(wall, 2),
        "prompt_tokens": pt,
        "cached_tokens": cached,
        "cached_frac": round(cached / pt, 3) if pt else 0.0,
        "completion_tokens": usage.get("completion_tokens", 0),
        "probe_before": before,
    }


def churn(o: Omlx, model_id: str, rounds: int, target_tokens: int) -> list[dict]:
    """Push unrelated large prompts through to evict the prefix from RAM."""
    out = []
    for i in range(rounds):
        junk = build_prefix(target_tokens, f"CHURN{nonce(6)}")
        r = ask(o, junk, "Reply with the single word: ok.", model_id, 8)
        r["churn_round"] = i
        out.append(r)
    return out


def run_arm(
    o: Omlx,
    label: str,
    hot_size: str,
    args: argparse.Namespace,
) -> dict:
    """Set the hot cache to hot_size, then run the full workload once."""
    print(f"\n{'=' * 68}\n== arm '{label}': hot_cache_max_size={hot_size}\n{'=' * 68}")

    applied = None
    try:
        o.update_global_settings({"hot_cache_max_size": hot_size})
        applied = (o.global_settings().get("cache") or {}).get("hot_cache_max_size")
        print(f"   server reports hot_cache_max_size={applied!r}")
    except (OmlxError, OSError) as e:
        return {"arm": label, "error": f"could not set hot cache: {str(e)[:200]}"}

    # Applying cache settings unloads every model. Load explicitly so the cold
    # prefill below measures prefill and not a 28 GB engine load.
    print("   loading model…")
    t0 = time.perf_counter()
    try:
        o.load(args.model)
    except (OmlxError, OSError) as e:
        return {"arm": label, "error": f"load failed: {str(e)[:200]}"}
    print(f"   loaded in {time.perf_counter() - t0:.0f}s")

    tag = nonce()
    prefix = build_prefix(args.prefix_tokens, tag)
    print(f"   prefix tag={tag} target={args.prefix_tokens} tokens")

    # A tiny unrelated request first, so the very first measured request is not
    # also paying one-off warmup costs inside the freshly loaded engine.
    ask(o, "warmup", "Reply with: ok.", args.model, 4)

    print("\n-- cold (first prefill of this prefix)")
    cold = ask(o, prefix, QUESTIONS[0], args.model, args.max_tokens)
    _show(cold)

    print(f"\n-- repeats ({args.repeats} questions behind the same prefix)")
    repeats = []
    for i in range(args.repeats):
        q = QUESTIONS[(i + 1) % len(QUESTIONS)]
        r = ask(o, prefix, q, args.model, args.max_tokens)
        repeats.append(r)
        _show(r)

    print(f"\n-- churn ({args.churn} unrelated prompts of the same size)")
    churned = churn(o, args.model, args.churn, args.prefix_tokens)
    for c in churned:
        _show(c)

    print("\n-- post-churn (same prefix, same question as cold)")
    post = ask(o, prefix, QUESTIONS[0], args.model, args.max_tokens)
    _show(post)

    ok_repeats = [r for r in repeats if "error" not in r]
    return {
        "arm": label,
        "hot_cache_requested": hot_size,
        "hot_cache_reported": applied,
        "prefix_tag": tag,
        "cold": cold,
        "repeats": repeats,
        "churn": churned,
        "post_churn": post,
        "summary": {
            "cold_s": cold.get("wall_s"),
            "repeat_median_s": round(
                statistics.median([r["wall_s"] for r in ok_repeats]), 2
            )
            if ok_repeats
            else None,
            "repeat_median_cached_frac": round(
                statistics.median([r["cached_frac"] for r in ok_repeats]), 3
            )
            if ok_repeats
            else None,
            "post_churn_s": post.get("wall_s"),
            "post_churn_cached_frac": post.get("cached_frac"),
        },
    }


def _show(r: dict) -> None:
    if "error" in r:
        print(f"     FAILED {r['error']}")
        return
    p = r.get("probe_before") or {}
    loc = (
        f"hot={p.get('hot')} disk={p.get('disk')} cold={p.get('cold')}"
        if "error" not in p
        else "probe unavailable"
    )
    print(
        f"     {r['wall_s']:>7.2f}s  prompt={r['prompt_tokens']:>7} "
        f"cached={r['cached_tokens']:>7} ({r['cached_frac']:.0%})  [{loc}]"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--hot-size", default="20GB", help='e.g. "20GB"')
    ap.add_argument("--prefix-tokens", type=int, default=16384)
    ap.add_argument("--repeats", type=int, default=6)
    ap.add_argument("--churn", type=int, default=4)
    ap.add_argument("--max-tokens", type=int, default=48)
    ap.add_argument(
        "--arms",
        default="both",
        choices=["both", "baseline", "hot"],
        help="baseline measures at the shipped value and changes nothing",
    )
    ap.add_argument(
        "--restore",
        default="0",
        help="value to restore on exit; the shipped value is 0 (disabled)",
    )
    args = ap.parse_args()

    o = Omlx()
    variant = {
        k: v
        for k, v in o.settings(args.model).items()
        if k.startswith(VARIANT_PREFIXES) and v not in (None, False)
    }
    original = (o.global_settings().get("cache") or {}).get("hot_cache_max_size")

    print("== hot cache A/B")
    print(f"   model={args.model}")
    print(f"   engine variant={variant}")
    print(f"   hot_cache_max_size currently {original!r}")
    print(
        f"   prefix={args.prefix_tokens} tokens repeats={args.repeats} "
        f"churn={args.churn}"
    )

    plan = {
        "both": [("baseline", "0"), ("hot", args.hot_size)],
        "baseline": [("baseline", original or "0")],
        "hot": [("hot", args.hot_size)],
    }[args.arms]

    arms: list[dict] = []
    try:
        for label, size in plan:
            try:
                arms.append(run_arm(o, label, size, args))
            except Exception as e:  # noqa: BLE001
                print(f"!! arm '{label}' failed: {type(e).__name__}: {e}")
                arms.append({"arm": label, "error": f"{type(e).__name__}: {e}"})
    finally:
        print(f"\n-- restoring hot_cache_max_size={args.restore!r}")
        try:
            o.update_global_settings({"hot_cache_max_size": args.restore})
            now = (o.global_settings().get("cache") or {}).get("hot_cache_max_size")
            print(f"   server reports hot_cache_max_size={now!r}")
            # The restore unloaded the model again; bring it back so the machine
            # is left serving, not cold.
            o.load(args.model)
            print("   model reloaded")
        except (OmlxError, OSError) as e:
            print(f"!! RESTORE FAILED: {e}")
            print("!! check `hot_cache_max_size` in the oMLX admin UI before use")

    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model_id": args.model,
        "engine_variant": variant,
        "hot_cache_before": original,
        "hot_cache_restored_to": args.restore,
        "request": vars(args),
        "arms": arms,
    }
    Omlx.save("cache_ab", payload)

    print(f"\n  {'arm':<10} {'cold s':>8} {'repeat s':>9} {'repeat hit':>11} "
          f"{'post-churn s':>13} {'post hit':>9}")
    print("  " + "-" * 66)
    for a in arms:
        if "error" in a:
            print(f"  {a['arm']:<10} FAILED: {a['error'][:50]}")
            continue
        s = a["summary"]
        print(
            f"  {a['arm']:<10} {str(s['cold_s']):>8} {str(s['repeat_median_s']):>9} "
            f"{str(s['repeat_median_cached_frac']):>11} "
            f"{str(s['post_churn_s']):>13} {str(s['post_churn_cached_frac']):>9}"
        )

    good = [a for a in arms if "error" not in a]
    if len(good) == 2:
        base, hot = good[0]["summary"], good[1]["summary"]
        if base["post_churn_s"] and hot["post_churn_s"]:
            gain = base["post_churn_s"] / hot["post_churn_s"]
            print(
                f"\n  post-churn speedup from the hot cache: {gain:.2f}x "
                f"({base['post_churn_s']}s -> {hot['post_churn_s']}s)"
            )
            print(
                "  This is the number that matters. A gain only in the repeat "
                "column would mean the SSD tier was already sufficient."
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
