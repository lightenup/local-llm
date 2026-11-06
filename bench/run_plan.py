#!/usr/bin/env python3
"""Declare a benchmark chain's steps so the Running now page can show them.

    python3 bench/run_plan.py start flint-lcb500 \\
        --step "flint_mixed_lcb500|flint-mixed-3-4bit|livecodebench|500" \\
        --step "flint_oq8e_lcb500|flint-oQ8e|livecodebench|500"
    python3 bench/run_plan.py show
    python3 bench/run_plan.py clear

A chain script calls `start` once at the top; nothing per step. Each step is
label|model|suite[,suite...]|samples, where label is the --label given to
bench/accuracy.py. dashboard/live.py works out each step's state from what
already exists: a step is done when results/acc_<label>.json is written after
the plan started, running when oMLX reports that model on one of its suites,
pending otherwise.

The plan lives in results/.run_plan.json, untracked. `start` replaces any
earlier plan.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

PLAN = Path(__file__).resolve().parent.parent / "results" / ".run_plan.json"


def parse_step(s: str) -> dict:
    parts = s.split("|")
    if len(parts) != 4 or not all(parts) or not parts[3].isdigit():
        raise argparse.ArgumentTypeError(f"step must be label|model|suites|samples: {s!r}")
    label, model, suites, samples = parts
    return {"label": label, "model": model, "suites": suites.split(","),
            "samples": int(samples)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("start")
    st.add_argument("name")
    st.add_argument("--step", action="append", type=parse_step, required=True)
    st.add_argument("--started", type=float, default=None,
                    help="epoch the chain began (default: now); for a plan written "
                         "after its chain started")
    sub.add_parser("show")
    sub.add_parser("clear")
    a = ap.parse_args(argv)
    if a.cmd == "start":
        plan = {"name": a.name, "started": a.started or time.time(), "steps": a.step}
        PLAN.write_text(json.dumps(plan, indent=1))
        print(f"plan {a.name}: {len(a.step)} steps -> {PLAN.name}")
    elif a.cmd == "show":
        print(PLAN.read_text() if PLAN.exists() else "no plan")
    else:
        PLAN.unlink(missing_ok=True)
        print("plan cleared")
    return 0


if __name__ == "__main__":
    sys.exit(main())
