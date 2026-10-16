#!/usr/bin/env python3
"""Resolve a metric's source pointer into results/*.json.

A source is a file (relative to results/) plus a path expression with exactly
three chainable constructs (research.md R3):

    key                    object member
    [N]                    integer list index
    [?key=value]           predicate filter over a list of objects — LAST match wins

Resolution yields a scalar (number or string) or a stale outcome naming what
went wrong (missing file, unresolvable path, non-scalar). Stale is a runtime
condition (exit 2, site still written); a malformed path is a syntax error
caught at validation time (exit 1), so resolve() re-raises PathSyntaxError.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


class PathSyntaxError(ValueError):
    """A source_path that does not parse as a path expression."""


@dataclass(frozen=True)
class Resolution:
    outcome: str  # "value" | "stale"
    value: object = None
    reason: str | None = None

    @classmethod
    def value(cls, v: object) -> "Resolution":
        return cls("value", v, None)

    @classmethod
    def stale(cls, reason: str) -> "Resolution":
        return cls("stale", None, reason)


# ---- path-expression parsing --------------------------------------------

@dataclass(frozen=True)
class _Index:
    i: int


@dataclass(frozen=True)
class _Predicate:
    key: str
    value: object


@dataclass(frozen=True)
class _Step:
    key: str
    selector: object | None = None  # _Index | _Predicate | None


def _parse_predicate_value(raw: str) -> object:
    raw = raw.strip()
    if raw == "":
        raise PathSyntaxError("empty predicate value")
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    if raw.lstrip("-").isdigit():
        return int(raw)
    return raw


def parse_path(path: str) -> list[_Step]:
    """Parse a path expression into steps. Raises PathSyntaxError."""
    if not isinstance(path, str) or path == "":
        raise PathSyntaxError("empty path expression")
    steps: list[_Step] = []
    i, n = 0, len(path)
    while i < n:
        j = i
        while j < n and path[j] not in ".[":
            j += 1
        key = path[i:j]
        if key == "":
            raise PathSyntaxError(f"expected a key at position {i} in {path!r}")
        selector: object | None = None
        if j < n and path[j] == "[":
            close = path.find("]", j)
            if close == -1:
                raise PathSyntaxError(f"unterminated '[' in {path!r}")
            sel = path[j + 1:close]
            if sel.startswith("?"):
                body = sel[1:]
                if "=" not in body:
                    raise PathSyntaxError(f"bad predicate {sel!r} in {path!r}")
                pk, pv = body.split("=", 1)
                pk = pk.strip()
                if pk == "":
                    raise PathSyntaxError(f"empty predicate key in {path!r}")
                selector = _Predicate(pk, _parse_predicate_value(pv))
            else:
                s = sel.strip()
                if not s.lstrip("-").isdigit():
                    raise PathSyntaxError(f"bad index {sel!r} in {path!r}")
                selector = _Index(int(s))
            i = close + 1
        else:
            i = j
        steps.append(_Step(key, selector))
        if i < n:
            if path[i] != ".":
                raise PathSyntaxError(f"unexpected {path[i]!r} at position {i} in {path!r}")
            i += 1
    return steps


# ---- resolution ----------------------------------------------------------

_UNRESOLVED = object()


def _apply(node: object, step: _Step) -> object:
    if isinstance(node, dict):
        if step.key not in node:
            return _UNRESOLVED
        node = node[step.key]
    elif isinstance(node, list):
        # a bare key is a dict member; on a list only [N] / [?k=v] apply
        return _UNRESOLVED
    else:
        return _UNRESOLVED
    if step.selector is None:
        return node
    if isinstance(step.selector, _Index):
        if not isinstance(node, list) or not 0 <= step.selector.i < len(node):
            return _UNRESOLVED
        return node[step.selector.i]
    if isinstance(step.selector, _Predicate):
        if not isinstance(node, list):
            return _UNRESOLVED
        match = _UNRESOLVED
        for el in node:  # last match wins (bench/report.py::own_results)
            if isinstance(el, dict) and el.get(step.selector.key) == step.selector.value:
                match = el
        return match
    return _UNRESOLVED


def resolve(file: Path, path: str) -> Resolution:
    """Resolve `path` against the JSON at `file`.

    Data conditions (missing file, unresolvable or non-scalar path) become
    stale outcomes. A malformed path raises PathSyntaxError.
    """
    if not file.exists() or not file.is_file():
        return Resolution.stale(f"missing file: {file.name}")
    try:
        data = json.loads(file.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return Resolution.stale(f"unreadable file {file.name}: {e}")
    try:
        steps = parse_path(path)
    except PathSyntaxError as e:
        raise PathSyntaxError(f"{file.name}: {e}") from e
    node = data
    for step in steps:
        node = _apply(node, step)
        if node is _UNRESOLVED:
            return Resolution.stale(f"path {path!r} does not resolve in {file.name}")
    if isinstance(node, (dict, list)) or node is None:
        return Resolution.stale(f"path {path!r} is not a scalar in {file.name}")
    return Resolution.value(node)


def check_file_ref(file: str) -> None:
    """Validate a source_file reference stays inside results/. Raises ValueError."""
    if not isinstance(file, str) or file == "":
        raise ValueError("source_file must be a non-empty string")
    p = Path(file)
    if p.is_absolute():
        raise ValueError(f"source_file must be relative to results/: {file!r}")
    if ".." in p.parts:
        raise ValueError(f"source_file must not escape results/: {file!r}")
