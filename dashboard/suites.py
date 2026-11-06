"""Benchmark suite cards: load findings/suites/*.toml and summarise runs.

Each [[suite]] describes one benchmark (what it measures, how this repo runs
it, where to read more). The run summary is read from results/acc_*.json, the
files bench/accuracy.py writes; no score is shown, because suite scores here
only compare paired and on the same harness (FINDINGS §8, §41).
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from findings import Problem

GROUPS = ("code", "reason", "general", "safety", "harness")
GROUP_TITLES = {
    "code": "Code",
    "reason": "Reasoning and maths",
    "general": "General knowledge",
    "safety": "Safety and bias",
    "harness": "Our own harnesses",
}
REQUIRED = ("id", "name", "group", "measures", "size", "how_we_run", "links")


@dataclass(frozen=True)
class Link:
    label: str
    url: str


@dataclass(frozen=True)
class Suite:
    id: str
    name: str
    group: str
    measures: str
    size: str
    how_we_run: str
    links: tuple[Link, ...]
    findings: tuple[int, ...] = ()
    harness: bool = False
    run: bool = True


@dataclass
class RunSummary:
    runs: int = 0
    models: set[str] = field(default_factory=set)


def _validate(data: dict, fname: str, i: int, problems: list[Problem]) -> Suite | None:
    where = f"suite[{i}]"
    missing = [k for k in REQUIRED if k not in data]
    for k in missing:
        problems.append(Problem(fname, f"{where}.{k}", "required field missing"))
    if missing:
        return None
    if data["group"] not in GROUPS:
        problems.append(Problem(fname, f"{where}.group",
                                f"unknown group {data['group']!r}; one of {', '.join(GROUPS)}"))
        return None
    links = []
    for j, ln in enumerate(data["links"]):
        if not isinstance(ln, dict) or not ln.get("label") or not ln.get("url"):
            problems.append(Problem(fname, f"{where}.links[{j}]", "needs label and url"))
            return None
        links.append(Link(ln["label"], ln["url"]))
    return Suite(
        id=data["id"], name=data["name"], group=data["group"],
        measures=data["measures"], size=data["size"],
        how_we_run=data["how_we_run"], links=tuple(links),
        findings=tuple(data.get("findings", ())),
        harness=bool(data.get("harness", False)),
        run=bool(data.get("run", True)),
    )


def load_suites(suites_dir: Path) -> tuple[list[Suite], list[Problem]]:
    """Every [[suite]] in suites_dir/*.toml, in file then entry order.
    An absent directory is not an error: the view shows its empty state."""
    suites: list[Suite] = []
    problems: list[Problem] = []
    if not suites_dir.is_dir():
        return suites, problems
    seen: set[str] = set()
    for fname in sorted(suites_dir.glob("*.toml")):
        try:
            with open(fname, "rb") as fh:
                data = tomllib.load(fh)
        except tomllib.TOMLDecodeError as e:
            problems.append(Problem(fname.name, "", f"TOML parse error at line {e.lineno}: {e.msg}"))
            continue
        for i, entry in enumerate(data.get("suite", [])):
            s = _validate(entry, fname.name, i, problems)
            if s is None:
                continue
            if s.id in seen:
                problems.append(Problem(fname.name, f"suite[{i}].id", f"duplicate id {s.id!r}"))
                continue
            seen.add(s.id)
            suites.append(s)
    return suites, problems


def run_summaries(results_dir: Path) -> dict[str, RunSummary]:
    """benchmark id -> how many stored runs and which models, from acc_*.json.
    Unreadable files are skipped; this is context for a card, not a metric."""
    out: dict[str, RunSummary] = {}
    for p in sorted(results_dir.glob("acc_*.json")):
        try:
            data = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        for r in data.get("results") or []:
            b = r.get("benchmark") if isinstance(r, dict) else None
            if not b:
                continue
            s = out.setdefault(b, RunSummary())
            s.runs += 1
            if r.get("model_id"):
                s.models.add(r["model_id"])
    return out
