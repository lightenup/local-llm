"""How far each model has come through the evaluation pipeline.

Built read-only from what the harnesses already write, with no list to keep
in sync:

  gate      results/gate_*.json        bench/candidate_gate.py
  accuracy  results/acc_*.json         bench/accuracy.py
  task fit  results/task_*.json        bench/taskbench.py (model-named runs only;
                                       role-named runs depend on the date)
  decision  findings/*.toml status     shipped / on-trial / retired / dead-end

A finding names a model by a short id ("Qwen3.8-27B-oQ4e") where the results
use the full one ("Qwen3.8-27B-oQ4e-mtp"); a full id that starts with the
short one is the same model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

# Below this a suite run is a sample (a timing check), not a measurement.
FULL_RUN = 250
STAGES = ("gate", "accuracy", "task", "decision")


@dataclass
class SuiteRun:
    benchmark: str
    accuracy: float
    total: int
    truncated: int
    file: str
    # livecodebench only: oMLX 0.7.1rc1 re-prompted the 216 functional problems
    # (FINDINGS §41), so a score before and after the change does not compare
    harness: str | None = None


@dataclass
class Candidate:
    model: str
    gates: list[dict] = field(default_factory=list)
    suites: dict[str, SuiteRun] = field(default_factory=dict)
    samples: int = 0
    tasks: list[str] = field(default_factory=list)
    finding: object = None  # findings.Finding
    roles: list[str] = field(default_factory=list)

    # ---- per-stage state: "pass" | "fail" | "partial" | "none" ----
    def stage(self, name: str) -> str:
        if name == "gate":
            if not self.gates:
                return "none"
            if any(g.get("verdict", "").startswith("PASS") and not g.get("replaces")
                   for g in self.gates):
                return "pass"
            if any(g.get("verdict", "").startswith("PASS") for g in self.gates):
                return "partial"
            return "fail"
        if name == "accuracy":
            return "pass" if self.suites else ("partial" if self.samples else "none")
        if name == "task":
            return "pass" if self.tasks else "none"
        if self.roles:
            return "pass"
        status = getattr(self.finding, "status", None)
        return {"shipped": "pass", "retired": "fail", "dead-end": "fail",
                "on-trial": "partial", "open": "partial"}.get(status, "none")

    @property
    def decision(self) -> str:
        if self.roles:
            return "in lineup"
        status = getattr(self.finding, "status", None)
        if status:
            return status
        return "open" if self.gates else "not recorded"


def _load(p: Path):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def _lcb_harness(questions: list) -> str | None:
    if not questions:
        return None
    return ("current" if any("do not read from stdin" in (q.get("question") or "")
                             for q in questions) else "pre-0.7.1")


def build(results_dir: Path, findings: list, roles: list | None = None) -> list[Candidate]:
    by_model: dict[str, Candidate] = {}

    def get(model: str) -> Candidate:
        return by_model.setdefault(model, Candidate(model))

    for p in sorted(results_dir.glob("gate_*.json")):
        data = _load(p)
        for r in data if isinstance(data, list) else [data] if isinstance(data, dict) else []:
            if isinstance(r, dict) and r.get("model_id"):
                get(r["model_id"]).gates.append({**r, "file": p.name})

    for p in sorted(results_dir.glob("acc_*.json")):
        data = _load(p)
        if not isinstance(data, dict):
            continue
        for r in data.get("results") or []:
            if not isinstance(r, dict) or not r.get("model_id") or not r.get("benchmark"):
                continue
            c = get(r["model_id"])
            total = int(r.get("total") or 0)
            if total < FULL_RUN:
                c.samples += 1
                continue
            qs = r.get("question_results") or []
            run = SuiteRun(r["benchmark"], float(r.get("accuracy") or 0), total,
                           sum(1 for q in qs if q.get("finish_reason") == "length"), p.name,
                           _lcb_harness(qs) if r["benchmark"] == "livecodebench" else None)
            prev = c.suites.get(run.benchmark)
            # one run per suite: the current harness first, then the larger
            # sample, then the file name (mtimes differ per checkout)
            def rank(x: SuiteRun) -> tuple:
                return (x.harness != "pre-0.7.1", x.total, x.file)
            if prev is None or rank(run) > rank(prev):
                c.suites[run.benchmark] = run

    for p in sorted(results_dir.glob("task_*.json")):
        data = _load(p)
        model = (data or {}).get("model") if isinstance(data, dict) else None
        if model and "-" in model:  # model-named, not a role
            get(model.split(":", 1)[0]).tasks.append(p.name)

    for c in by_model.values():
        for f in findings:
            if any(c.model == m or c.model.startswith(m + "-") for m in f.models):
                # the most specific finding wins: shipped/retired over dead-end notes
                if c.finding is None or f.status in ("shipped", "retired", "on-trial"):
                    c.finding = f

    for role in roles or []:
        for c in by_model.values():
            if c.model == role.model:
                c.roles.append(role.role)

    order = {"open": 0, "on-trial": 1, "in lineup": 2, "shipped": 3, "retired": 4,
             "dead-end": 5, "not recorded": 6}
    return sorted(by_model.values(), key=lambda c: (order.get(c.decision, 9), c.model.lower()))
