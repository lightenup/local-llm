#!/usr/bin/env python3
"""Compare two accuracy runs question by question instead of score by score.

    python3 bench/paired_compare.py acc_code_temp07 acc_code_temp03

Suite scores are the wrong instrument for judging a small configuration change.
At 500 samples the standard error on a single score is about 2.2 points, so the
95% interval is roughly +/-4.4, and the interval on the *difference* between two
independent scores is about +/-6.2 -- larger still, because the variances add.
Any change worth making is invisible against that. But both arms answered the
*same* questions, so the arms are paired: the only informative quantity is the
set of questions whose verdict flipped. That is McNemar's test, and it resolves
differences an order of magnitude smaller than the independent-sample bound,
because every question both arms got right and every question both got wrong
carries no information about which arm is better and drops out.

Reads only saved result files, so it never touches the GPU.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

RESULTS = Path(__file__).resolve().parent.parent / "results"


def load(label: str) -> dict:
    p = RESULTS / f"{label if label.startswith('acc_') else 'acc_' + label}.json"
    if not p.exists():
        raise SystemExit(f"no such result file: {p}")
    return json.loads(p.read_text())


def run_records(payload: dict) -> dict[str, dict]:
    """This run's own record per suite.

    Result files written before the accuracy.py freshness fix can contain the
    whole accumulated history rather than just this run, with the run's own
    records appended last, so take the trailing window.
    """
    suites = list(payload["request"]["benchmarks"])
    tail = payload["results"][-len(suites):]
    return {r["benchmark"]: r for r in tail}


def two_sided_binomial(k: int, n: int) -> float:
    """Exact two-sided p for k successes in n fair Bernoulli trials.

    This is McNemar's exact test: of the n questions that flipped, k went one
    way. Under the null the two arms are equivalent, so each flip is a coin
    toss. Exact rather than chi-squared because n here is often under 25.
    """
    if n == 0:
        return 1.0
    from math import comb

    total = 2**n
    tail = sum(comb(n, i) for i in range(0, min(k, n - k) + 1))
    return min(1.0, 2 * tail / total)


def compare_suite(a: dict, b: dict) -> dict | None:
    qa = {q["id"]: q for q in (a.get("question_results") or [])}
    qb = {q["id"]: q for q in (b.get("question_results") or [])}
    shared = sorted(set(qa) & set(qb))
    if not shared:
        return None

    both = a_only = b_only = neither = 0
    flips_to_b: list[str] = []
    flips_to_a: list[str] = []
    for qid in shared:
        ca, cb = bool(qa[qid].get("correct")), bool(qb[qid].get("correct"))
        if ca and cb:
            both += 1
        elif ca and not cb:
            a_only += 1
            flips_to_a.append(qid)
        elif cb and not ca:
            b_only += 1
            flips_to_b.append(qid)
        else:
            neither += 1

    discordant = a_only + b_only
    return {
        "n_shared": len(shared),
        "both_correct": both,
        "neither_correct": neither,
        "only_a_correct": a_only,
        "only_b_correct": b_only,
        "discordant": discordant,
        "net_to_b": b_only - a_only,
        "score_a": round(100 * (both + a_only) / len(shared), 2),
        "score_b": round(100 * (both + b_only) / len(shared), 2),
        "p_value": round(two_sided_binomial(b_only, discordant), 5),
        "flips_to_a_sample": flips_to_a[:8],
        "flips_to_b_sample": flips_to_b[:8],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("arm_a", help="baseline result label, e.g. acc_code_temp07")
    ap.add_argument("arm_b", help="candidate result label, e.g. acc_code_temp03")
    ap.add_argument("--save", default="", help="save as results/paired_<name>.json")
    args = ap.parse_args()

    pa, pb = load(args.arm_a), load(args.arm_b)
    ra, rb = run_records(pa), run_records(pb)
    suites = [s for s in ra if s in rb]
    if not suites:
        raise SystemExit(f"no shared suites between {args.arm_a} and {args.arm_b}")

    print(f"== paired comparison: A={args.arm_a}  B={args.arm_b}")
    print(f"   shared suites: {suites}")
    print()
    hdr = (
        f"  {'suite':<16} {'A':>6} {'B':>6} {'A only':>7} {'B only':>7} "
        f"{'net B':>6} {'flips':>6} {'p':>8}"
    )
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    per_suite: dict[str, dict] = {}
    tot_a = tot_b = tot_n = 0
    for s in suites:
        c = compare_suite(ra[s], rb[s])
        if c is None:
            print(f"  {s:<16} (no per-question data)")
            continue
        per_suite[s] = c
        tot_a += c["only_a_correct"]
        tot_b += c["only_b_correct"]
        tot_n += c["n_shared"]
        print(
            f"  {s:<16} {c['score_a']:>6} {c['score_b']:>6} "
            f"{c['only_a_correct']:>7} {c['only_b_correct']:>7} "
            f"{c['net_to_b']:>+6} {c['discordant']:>6} {c['p_value']:>8}"
        )

    pooled_n = tot_a + tot_b
    pooled_p = two_sided_binomial(tot_b, pooled_n)
    print()
    print(f"  pooled over {tot_n} questions: {tot_a} flips to A, {tot_b} to B")
    print(f"  pooled McNemar p = {pooled_p:.5f}")
    if pooled_p < 0.05:
        winner = "B" if tot_b > tot_a else "A"
        verdict = f"{winner} is better (p={pooled_p:.5f}, pooled across suites)"
    else:
        verdict = (
            f"no significant difference (p={pooled_p:.5f}); "
            f"{'B' if tot_b > tot_a else 'A'} leads but within chance"
        )
    print(f"  verdict: {verdict}")

    if args.save:
        payload = {
            "arm_a": args.arm_a,
            "arm_b": args.arm_b,
            "suites": per_suite,
            "pooled": {
                "questions": tot_n,
                "flips_to_a": tot_a,
                "flips_to_b": tot_b,
                "p_value": round(pooled_p, 5),
            },
            "verdict": verdict,
        }
        out = RESULTS / f"paired_{args.save}.json"
        out.write_text(json.dumps(payload, indent=2) + "\n")
        print(f"  saved -> {out.relative_to(RESULTS.parent)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
