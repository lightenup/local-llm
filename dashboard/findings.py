#!/usr/bin/env python3
"""Load and validate the curated findings layer (findings/*.toml).

One TOML file per finding; filename stem must equal the finding's id.
Validation follows data-model.md "Validation rules (FR-007)": a single run
aggregates ALL problems (file + field each). Data conditions (a well-formed
source path that does not resolve at runtime) are NOT validation failures —
they are stale outcomes handled by generate.py (exit 2).
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from sources import PathSyntaxError, check_file_ref, parse_path

STATUSES = ("shipped", "on-trial", "retired", "dead-end", "open")
DIRECTIONS = ("higher", "lower")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

REQUIRED_FINDING_FIELDS = {
    "id": str,
    "title": str,
    "status": str,
    "models": list,
    "section": int,
    "metrics": list,
}

REQUIRED_METRIC_FIELDS = {
    "name": str,
    "unit": str,
    "direction": str,
    "tooltip_what": str,
    "tooltip_how": str,
    "tooltip_evidence": str,
}


@dataclass
class Problem:
    file: str
    field: str
    message: str

    def __str__(self) -> str:
        return f"{self.file}: {self.field}: {self.message}"

    def __repr__(self) -> str:
        return str(self)


@dataclass
class Metric:
    name: str
    unit: str
    direction: str
    headline: bool
    tooltip_what: str
    tooltip_how: str
    tooltip_evidence: str
    source_file: str | None
    source_path: str | None
    baseline_label: str | None
    baseline_source_file: str | None
    baseline_source_path: str | None
    index: int  # position within the finding's metrics list (stable order)


@dataclass
class Finding:
    id: str
    title: str
    status: str
    models: list
    section: int
    metrics: list = field(default_factory=list)
    section_title: str | None = None
    rationale: str | None = None
    updated: str | None = None
    tier: str | None = None
    source_file: Path | None = None


def _type_name(value: object) -> str:
    return type(value).__name__


def _check_types(data: object, field_name: str, expected: type, problems: list[Problem], fname: str) -> bool:
    if not isinstance(data, expected) or (expected is int and isinstance(data, bool)):
        problems.append(Problem(fname, field_name, f"expected {expected.__name__}, got {_type_name(data)}"))
        return False
    return True


def _validate_metric(m: object, i: int, problems: list[Problem], fname: str) -> Metric | None:
    fname_field = f"metrics[{i}]"
    if not isinstance(m, dict):
        problems.append(Problem(fname, fname_field, f"expected a table, got {_type_name(m)}"))
        return None
    metric = None
    ok = True
    for f, t in REQUIRED_METRIC_FIELDS.items():
        if f not in m:
            problems.append(Problem(fname, f"{fname_field}.{f}", "missing required field"))
            ok = False
        elif not _check_types(m[f], f"{fname_field}.{f}", t, problems, fname):
            ok = False
    if ok and m["direction"] not in DIRECTIONS:
        problems.append(Problem(fname, f"{fname_field}.direction",
                                f"must be one of {', '.join(DIRECTIONS)}"))
        ok = False
    if not ok:
        return None
    source_file = m.get("source_file")
    source_path = m.get("source_path")
    if (source_file is None) != (source_path is None):
        problems.append(Problem(fname, f"{fname_field}.source",
                                "source_file and source_path must be given together"))
        return None
    if source_file is not None:
        if not _check_types(source_file, f"{fname_field}.source_file", str, problems, fname):
            return None
        if not _check_types(source_path, f"{fname_field}.source_path", str, problems, fname):
            return None
        try:
            check_file_ref(source_file)
        except ValueError as e:
            problems.append(Problem(fname, f"{fname_field}.source_file", str(e)))
            return None
        try:
            parse_path(source_path)
        except PathSyntaxError as e:
            problems.append(Problem(fname, f"{fname_field}.source_path", str(e)))
            return None
    headline = m.get("headline", False)
    if not isinstance(headline, bool):
        problems.append(Problem(fname, f"{fname_field}.headline", "expected boolean"))
        return None
    baseline_source_file = m.get("baseline_source_file")
    baseline_source_path = m.get("baseline_source_path")
    if (baseline_source_file is None) != (baseline_source_path is None):
        problems.append(Problem(fname, f"{fname_field}.baseline_source",
                                "baseline_source_file and baseline_source_path must be given together"))
        return None
    if baseline_source_file is not None:
        try:
            check_file_ref(baseline_source_file)
        except ValueError as e:
            problems.append(Problem(fname, f"{fname_field}.baseline_source_file", str(e)))
            return None
        try:
            parse_path(baseline_source_path)
        except PathSyntaxError as e:
            problems.append(Problem(fname, f"{fname_field}.baseline_source_path", str(e)))
            return None
    return Metric(
        name=m["name"],
        unit=m["unit"],
        direction=m["direction"],
        headline=headline,
        tooltip_what=m["tooltip_what"],
        tooltip_how=m["tooltip_how"],
        tooltip_evidence=m["tooltip_evidence"],
        source_file=source_file,
        source_path=source_path,
        baseline_label=m.get("baseline_label"),
        baseline_source_file=baseline_source_file,
        baseline_source_path=baseline_source_path,
        index=i,
    )


def _validate_finding(data: object, fname: Path, problems: list[Problem]) -> Finding | None:
    fname_s = fname.name
    if not isinstance(data, dict):
        problems.append(Problem(fname_s, "", f"expected a TOML table, got {_type_name(data)}"))
        return None
    ok = True
    for f, t in REQUIRED_FINDING_FIELDS.items():
        if f == "metrics":
            # an empty findings list is valid (a finding closed by argument or
            # by the measurement named in the rationale); absent or wrong type
            # is a problem
            if "metrics" not in data:
                data["metrics"] = []
            if not _check_types(data["metrics"], "metrics", list, problems, fname_s):
                ok = False
            continue
        if f not in data:
            problems.append(Problem(fname_s, f, "missing required field"))
            ok = False
        elif not _check_types(data[f], f, t, problems, fname_s):
            ok = False
    if ok:
        if not ID_RE.match(data["id"]):
            problems.append(Problem(fname_s, "id",
                                    f"must match {ID_RE.pattern}"))
            ok = False
        if data["id"] != fname.stem:
            problems.append(Problem(fname_s, "id",
                                    f"id {data['id']!r} does not equal filename stem {fname.stem!r}"))
        if data["status"] not in STATUSES:
            problems.append(Problem(fname_s, "status",
                                    f"must be one of {', '.join(STATUSES)}"))
            ok = False
        if data["section"] < 1:
            problems.append(Problem(fname_s, "section", "must be >= 1"))
            ok = False
        if data["title"] == "":
            problems.append(Problem(fname_s, "title", "must be non-empty"))
            ok = False
        for i, m in enumerate(data["models"]):
            if not _check_types(m, f"models[{i}]", str, problems, fname_s):
                ok = False
    if not ok:
        return None
    metrics = []
    names = set()
    for i, m in enumerate(data["metrics"]):
        metric = _validate_metric(m, i, problems, fname_s)
        if metric is None:
            continue
        if metric.name in names:
            problems.append(Problem(fname_s, f"metrics[{i}].name",
                                    f"duplicate metric name {metric.name!r}"))
            continue
        names.add(metric.name)
        metrics.append(metric)
    tier = data.get("tier")
    if tier is not None and tier not in ("quality", "fast", "additive"):
        problems.append(Problem(fname_s, "tier",
                                "must be one of quality, fast, additive"))
        return None
    return Finding(
        id=data["id"],
        title=data["title"],
        status=data["status"],
        models=list(data["models"]),
        section=data["section"],
        metrics=metrics,
        section_title=data.get("section_title"),
        rationale=data.get("rationale"),
        updated=str(data["updated"]) if data.get("updated") is not None else None,
        tier=tier,
        source_file=fname,
    )


def load_findings(findings_dir: Path) -> tuple[list[Finding], list[Problem]]:
    """Load and validate every findings/*.toml in findings_dir.

    Returns (findings, problems). An empty or absent directory is not an
    error. All problems found in the run are aggregated, not just the first.
    """
    findings: list[Finding] = []
    problems: list[Problem] = []
    if not findings_dir.is_dir():
        return findings, problems
    seen_ids: dict[str, str] = {}
    for fname in sorted(findings_dir.glob("*.toml")):
        try:
            with open(fname, "rb") as fh:
                data = tomllib.load(fh)
        except tomllib.TOMLDecodeError as e:
            problems.append(Problem(fname.name, "", f"TOML parse error at line {e.lineno}: {e.msg}"))
            continue
        finding = _validate_finding(data, fname, problems)
        if finding is None:
            continue
        if finding.id in seen_ids:
            problems.append(Problem(fname.name, "id",
                                    f"duplicate id {finding.id!r} (also in {seen_ids[finding.id]})"))
            continue
        seen_ids[finding.id] = fname.name
        findings.append(finding)
    findings.sort(key=lambda f: f.id)
    return findings, problems


def _slug(title: str) -> str:
    """GitHub anchor slug: lowercase, punctuation stripped, spaces -> dashes."""
    s = title.lower()
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    s = re.sub(r"[\s]+", "-", s.strip())
    return s


_HEADING_RE = re.compile(r"^## (\d+)\. (.+)$")


def load_sections(findings_md_path: Path) -> dict[int, tuple[str, str]]:
    """Parse `## N. Title` headings into number -> (title, anchor)."""
    sections: dict[int, tuple[str, str]] = {}
    if not findings_md_path.is_file():
        return sections
    for line in findings_md_path.read_text().splitlines():
        m = _HEADING_RE.match(line)
        if m:
            num = int(m.group(1))
            title = m.group(2).strip()
            sections[num] = (title, _slug(f"{num}. {title}"))
    return sections


def section_anchor(sections: dict[int, tuple[str, str]], num: int) -> str | None:
    entry = sections.get(num)
    return entry[1] if entry else None


def section_title_mismatches(findings: list[Finding],
                             sections: dict[int, tuple[str, str]]) -> list[str]:
    """Recorded section_title values that no longer match the heading (warnings)."""
    warnings = []
    for f in findings:
        if f.section_title is None:
            continue
        entry = sections.get(f.section)
        if entry is None:
            warnings.append(f"{f.id}: section {f.section} not found in FINDINGS.md")
        elif entry[0] != f.section_title:
            warnings.append(
                f"{f.id}: section_title {f.section_title!r} does not match "
                f"heading at section {f.section}: {entry[0]!r}")
    return warnings
