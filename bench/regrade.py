#!/usr/bin/env python3
"""Re-score stored accuracy answers with the CURRENTLY installed oMLX graders.

Why: oMLX upgrades change graders under results already on disk. 0.7.1rc1
(2026-10-15) rewrote livecodebench's prompt and scoring for its 216 functional
problems, and mbpp.py, humaneval.py and the shared base.py all changed after
some baselines were collected. A paired comparison across grader versions
measures the grader as much as the model (FINDINGS §41).

Every result file keeps each question's full answer in `raw_response`, so the
grader half of that difference can be removed without a GPU: run every arm's
stored answers through today's extract_answer/check_answer. What this cannot
remove is a PROMPT change: an arm that was asked a different question gave a
different answer. Questions whose stored prompt differs from today's
format_prompt are reported and dropped from the comparison, not re-scored.

Must run under oMLX's own interpreter so it imports the installed graders:

    /Applications/oMLX.app/Contents/Resources/Python/cpython-3.11/bin/python3.11 \\
        bench/regrade.py acc_oq4e_code acc_g4moe_code ...

SECURITY: the code suites execute model-generated code, exactly as oMLX did
when the suites ran (its own subprocess sandbox and timeouts). Do not point it
at results from a model you would not already have run here.
"""
from __future__ import annotations

import asyncio
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

OMLX = Path("/Applications/oMLX.app/Contents/Resources")
sys.path.insert(0, str(OMLX))
from omlx.eval import BENCHMARKS  # noqa: E402

RESULTS = Path(__file__).resolve().parent.parent / "results"


def regrade(label: str, cache: dict) -> dict:
    data = json.loads((RESULTS / f"{label}.json").read_text())
    out = {"label": label, "suites": {}}
    for r in data["results"]:
        name = r["benchmark"]
        if name not in BENCHMARKS:
            continue
        bench = BENCHMARKS[name]()
        n = len(r["question_results"])
        key = (name, n)
        if key not in cache:
            items = asyncio.run(bench.load_dataset(sample_size=n))
            cache[key] = {str(it.get("id", i)): it for i, it in enumerate(items)}
        items = cache[key]

        def score(q, bench=bench, items=items):
            item = items.get(str(q["id"]))
            if item is None:
                return q["id"], None, "missing"
            prompt = "\n".join(m["content"] for m in bench.format_prompt(item))
            if prompt != q.get("question"):
                return q["id"], None, "prompt_changed"
            pred = bench.extract_answer(q.get("raw_response") or "", item)
            return q["id"], bool(pred) and bool(bench.check_answer(pred, item)), "ok"

        with ThreadPoolExecutor(8) as ex:
            rows = list(ex.map(score, r["question_results"]))
        stored = {str(q["id"]): bool(q.get("correct")) for q in r["question_results"]}
        ok = {qid: c for qid, c, s in rows if s == "ok"}
        changed = sum(1 for qid, c in ok.items() if c != stored[str(qid)])
        out["suites"][name] = {
            "n": n,
            "comparable": len(ok),
            "prompt_changed": sum(1 for _, _, s in rows if s == "prompt_changed"),
            "missing": sum(1 for _, _, s in rows if s == "missing"),
            "stored_acc": round(100 * sum(stored[str(k)] for k in ok) / max(len(ok), 1), 2),
            "regraded_acc": round(100 * sum(ok.values()) / max(len(ok), 1), 2),
            "verdict_changed": changed,
            "correct": {str(k): v for k, v in ok.items()},
        }
        s = out["suites"][name]
        print(f"  {label:22s} {name:14s} comparable {s['comparable']:3d}/{n} "
              f"stored {s['stored_acc']:5.1f} -> regraded {s['regraded_acc']:5.1f} "
              f"({changed} verdicts changed, {s['prompt_changed']} prompt-changed)",
              flush=True)
    return out


def main() -> int:
    labels = sys.argv[1:]
    if not labels:
        print(__doc__)
        return 2
    cache: dict = {}
    for label in labels:
        res = regrade(label, cache)
        (RESULTS / f"regrade_{label.removeprefix('acc_')}.json").write_text(
            json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
