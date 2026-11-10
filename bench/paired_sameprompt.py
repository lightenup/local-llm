#!/usr/bin/env python3
"""Paired exact McNemar over the questions both arms were asked identically.

paired_compare.py pairs by question id. Across an oMLX upgrade that can pair
different prompts: 0.7.1rc1 re-prompted livecodebench's 216 functional
problems (FINDINGS §41). Every result keeps the prompt it was asked
(`question`), so this pairs only ids whose stored prompt is byte-identical and
reports how many each suite kept.

    python3 bench/paired_sameprompt.py --save q36 acc_a,acc_b acc_c,acc_d
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

RESULTS = Path(__file__).resolve().parent.parent / "results"


def load(label: str) -> dict[str, dict[str, dict]]:
    data = json.loads((RESULTS / f"{label}.json").read_text())
    return {
        r["benchmark"]: {str(q["id"]): q for q in r["question_results"]}
        for r in data["results"]
    }


def exact_p(b: int, c: int) -> float:
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    return min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2**n)


def compare(a_label: str, b_label: str) -> dict:
    a, b = load(a_label), load(b_label)
    suites: dict[str, dict] = {}
    tot = {"n": 0, "a_correct": 0, "b_correct": 0, "a_only": 0, "b_only": 0}
    for name, qa in a.items():
        qb = b.get(name)
        if not qb:
            continue
        ids = [i for i in qa if i in qb and qa[i].get("question") == qb[i].get("question")]
        if not ids:
            continue
        ca = sum(bool(qa[i]["correct"]) for i in ids)
        cb = sum(bool(qb[i]["correct"]) for i in ids)
        x = sum(1 for i in ids if qa[i]["correct"] and not qb[i]["correct"])
        y = sum(1 for i in ids if qb[i]["correct"] and not qa[i]["correct"])
        suites[name] = {
            "n": len(ids), "of": len(qa),
            "a": round(100 * ca / len(ids), 1), "b": round(100 * cb / len(ids), 1),
            "a_only": x, "b_only": y, "p": float(f"{exact_p(x, y):.3g}"),
        }
        for k, v in (("n", len(ids)), ("a_correct", ca), ("b_correct", cb),
                     ("a_only", x), ("b_only", y)):
            tot[k] += v
    n = tot["n"] or 1
    pooled = {
        "n": tot["n"],
        "a": round(100 * tot["a_correct"] / n, 1), "b": round(100 * tot["b_correct"] / n, 1),
        "a_only": tot["a_only"], "b_only": tot["b_only"],
        "p": float(f"{exact_p(tot['a_only'], tot['b_only']):.3g}"),
    }
    return {"a": a_label, "b": b_label, "suites": suites, "pooled": pooled}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pairs", nargs="+", metavar="A,B")
    ap.add_argument("--save", help="results/paired_<name>_sameprompt.json")
    args = ap.parse_args()
    out = []
    for pair in args.pairs:
        a_label, b_label = pair.split(",", 1)
        r = compare(a_label, b_label)
        out.append(r)
        p = r["pooled"]
        print(f"== {a_label} vs {b_label}: n={p['n']} {p['a']} vs {p['b']} "
              f"flips {p['a_only']}/{p['b_only']} p={p['p']}")
        for name, s in r["suites"].items():
            print(f"   {name:14s} n={s['n']:3d}/{s['of']:<3d} {s['a']:5.1f} vs {s['b']:5.1f} "
                  f"flips {s['a_only']}/{s['b_only']} p={s['p']}")
    if args.save:
        path = RESULTS / f"paired_{args.save}_sameprompt.json"
        path.write_text(json.dumps({
            "method": "exact McNemar on questions whose stored prompt is identical in both arms",
            "pairs": out,
        }, indent=1) + "\n")
        print(f"saved -> {path.relative_to(RESULTS.parent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
