#!/usr/bin/env python3
"""A/B the reason profile's thinking budget: throughput against runaway risk.

docs/FINDINGS.md section 7 measured that `thinking_budget_enabled` costs the
entire MTP win on the reason profile (17.6 t/s against 31.9 t/s). That settles
the price but not the decision, because the budget exists to stop a hard prompt
spending unbounded tokens thinking and never reaching an answer. This measures
both sides on the same prompts:

    arm "on"   thinking_budget_enabled=True   (as shipped)
    arm "off"  thinking_budget_enabled=False

Recorded per run: overall t/s, reasoning tokens, answer tokens, finish_reason,
and whether the expected answer appears. The prompts are short, so prefill is a
negligible share of wall time and t/s is comparable to the section 7 numbers.

Caveat on the thinking count: this oMLX build does not return
`completion_tokens_details.reasoning_tokens`, so it is derived by splitting the
exact `completion_tokens` between `reasoning_content` and `content` by character
share. `reasoning_tokens_reported` in the saved JSON records which it was.

Decision rule, fixed before the run: if no "off" run exceeds the 8,192-token
budget and none finishes on `length`, the budget is protecting against nothing
measurable and should come off. If any run truncates, it stays on.

The profile is always restored to the definitions.py value on exit, including on
Ctrl-C or an exception, because leaving the budget off would silently change the
served profile.

    python3 bench/thinking_budget_ab.py
    python3 bench/thinking_budget_ab.py --repeats 2 --only hard_geometry
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "profiles"))

from definitions import PROFILES  # noqa: E402
from omlx_client import MODEL, Omlx, OmlxError  # noqa: E402

PROFILE_NAME = "reason"

# Prompts chosen to demand real reasoning, since a prompt the model answers in
# 200 tokens cannot show a runaway. `expect` is a substring that must appear in
# the answer for the run to count as correct; None means correctness is not
# scored and only the token accounting matters.
PROMPTS: dict[str, dict] = {
    "hard_geometry": {
        "expect": None,
        "text": (
            "A regular tetrahedron has edge length 1. A sphere passes through "
            "all four vertices. A second sphere is tangent to all six edges. "
            "Find the exact ratio of the larger radius to the smaller, and "
            "show the derivation."
        ),
    },
    "combinatorics": {
        "expect": None,
        "text": (
            "In how many ways can 8 distinguishable rooks be placed on an 8x8 "
            "board so that no two share a row or column, and no rook sits on "
            "either main diagonal? Derive the count exactly."
        ),
    },
    "algorithm_design": {
        "expect": None,
        "text": (
            "Design an algorithm that, given a stream of n integers and memory "
            "for only O(sqrt(n)) of them, reports every value occurring more "
            "than sqrt(n) times, with no false negatives. State the invariant, "
            "prove correctness, and give the exact space bound."
        ),
    },
    "trap_arithmetic": {
        # A prompt with a specific checkable answer, to catch an arm that
        # reasons at length and still lands wrong.
        "expect": "360",
        "text": (
            "A train leaves A at 60 km/h. Two hours later a second leaves A at "
            "90 km/h on the same track. How far from A do they meet? Reason it "
            "through carefully and state the distance."
        ),
    },
}

ARMS = {"on": True, "off": False}


def shipped_settings() -> dict:
    for spec in PROFILES:
        if spec["name"] == PROFILE_NAME:
            return spec["settings"]
    raise SystemExit(f"no '{PROFILE_NAME}' profile in definitions.py")


def served_id(o: Omlx, model_id: str) -> str:
    for p in o.list_profiles(model_id):
        if p["name"] == PROFILE_NAME:
            return p.get("model_id") or f"{model_id}:{p.get('api_name')}"
    raise SystemExit(f"profile '{PROFILE_NAME}' is not stored on {model_id}")


def set_budget(o: Omlx, model_id: str, enabled: bool) -> None:
    """Rewrite the profile with only thinking_budget_enabled changed."""
    spec = shipped_settings()
    settings = {**spec, "thinking_budget_enabled": enabled}
    o.update_profile(PROFILE_NAME, model_id=model_id, settings=settings)


def run_one(o: Omlx, model: str, prompt: str, max_tokens: int) -> dict:
    t0 = time.perf_counter()
    resp = o.chat(
        [{"role": "user", "content": prompt}],
        model=model,
        max_tokens=max_tokens,
        timeout=1800.0,
    )
    wall = time.perf_counter() - t0

    choice = resp["choices"][0]
    msg = choice.get("message") or {}
    usage = resp.get("usage") or {}
    details = usage.get("completion_tokens_details") or {}
    content = msg.get("content") or ""
    reasoning = msg.get("reasoning_content") or ""
    completion = usage.get("completion_tokens", 0)

    # This oMLX build does not populate completion_tokens_details.reasoning_tokens
    # even though the template exposes reasoning_content, so the count usually
    # has to be derived. Split the exact completion_tokens by character share
    # rather than dividing by a fixed chars-per-token constant: the total is then
    # guaranteed right and the only assumption is that thinking and answer text
    # tokenise at a similar rate. Measured chars/token here is ~3.1, so a
    # 4-per-token constant undercounts thinking by roughly 25%, which is the
    # wrong direction for a rule about whether the budget was exceeded.
    reported = details.get("reasoning_tokens")
    if reported is not None:
        reasoning_tokens = reported
    elif reasoning:
        share = len(reasoning) / (len(reasoning) + len(content) or 1)
        reasoning_tokens = round(completion * share)
    else:
        reasoning_tokens = 0

    return {
        "wall_s": round(wall, 2),
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": completion,
        "reasoning_tokens": reasoning_tokens,
        "reasoning_tokens_reported": reported is not None,
        "answer_chars": len(content),
        "tok_per_s": round(completion / wall, 2) if wall else 0.0,
        "finish_reason": choice.get("finish_reason"),
        "answer_tail": content.strip()[-160:],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--only", choices=list(PROMPTS), help="run a single prompt")
    ap.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        help="default: the reason profile's own max_tokens",
    )
    args = ap.parse_args()

    spec = shipped_settings()
    budget = spec.get("thinking_budget_tokens", 8192)
    max_tokens = args.max_tokens or spec.get("max_tokens", 32768)
    names = [args.only] if args.only else list(PROMPTS)

    o = Omlx()
    model = served_id(o, args.model)

    print(f"== thinking budget A/B on {model}")
    print(f"   prompts={names} repeats={args.repeats} max_tokens={max_tokens}")
    print(f"   shipped budget={budget} tokens\n")

    runs: list[dict] = []
    try:
        for arm, enabled in ARMS.items():
            set_budget(o, args.model, enabled)
            print(f"-- arm '{arm}' (thinking_budget_enabled={enabled})")
            for name in names:
                for i in range(args.repeats):
                    try:
                        r = run_one(o, model, PROMPTS[name]["text"], max_tokens)
                    except (OmlxError, OSError) as e:
                        print(f"   {name:<18} rep{i} FAILED {type(e).__name__}: {e}")
                        runs.append(
                            {"arm": arm, "prompt": name, "rep": i, "error": str(e)[:300]}
                        )
                        continue
                    expect = PROMPTS[name]["expect"]
                    r["correct"] = (
                        None if expect is None else expect in r["answer_tail"]
                    )
                    r.update(arm=arm, prompt=name, rep=i)
                    runs.append(r)
                    flag = "" if r["finish_reason"] == "stop" else "  <- TRUNCATED"
                    over = "" if r["reasoning_tokens"] <= budget else "  <- OVER BUDGET"
                    print(
                        f"   {name:<18} rep{i} {r['tok_per_s']:>6} t/s  "
                        f"think={r['reasoning_tokens']:>6}  "
                        f"total={r['completion_tokens']:>6}  "
                        f"finish={r['finish_reason']}{flag}{over}"
                    )
            print()
    finally:
        shipped = spec.get("thinking_budget_enabled", True)
        set_budget(o, args.model, shipped)
        print(f"-- restored reason profile: thinking_budget_enabled={shipped}")

    ok = [r for r in runs if "error" not in r]
    by_arm = {
        arm: [r for r in ok if r["arm"] == arm] for arm in ARMS
    }
    summary = {}
    for arm, rs in by_arm.items():
        if not rs:
            continue
        summary[arm] = {
            "runs": len(rs),
            "median_tok_per_s": round(statistics.median(r["tok_per_s"] for r in rs), 2),
            "median_reasoning_tokens": int(
                statistics.median(r["reasoning_tokens"] for r in rs)
            ),
            "max_reasoning_tokens": max(r["reasoning_tokens"] for r in rs),
            "truncated": sum(1 for r in rs if r["finish_reason"] != "stop"),
            "over_budget": sum(1 for r in rs if r["reasoning_tokens"] > budget),
            "wrong": sum(1 for r in rs if r["correct"] is False),
        }

    off = summary.get("off", {})
    verdict = (
        "insufficient data"
        if not off
        else (
            "budget can come off: no run exceeded the budget or truncated"
            if off["truncated"] == 0 and off["over_budget"] == 0
            else "keep the budget: at least one unbudgeted run truncated or overran"
        )
    )

    payload = {
        "profile": PROFILE_NAME,
        "served_model_id": model,
        "budget_tokens": budget,
        "max_tokens": max_tokens,
        "summary": summary,
        "verdict": verdict,
        "runs": runs,
    }
    Omlx.save("thinking_budget_ab", payload)

    print(f"\n  {'arm':<5} {'t/s':>7} {'think med':>10} {'think max':>10} "
          f"{'trunc':>6} {'over':>5} {'wrong':>6}")
    print("  " + "-" * 56)
    for arm, s in summary.items():
        print(
            f"  {arm:<5} {s['median_tok_per_s']:>7} {s['median_reasoning_tokens']:>10} "
            f"{s['max_reasoning_tokens']:>10} {s['truncated']:>6} "
            f"{s['over_budget']:>5} {s['wrong']:>6}"
        )
    print(f"\n  verdict: {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
