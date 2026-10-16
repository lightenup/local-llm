#!/usr/bin/env python3
"""Measure whether the 262k context on `qwen-long` is usable, not just admitted.

The evidence behind the long profile's 262,144-token window is thin: a context
probe accepted a 262k prompt (results/ctx_mtp.json, FINDINGS section 5) and one
needle was retrieved at 46k (smoke.py::check_long). Admitting tokens is not the
same as attending to them. This script sweeps context length against needle
depth and reports retrieval accuracy plus one multi-hop question per length that
requires combining two facts stored far apart.

A failure here means the profile is advertising context it cannot reason over:
either a depth band goes dark (the classic "lost in the middle" shape, retrieval
holds at the ends and collapses in the centre) or single-needle retrieval holds
while the multi-hop question fails, which means the window is a search index and
not a working memory. Either result is a reason to lower `max_context_window`
for `qwen-long` or to document a trusted depth range for it.

Cost control: a cold 262k prefill is 18.7 minutes at 234 tok/s (measured, not
extrapolated), so paying it per question is unaffordable. One haystack is built
per length, the first request pays the prefill, and every later question reuses
the exact same leading messages so oMLX's prefix cache serves them -- 15-19 s
against that 18.7 minutes, at 99.3% reuse. Every request records
`usage.prompt_tokens_details.cached_tokens`, so the saved result proves the
reuse happened. Low cached_tokens on the repeat requests is itself a finding and
is printed loudly.

Sizing the haystack: the first run of this script assumed a flat 3.1 characters
per token and reached only 136,196 tokens when aiming at 262,144, so the 262k
window was never actually tested (FINDINGS section 8). The assumption was wrong
in shape, not just in value. Fitting the three lengths that run recorded gives

    tokens = 0.1653855 * chars + 1796        (exact to the token, all three)

so there is a fixed ~1,796-token overhead — 240 section headers, the instruction
text and the chat template — sitting on top of a constant per-character rate.
A single ratio therefore cannot hold across lengths: the same build measures
5.74 chars/token at 65k and 5.97 at 262k purely because that constant amortises.

So the ratio is no longer assumed. calibrate() sends two small probes of the
real haystack construction, reads `prompt_tokens` back, and fits the line. That
costs about 30 s once per run and, unlike any hardcoded constant, stays correct
when the model underneath changes — which matters because A1 in docs/backlog.md
puts a different tokenizer behind this same harness.

    python3 bench/longctx_quality.py
    python3 bench/longctx_quality.py --lengths 65536 131072
    python3 bench/longctx_quality.py --only-length 262144 --depths 0.5
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from omlx_client import Omlx, OmlxError  # noqa: E402

# Seed estimate only, used to size the two calibration probes. The real
# chars->tokens relationship is measured per model at run time by calibrate();
# see the module docstring for why a single ratio cannot be right.
SEED_CHARS_PER_TOKEN = 6.0

# Tokens held back from the target so the answer has somewhere to go. The
# profile's max_context_window has to cover prompt *and* completion, so aiming a
# prompt at exactly 262,144 overflows the window the moment the model replies.
SAFETY_TOKENS = 256

# Calibration probe sizes, in characters. Small enough to prefill in seconds,
# far enough apart to separate slope from intercept, and both above the
# ~54,000-character floor that 240 sections of one FILLER repeat imposes.
CALIB_CHARS = (60_000, 180_000)

# Engine variant fields. A long-context quality number is meaningless without
# knowing which accelerators were live, and MTP/TurboQuant in particular change
# what the KV cache holds.
VARIANT_PREFIXES = (
    "vlm_mtp",
    "mtp_",
    "turboquant",
    "dflash",
    "specprefill",
    "qwen35_ane",
)

FILLER = (
    "The quarterly maintenance log records routine inspection of pumps, "
    "valves, and seals with no anomalies detected. Torque readings stayed "
    "inside the documented tolerance band and no corrective work was raised. "
)

SECTIONS = 240
NEEDLE_TMPL = "The calibration constant for sensor array {key} is {value}."
REQUEST_TIMEOUT = 3600.0
SEED = 20260906


def variant_snapshot(o: Omlx, model_id: str) -> dict:
    """Engine settings for the base model behind a served profile id."""
    base = model_id.split(":", 1)[0]
    try:
        s = o.settings(base)
    except (OmlxError, OSError, Exception):  # noqa: BLE001
        return {}
    return {
        k: v
        for k, v in s.items()
        if any(k.startswith(p) for p in VARIANT_PREFIXES)
    }


def resolve_model(o: Omlx, override: str | None) -> str:
    if override:
        return override
    for p in o.list_profiles():
        if p.get("name") == "longctx":
            model_id = p.get("model_id")
            if not model_id:
                raise OmlxError("longctx profile has no model_id")
            return model_id
    raise OmlxError("no profile named 'longctx' on this model")


def make_needles(depths: list[float], rng: random.Random) -> list[dict]:
    """One distinct fact per depth, unique key and unique value."""
    letters = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    used_keys: set[str] = set()
    used_values: set[int] = set()
    needles = []
    for depth in depths:
        while True:
            key = f"{rng.choice(letters)}{rng.randint(10, 99)}"
            if key not in used_keys:
                used_keys.add(key)
                break
        while True:
            value = rng.randint(10_000, 99_999)
            if value not in used_values:
                used_values.add(value)
                break
        needles.append(
            {
                "depth": depth,
                "key": key,
                "value": value,
                "sentence": NEEDLE_TMPL.format(key=key, value=value),
            }
        )
    return needles


def build_haystack(target_chars: int, needles: list[dict]) -> str:
    """Numbered filler sections with each needle planted at its depth.

    Length is controlled to the character, not to the nearest whole repeat of
    FILLER. Rounding to whole repeats quantises the haystack in steps of
    240 * 208 = 49,920 characters, about 8,256 tokens, which is 3% of a 262k
    target and makes it impossible to aim deliberately near the window ceiling.

    The section headers and the joins between them cost characters too, so the
    repeat count is chosen to undershoot deliberately and the remainder is padded
    on afterwards. Planting a needle also shortens its section slightly, since
    the needle sentence is shorter than the filler it replaces; the same padding
    absorbs that.
    """
    headers = sum(len(f"[section {i}]\n") for i in range(SECTIONS))
    joins = 2 * (SECTIONS - 1)
    body_budget = target_chars - headers - joins
    unit = SECTIONS * len(FILLER)
    reps = max(1, body_budget // unit)

    blocks = [FILLER * reps for _ in range(SECTIONS)]
    for n in needles:
        idx = min(SECTIONS - 1, max(0, round(n["depth"] * (SECTIONS - 1))))
        n["section"] = idx
        # Overwrite one filler sentence rather than appending, so planting a
        # needle does not push the section past its share of the budget.
        blocks[idx] = n["sentence"] + " " + (FILLER * max(1, reps - 1))

    def assemble(bs: list[str]) -> str:
        return "\n\n".join(f"[section {i}]\n{b}" for i, b in enumerate(bs))

    text = assemble(blocks)
    # Top up the final section to hit the budget exactly. The last section is
    # never a needle site: the deepest depth used is 0.95, which lands on
    # section 227 of 240.
    deficit = target_chars - len(text)
    if deficit > 0:
        pad = (FILLER * (deficit // len(FILLER) + 1))[:deficit]
        blocks[-1] += pad
        text = assemble(blocks)
    return text


MIN_HAYSTACK_CHARS = len(build_haystack(1, []))


def build_questions(needles: list[dict]) -> list[dict]:
    """Single-needle retrieval questions, plus one two-needle multi-hop."""
    qs = []
    for n in needles:
        qs.append(
            {
                "kind": "retrieval",
                "depth": n["depth"],
                "expected": str(n["value"]),
                "prompt": (
                    f"What is the calibration constant for sensor array "
                    f"{n['key']}? Answer with the number only."
                ),
            }
        )
    if len(needles) >= 2:
        a, b = needles[0], needles[-1]
        qs.append(
            {
                "kind": "multihop",
                "depth": None,
                "expected": str(a["value"] + b["value"]),
                "prompt": (
                    f"Add the calibration constant for sensor array {a['key']} "
                    f"to the calibration constant for sensor array {b['key']}. "
                    f"Answer with the resulting number only."
                ),
            }
        )
    return qs


def make_prefix(haystack: str) -> list[dict]:
    """The shared leading messages. Question goes after these, never inside."""
    return [
        {
            "role": "user",
            "content": (
                "Read the following maintenance archive. Answer later questions "
                "using only what it contains.\n\n" + haystack
            ),
        },
        {"role": "assistant", "content": "Archive read. Ready for questions."},
    ]


def calibrate(o: Omlx, model: str, depths: list[float]) -> dict:
    """Fit tokens = slope * chars + intercept for this model's tokenizer.

    Two probes rather than one, because the intercept is the whole point: the
    section headers, instruction text and chat template contribute a fixed token
    cost that a single ratio silently folds into the slope, and that error then
    scales with the target. Both probes are built by the same code path the real
    haystacks use, so whatever overhead that construction carries is inside the
    fit rather than unaccounted for.

    Returns the fit plus the raw probes, so a later reader can check it rather
    than trust it. Falls back to the seed ratio if a probe fails.
    """
    rng = random.Random(SEED)
    probes = []
    for chars in CALIB_CHARS:
        needles = make_needles(depths, rng)
        haystack = build_haystack(chars, needles)
        q = build_questions(needles)[0]
        r = ask(o, model, make_prefix(haystack), q["prompt"], 1)
        probes.append({
            "chars": len(haystack),
            "prompt_tokens": r["prompt_tokens"],
            "ok": r["ok"],
            "error": r["error"],
        })
        print(f"   calib {len(haystack):>8} chars -> {r['prompt_tokens']:>7} tokens"
              + (f"  | {r['error']}" if r["error"] else ""))

    good = [p for p in probes if p["ok"] and p["prompt_tokens"]]
    if len(good) < 2 or good[1]["chars"] == good[0]["chars"]:
        print(f"   !! calibration failed, falling back to {SEED_CHARS_PER_TOKEN} "
              "chars/token; the achieved lengths below will be approximate")
        return {"slope": 1.0 / SEED_CHARS_PER_TOKEN, "intercept": 0.0,
                "fitted": False, "probes": probes}

    (c1, t1), (c2, t2) = ((p["chars"], p["prompt_tokens"]) for p in good[:2])
    slope = (t2 - t1) / (c2 - c1)
    if slope <= 0:
        print("   !! calibration slope non-positive, falling back to seed ratio")
        return {"slope": 1.0 / SEED_CHARS_PER_TOKEN, "intercept": 0.0,
                "fitted": False, "probes": probes}
    intercept = t1 - slope * c1
    print(f"   fit: tokens = {slope:.7f} * chars + {intercept:.0f}  "
          f"({1 / slope:.3f} chars/token asymptotic)")
    return {"slope": slope, "intercept": intercept, "fitted": True,
            "probes": probes}


def chars_for(calib: dict, tokens: int) -> int:
    """Character budget that should land on `tokens` prompt tokens."""
    return max(len(FILLER), int((tokens - calib["intercept"]) / calib["slope"]))


def ask(o: Omlx, model: str, prefix: list[dict], prompt: str, max_tokens: int) -> dict:
    """One chat request. Never raises: an unattended run must not die here."""
    messages = prefix + [{"role": "user", "content": prompt}]
    try:
        r = o.timed_chat(
            messages,
            model=model,
            max_tokens=max_tokens,
            timeout=REQUEST_TIMEOUT,
        )
        return {
            "ok": True,
            "text": r["text"],
            "prompt_tokens": r["prompt_tokens"],
            "completion_tokens": r["completion_tokens"],
            "cached_tokens": r["cached_tokens"],
            "wall_s": r["wall_s"],
            "finish_reason": r["finish_reason"],
            "error": None,
        }
    except (OmlxError, OSError, Exception) as e:  # noqa: BLE001
        return {
            "ok": False,
            "text": "",
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cached_tokens": 0,
            "wall_s": 0.0,
            "finish_reason": None,
            "error": f"{type(e).__name__}: {str(e)[:300]}",
        }


def run_length(
    o: Omlx, model: str, target: int, depths: list[float], args, calib: dict
) -> list[dict]:
    rng = random.Random(SEED + target)
    needles = make_needles(depths, rng)
    # Hold back room for the completion: max_context_window has to cover prompt
    # and answer together, so a prompt sized at exactly `target` overflows the
    # window the instant the model replies.
    reserve = args.max_tokens + SAFETY_TOKENS
    prompt_target = max(len(FILLER), target - reserve)
    haystack = build_haystack(chars_for(calib, prompt_target), needles)
    questions = build_questions(needles)

    # Haystack first, question last: the shared prefix is then everything
    # except the trailing user turn, which is what the prefix cache can serve.
    prefix = make_prefix(haystack)

    print(
        f"\n== target {target} tok  ({len(haystack)} chars, {SECTIONS} sections, "
        f"{len(questions)} questions)"
    )
    print(f"   aiming at {prompt_target} prompt tokens "
          f"(reserving {reserve} for the answer)")
    print(f"   depths={depths} budget={args.max_length_seconds}s")

    rows: list[dict] = []
    t_start = time.perf_counter()
    for i, q in enumerate(questions):
        elapsed = time.perf_counter() - t_start
        if i and elapsed > args.max_length_seconds:
            rows.append(
                {
                    "target_tokens": target,
                    "kind": q["kind"],
                    "depth": q["depth"],
                    "expected": q["expected"],
                    "got": "",
                    "correct": False,
                    "skipped": True,
                    "prompt_tokens": 0,
                    "cached_tokens": 0,
                    "wall_s": 0.0,
                    "error": f"skipped: length budget exhausted after {elapsed:.0f}s",
                }
            )
            print(f"   {q['kind']:<9} depth={q['depth']} SKIP (budget)")
            continue

        r = ask(o, model, prefix, q["prompt"], args.max_tokens)
        got = r["text"].strip()
        correct = bool(r["ok"]) and q["expected"] in got.replace(",", "")
        rows.append(
            {
                "target_tokens": target,
                "kind": q["kind"],
                "depth": q["depth"],
                "expected": q["expected"],
                "got": got[:160],
                "correct": correct,
                "skipped": False,
                "prompt_tokens": r["prompt_tokens"],
                "cached_tokens": r["cached_tokens"],
                "wall_s": r["wall_s"],
                "error": r["error"],
            }
        )
        depth_s = "  multi  " if q["depth"] is None else f"d={q['depth']:<5}"
        print(
            f"   {q['kind']:<9} {depth_s} {'PASS' if correct else 'FAIL'} "
            f"prompt={r['prompt_tokens']} cached={r['cached_tokens']} "
            f"{r['wall_s']}s -> {got[:40]!r}"
            + (f" | {r['error']}" if r["error"] else "")
        )
    return rows


def summarize(rows: list[dict]) -> dict:
    def acc(sel: list[dict]) -> dict:
        scored = [r for r in sel if not r["skipped"]]
        hits = sum(1 for r in scored if r["correct"])
        return {
            "asked": len(sel),
            "scored": len(scored),
            "correct": hits,
            "skipped": sum(1 for r in sel if r["skipped"]),
            "accuracy": round(hits / len(scored), 3) if scored else None,
        }

    lengths = sorted({r["target_tokens"] for r in rows})
    retrieval = [r for r in rows if r["kind"] == "retrieval"]
    by_length = {}
    for L in lengths:
        sel = [r for r in retrieval if r["target_tokens"] == L]
        entry = acc(sel)
        seen = [r["prompt_tokens"] for r in rows
                if r["target_tokens"] == L and r["prompt_tokens"]]
        entry["verified_prompt_tokens"] = max(seen) if seen else None
        # First request pays the prefill; only the repeats can show a cache hit.
        repeats = [r for r in rows
                   if r["target_tokens"] == L and not r["skipped"]][1:]
        fracs = [
            r["cached_tokens"] / r["prompt_tokens"]
            for r in repeats
            if r["prompt_tokens"]
        ]
        entry["repeat_requests"] = len(repeats)
        entry["mean_cached_fraction"] = (
            round(sum(fracs) / len(fracs), 3) if fracs else None
        )
        by_length[str(L)] = entry

    depths = sorted({r["depth"] for r in retrieval if r["depth"] is not None})
    by_depth = {
        str(d): acc([r for r in retrieval if r["depth"] == d]) for d in depths
    }
    multihop = {
        str(r["target_tokens"]): {
            "correct": r["correct"],
            "skipped": r["skipped"],
            "expected": r["expected"],
            "got": r["got"],
            "error": r["error"],
        }
        for r in rows
        if r["kind"] == "multihop"
    }
    return {
        "lengths": lengths,
        "depths": depths,
        "by_length": by_length,
        "by_depth": by_depth,
        "multihop": multihop,
        "overall_retrieval": acc(retrieval),
    }


def print_matrix(rows: list[dict], summary: dict) -> None:
    lengths = summary["lengths"]
    depths = summary["depths"]
    cell = {}
    for r in rows:
        if r["kind"] == "retrieval":
            cell[(r["depth"], r["target_tokens"])] = (
                "skip" if r["skipped"] else ("PASS" if r["correct"] else "FAIL")
            )

    hdr = f"  {'depth':>7}" + "".join(f" {L:>9}" for L in lengths)
    print()
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for d in depths:
        line = f"  {d:>7.2f}"
        for L in lengths:
            line += f" {cell.get((d, L), '-'):>9}"
        print(line)

    mh = f"  {'multihop':>7}"
    for L in lengths:
        m = summary["multihop"].get(str(L))
        if m is None:
            mh += f" {'-':>9}"
        else:
            mh += f" {('skip' if m['skipped'] else 'PASS' if m['correct'] else 'FAIL'):>9}"
    print(mh)

    print()
    for L in lengths:
        e = summary["by_length"][str(L)]
        frac = e["mean_cached_fraction"]
        frac_s = "n/a" if frac is None else f"{frac:.0%}"
        print(
            f"  target {L:>7}  verified_prompt_tokens={e['verified_prompt_tokens']} "
            f"retrieval={e['correct']}/{e['scored']} acc={e['accuracy']} "
            f"skipped={e['skipped']} cache_hit={frac_s} on {e['repeat_requests']} repeats"
        )
        if frac is not None and frac < 0.5:
            print(
                f"    !! prefix cache did NOT serve the repeats at {L} "
                f"(mean {frac:.0%}); every question paid its own prefill, so "
                f"the wall times above are not repeat-query costs"
            )
        # The previous run of this script aimed at 262,144 and reached 136,196
        # without saying so, which is how a 262k claim went unverified for a
        # whole validation cycle. Never let that pass silently again.
        got = e["verified_prompt_tokens"]
        if got:
            miss = abs(got - L) / L
            if miss > 0.02:
                print(
                    f"    !! target {L} was NOT reached: {got} prompt tokens, "
                    f"{miss:.1%} short. This length is unverified."
                )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lengths", type=int, nargs="+",
                    default=[65536, 131072, 262144])
    ap.add_argument("--depths", type=float, nargs="+",
                    default=[0.05, 0.25, 0.5, 0.75, 0.95])
    ap.add_argument("--model", help="served model id; default is the longctx profile")
    ap.add_argument("--max-length-seconds", type=float, default=5400.0,
                    help="per-length wall budget before remaining questions are skipped")
    ap.add_argument("--max-tokens", type=int, default=64)
    ap.add_argument("--only-length", type=int, help="run just this target length")
    ap.add_argument("--label", default="longctx_quality",
                    help="results/<label>.json; the previous run is evidence for "
                         "a FINDINGS claim, so overwrite it deliberately")
    args = ap.parse_args()

    o = Omlx()
    try:
        model = resolve_model(o, args.model)
    except (OmlxError, OSError, Exception) as e:  # noqa: BLE001
        print(f"!! cannot resolve model: {type(e).__name__}: {str(e)[:200]}")
        return 1

    # Ascending order so the cheap, useful data lands before the expensive data.
    lengths = sorted({args.only_length} if args.only_length else set(args.lengths))
    depths = sorted(set(args.depths))
    variant = variant_snapshot(o, model)

    print(f"== longctx quality on {model}")
    print(f"   lengths={lengths} depths={depths} max_tokens={args.max_tokens}")
    # Print the switches, not just the knobs. Filtering on truthiness hides
    # every `*_enabled: False` while leaving its tuning parameters on screen, so
    # a disabled accelerator reads as an enabled one -- turboquant_kv_bits=4.0
    # shows while turboquant_kv_enabled=False does not. The saved JSON always
    # carries the full dict; this is a display fix.
    on = sorted(k for k, v in variant.items() if k.endswith("_enabled") and v)
    off = sorted(k for k, v in variant.items() if k.endswith("_enabled") and not v)
    print(f"   variant on:  { on or 'none' }")
    print(f"   variant off: { off or 'none' }")
    drafter = variant.get("vlm_mtp_draft_model") or variant.get("dflash_draft_model")
    if drafter:
        print(f"   drafter: {drafter}")

    print("\n== calibrating chars -> tokens for this model")
    calib = calibrate(o, model, depths)

    rows: list[dict] = []
    for target in lengths:
        try:
            rows += run_length(o, model, target, depths, args, calib)
        except (OmlxError, OSError, Exception) as e:  # noqa: BLE001
            print(f"!! length {target} aborted: {type(e).__name__}: {str(e)[:200]}")
            rows.append(
                {
                    "target_tokens": target,
                    "kind": "length_error",
                    "depth": None,
                    "expected": "",
                    "got": "",
                    "correct": False,
                    "skipped": True,
                    "prompt_tokens": 0,
                    "cached_tokens": 0,
                    "wall_s": 0.0,
                    "error": f"{type(e).__name__}: {str(e)[:300]}",
                }
            )

    summary = summarize(rows)
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model_id": model,
        "variant": variant,
        "request": {
            "lengths": lengths,
            "depths": depths,
            "max_tokens": args.max_tokens,
            "max_length_seconds": args.max_length_seconds,
            "sections": SECTIONS,
            "seed": SEED,
            "safety_tokens": SAFETY_TOKENS,
        },
        "calibration": calib,
        "records": rows,
        "summary": summary,
    }
    Omlx.save(args.label, payload)
    print_matrix(rows, summary)

    ov = summary["overall_retrieval"]
    print(f"\n  overall retrieval {ov['correct']}/{ov['scored']} acc={ov['accuracy']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
