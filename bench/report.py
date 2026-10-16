#!/usr/bin/env python3
"""Compare every saved result so variants can be judged side by side.

    python3 bench/report.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

RESULTS = Path(__file__).resolve().parent.parent / "results"


def load(pattern: str) -> list[dict]:
    out = []
    for p in sorted(RESULTS.glob(pattern)):
        try:
            out.append(json.loads(p.read_text()))
        except (OSError, json.JSONDecodeError) as e:
            print(f"  (skipped {p.name}: {e})", file=sys.stderr)
    return out


def cell(value: object, width: int = 14, scale: float = 1.0) -> str:
    """Fixed-width cell tolerating the nulls the bench harness emits."""
    if value is None:
        return f"{'-':>{width}}"
    if scale != 1.0:
        return f"{value * scale:>{width}.1f}"  # type: ignore[operator]
    return f"{value!s:>{width}}"


def pick(run: dict, pp: int, batch: int | None = None) -> dict | None:
    """Row for one prompt length at one batch size. A file can hold both a
    single-stream and a batched row for the same length, and only rows at the
    same batch size are comparable."""
    return next(
        (
            r
            for r in run["summary"]
            if r.get("pp") == pp and r.get("batch_size") == batch
        ),
        None,
    )


def throughput() -> None:
    runs = load("tp_*.json")
    if not runs:
        return
    labels = [r["label"] for r in runs]
    lengths = sorted({r["pp"] for run in runs for r in run["summary"] if r.get("pp")})
    # Labels are long enough to collide at 14 columns, which silently merges
    # neighbouring headers and makes the table unreadable.
    w = max(14, min(20, 2 + max(len(lbl) for lbl in labels)))

    for title, key, scale in (
        ("decode tok/s", "decode_tps", 1.0),
        ("TTFT seconds", "ttft_ms", 0.001),
        ("prefill tok/s", "prefill_tps", 1.0),
    ):
        print(f"\n== throughput, single stream: {title} by prompt length ==")
        print(f"  {'prompt':>8} " + "".join(f"{lbl[:w - 2]:>{w}}" for lbl in labels))
        shorts = 0
        for pp in lengths:
            cells = []
            for run in runs:
                r = pick(run, pp) or {}
                v = r.get(key)
                # A decode rate computed over a handful of tokens is not a
                # measurement (FINDINGS §27: 21.0 tok/s from a 2-token
                # generation, which read as "decode collapses at long context"
                # and was withdrawn). throughput.py flags those rows; this table
                # is where the number actually gets read, so it has to refuse
                # them rather than render them. ttft and prefill are unaffected
                # by a short generation and stay.
                if key == "decode_tps" and r.get("gen_short"):
                    shorts += 1
                    cells.append("short!".rjust(w))
                    continue
                cells.append(cell(v, width=w, scale=scale))
            print(f"  {pp:>8} " + "".join(cells))
        if shorts:
            print(f"  {'':>8}  ({shorts} cell(s) suppressed: the model returned "
                  f"far fewer tokens than requested, so no rate exists)")

    batches = sorted(
        {
            r["batch_size"]
            for run in runs
            for r in run["summary"]
            if r.get("batch_size")
        }
    )
    if not batches:
        return
    print("\n== throughput, batched: aggregate decode tok/s (per stream) ==")
    print(f"  {'pp':>7} {'bs':>3} " + "".join(f"{lbl:>18}" for lbl in labels))
    pairs = sorted(
        {
            (r["pp"], r["batch_size"])
            for run in runs
            for r in run["summary"]
            if r.get("batch_size")
        }
    )
    for pp, bs in pairs:
        cells = []
        for run in runs:
            r = pick(run, pp, bs)
            if not r or r.get("decode_tps") is None:
                cells.append(f"{'-':>18}")
            else:
                cells.append(
                    f"{r['decode_tps']} ({r['decode_tps_per_stream']})".rjust(18)
                )
        print(f"  {pp:>7} {bs:>3} " + "".join(cells))


def own_results(run: dict) -> list[dict]:
    """The records belonging to this run.

    Files written before the accuracy.py freshness fix hold the whole
    server-side history with the run's own records appended last, so read the
    trailing window rather than the first match per suite.
    """
    suites = list((run.get("request") or {}).get("benchmarks") or [])
    results = run.get("results") or []
    return results[-len(suites):] if suites else results


def accuracy() -> None:
    runs = load("acc_*.json")
    if not runs:
        return
    print("\n== accuracy: score by suite ==")
    own = {run["label"]: own_results(run) for run in runs}
    suites = sorted(
        {r["benchmark"] for rs in own.values() for r in rs if r.get("benchmark")}
    )
    labels = list(own)
    print(f"  {'suite':<16} " + "".join(f"{lbl[:17]:>18}" for lbl in labels))
    for s in suites:
        cells = []
        for lbl in labels:
            hit = next((r for r in own[lbl] if r.get("benchmark") == s), None)
            if hit and isinstance(hit.get("accuracy"), (int, float)):
                cells.append(f"{hit['accuracy'] * 100:>16.1f}% ")
            else:
                cells.append(f"{'-':>18}")
        print(f"  {s:<16} " + "".join(cells))
    print("\n  (n and seconds per suite)")
    for lbl, rs in own.items():
        ns = {r.get("benchmark"): [r.get("total"), round(r.get("time_s") or 0)] for r in rs}
        print(f"  {lbl:<20} {json.dumps(ns)}")


def context() -> None:
    runs = load("ctx_*.json")
    if not runs:
        return
    print("\n== context boundary ==")
    print(f"  {'label':<14} {'verified':>10} {'applied':>10} {'capped by':>12} {'prefill t/s':>12}")
    for run in runs:
        r = (run.get("raw") or {}).get("result") or {}
        print(
            f"  {run['label']:<14} {str(r.get('verified_tokens')):>10} "
            f"{str(r.get('applied_tokens')):>10} {str(r.get('capped_by')):>12} "
            f"{str(r.get('prefill_tps')):>12}"
        )


def ane() -> None:
    p = RESULTS / "ane_tune.json"
    if not p.exists():
        return
    rec = (json.loads(p.read_text()) or {}).get("recommendation") or {}
    print("\n== ANE tuner recommendation ==")
    print(
        f"  enabled={rec.get('enabled')} prefill_tps={rec.get('processing_tps')} "
        f"speedup={rec.get('speedup_percent')}%"
    )


def thinking_budget() -> None:
    p = RESULTS / "thinking_budget_ab.json"
    if not p.exists():
        return
    rec = json.loads(p.read_text())
    print("\n== reason profile: thinking budget A/B ==")
    print(
        f"  {'arm':<5} {'t/s':>7} {'think med':>10} {'think max':>10} "
        f"{'trunc':>6} {'over':>5} {'wrong':>6}"
    )
    for arm, s in (rec.get("summary") or {}).items():
        print(
            f"  {arm:<5} {s['median_tok_per_s']:>7} {s['median_reasoning_tokens']:>10} "
            f"{s['max_reasoning_tokens']:>10} {s['truncated']:>6} "
            f"{s['over_budget']:>5} {s['wrong']:>6}"
        )
    print(f"  budget={rec.get('budget_tokens')} tokens")
    print(f"  verdict: {rec.get('verdict')}")


def main() -> int:
    if not RESULTS.exists():
        print("no results yet")
        return 0
    throughput()
    context()
    ane()
    thinking_budget()
    accuracy()
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
