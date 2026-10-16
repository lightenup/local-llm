#!/usr/bin/env python3
"""The verifier substrate — one runner, two consumers (C9 and D1).

Why this exists as its own module. Wave 1 ended with a rule: *where something
can reject bad candidates, sample N and keep what passes*. Two backlog items
need that rule and need it differently:

  C9 needs a GATE      — binary, per-project, "is this candidate acceptable?",
                         answered as cheaply as possible, first pass wins.
  D1 needs a SCORER     — paired and per-item, "how often does this model produce
                         an acceptable candidate?", every candidate graded.

Those are the same operation read two ways, so the operation is defined once
here and both sit on top. `bench/best_of_n.py` is the prototype this
generalises: its verifier was `classify(extract_last_code_block(...))`, welded
to livecodebench's dataset shape, which is why it could not be reused.

THE SAFETY RULE, and it is the important one. A verifier that cannot run is not
the same as a verifier that failed. `Verdict.ok` is only ever True when a
verifier actually executed and actually passed. Every other outcome — no
verifier configured, verifier crashed, timed out, produced no artifact to check
— yields `ok=False` with `ran=False`, and a caller must not treat that as
licence to pick a candidate anyway. Picking blind is strictly worse than N=1,
because it spends N times the compute to select at random. This is the §G
fallback question, answered in code rather than in prose.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_TIMEOUT_S = 60.0

# Where a project declares what "acceptable" means for it. Looked up from the
# working directory upward, so a monorepo can put one at the root and override
# it per package.
SPEC_FILENAME = "verifiers.json"


# --------------------------------------------------------------------------
# Verdicts
# --------------------------------------------------------------------------

@dataclass
class Verdict:
    """The outcome of checking one candidate.

    `ok` and `ran` are separate on purpose. `ok=False, ran=True` means the
    verifier executed and rejected the candidate — real information, and the
    thing D1 counts. `ran=False` means no judgement was reached at all, and it
    must never be read as a rejection or (worse) as an acceptance.
    """
    ok: bool
    ran: bool
    reason: str
    detail: str = ""
    duration_s: float = 0.0

    @classmethod
    def passed(cls, reason: str = "ok", detail: str = "", t: float = 0.0) -> "Verdict":
        return cls(ok=True, ran=True, reason=reason, detail=detail, duration_s=t)

    @classmethod
    def failed(cls, reason: str, detail: str = "", t: float = 0.0) -> "Verdict":
        return cls(ok=False, ran=True, reason=reason, detail=detail, duration_s=t)

    @classmethod
    def inconclusive(cls, reason: str, detail: str = "", t: float = 0.0) -> "Verdict":
        """No judgement reached. Never counts as a pass, never as a fail."""
        return cls(ok=False, ran=False, reason=reason, detail=detail, duration_s=t)

    def __str__(self) -> str:
        state = "PASS" if self.ok else ("FAIL" if self.ran else "INCONCLUSIVE")
        return f"{state}({self.reason})"


# --------------------------------------------------------------------------
# Extraction: model output -> the artifact a verifier can actually check
# --------------------------------------------------------------------------

_FENCE = re.compile(r"```([A-Za-z0-9_+-]*)\n(.*?)```", re.DOTALL)


def extract(text: str, mode: str = "code") -> str | None:
    """Pull the checkable artifact out of a completion.

    Returns None when there is nothing to check, which the callers turn into an
    INCONCLUSIVE verdict rather than a failure — "the model wrote prose" and
    "the model wrote broken code" are different results and get counted apart.

    Modes:
      raw          the whole completion, unmodified
      code         the LAST fenced block, any language
      code:<lang>  the last fenced block tagged <lang> (falls back to any)
      xml          the last fenced block, else the first '<'-rooted document
    """
    if mode == "raw":
        return text or None

    blocks = _FENCE.findall(text or "")

    if mode.startswith("code"):
        want = mode.split(":", 1)[1].strip().lower() if ":" in mode else ""
        if want:
            tagged = [b for lang, b in blocks if lang.lower() == want]
            if tagged:
                return tagged[-1].strip()
        # Deliberate fallback: models routinely tag a PlantUML block `puml`,
        # `plantuml` or nothing at all, and rejecting on the tag would measure
        # label compliance rather than the thing being verified.
        return blocks[-1][1].strip() if blocks else None

    if mode == "xml":
        if blocks:
            return blocks[-1][1].strip()
        start = (text or "").find("<")
        return text[start:].strip() if start >= 0 else None

    raise ValueError(f"unknown extract mode: {mode!r}")


# --------------------------------------------------------------------------
# Verifiers
# --------------------------------------------------------------------------

def _substitute(template: str, *, file: str, dir: str) -> str:
    """Replace {file} and {dir}, and NOTHING else.

    Deliberately not `str.format`. A verifier command routinely contains literal
    braces — a dict literal in a `python3 -c` one-liner, a JSON payload, a
    PlantUML or draw.io body, shell brace expansion — and `str.format` would
    read every one of them as a placeholder and raise, or worse, silently
    substitute. Found the hard way: a `balanced('([{}])')` assertion blew up
    with "Replacement index 0 out of range". Only these two tokens are special.
    """
    return template.replace("{file}", file).replace("{dir}", dir)


@dataclass
class CommandVerifier:
    """Run a project-supplied command; exit 0 means acceptable.

    This is C9's per-project contract. The candidate is written to a temp file
    and the command is templated with `{file}` (and `{dir}` for its directory).
    Exit status is the whole judgement — no output parsing, because anything
    cleverer is a project's own business and belongs in its command.

    Run in a fresh temp directory with a wall-clock timeout. This executes
    model-generated content, so it carries the same caveat as
    `bench/lcb_audit.py`: it is a guard against accidents, not a sandbox
    against an adversary.
    """
    name: str
    command: str
    extract_mode: str = "code"
    suffix: str = ".txt"
    timeout_s: float = DEFAULT_TIMEOUT_S
    shell: bool = False
    env: dict = field(default_factory=dict)

    def verify(self, text: str) -> Verdict:
        artifact = extract(text, self.extract_mode)
        if artifact is None:
            return Verdict.inconclusive(
                "no_artifact",
                f"extract({self.extract_mode!r}) found nothing in "
                f"{len(text or '')} chars")

        t0 = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="verify-") as d:
            path = Path(d) / f"candidate{self.suffix}"
            path.write_text(artifact)
            cmd = _substitute(self.command, file=str(path), dir=d)
            try:
                proc = subprocess.run(
                    cmd if self.shell else shlex.split(cmd),
                    shell=self.shell, cwd=d, capture_output=True, text=True,
                    timeout=self.timeout_s,
                    env={**os.environ, **self.env},
                )
            except subprocess.TimeoutExpired:
                return Verdict.failed("timeout", f"exceeded {self.timeout_s}s",
                                      time.perf_counter() - t0)
            except FileNotFoundError as e:
                # The verifier itself is missing — a configuration fault, not a
                # candidate fault. Must not be counted against the model.
                return Verdict.inconclusive("verifier_missing", str(e),
                                            time.perf_counter() - t0)

        dt = time.perf_counter() - t0
        if proc.returncode == 0:
            return Verdict.passed("exit_0", (proc.stdout or "")[-400:], dt)
        return Verdict.failed(f"exit_{proc.returncode}",
                              ((proc.stderr or proc.stdout or "")[-400:]), dt)


@dataclass
class PythonSyntaxVerifier:
    """Cheapest useful gate: does the candidate parse as Python?

    Not a substitute for tests — it catches truncation and fenced-prose, which
    is what a large share of N=4's rejects actually are. Worth running first in
    a chain because it costs ~1 ms against a test suite's seconds.
    """
    name: str = "python-syntax"
    extract_mode: str = "code:python"

    def verify(self, text: str) -> Verdict:
        import ast
        artifact = extract(text, self.extract_mode)
        if artifact is None:
            return Verdict.inconclusive("no_artifact", "no fenced code block")
        t0 = time.perf_counter()
        try:
            ast.parse(artifact)
        except SyntaxError as e:
            return Verdict.failed("syntax_error", f"line {e.lineno}: {e.msg}",
                                  time.perf_counter() - t0)
        return Verdict.passed("parses", "", time.perf_counter() - t0)


@dataclass
class XMLWellFormedVerifier:
    """D1's 'XML validity' metric, and the draw.io half of the E lane."""
    name: str = "xml-wellformed"
    extract_mode: str = "xml"

    def verify(self, text: str) -> Verdict:
        import xml.etree.ElementTree as ET
        artifact = extract(text, self.extract_mode)
        if artifact is None:
            return Verdict.inconclusive("no_artifact", "no XML found")
        t0 = time.perf_counter()
        try:
            ET.fromstring(artifact)
        except ET.ParseError as e:
            return Verdict.failed("malformed", str(e), time.perf_counter() - t0)
        return Verdict.passed("wellformed", "", time.perf_counter() - t0)


@dataclass
class ChainVerifier:
    """Run verifiers in order; stop at the first that does not pass.

    Ordering is the caller's cost decision: put the cheap structural checks
    first so an expensive test run is only reached by candidates that could
    plausibly survive it.

    An INCONCLUSIVE link stops the chain and propagates, rather than being
    skipped over. A chain that could not run its second stage has not verified
    the candidate, and saying otherwise is exactly the failure this module
    exists to prevent.
    """
    name: str
    verifiers: list

    def verify(self, text: str) -> Verdict:
        total = 0.0
        last = Verdict.inconclusive("empty_chain", "no verifiers configured")
        for v in self.verifiers:
            last = v.verify(text)
            total += last.duration_s
            if not last.ok:
                return Verdict(last.ok, last.ran, f"{v.name}:{last.reason}",
                               last.detail, total)
        return Verdict.passed(f"{self.name}:all_passed", "", total)


class NullVerifier:
    """No verifier is configured. Always INCONCLUSIVE, never a pass.

    Deliberately not a no-op that returns True. If a project has no way to
    reject bad candidates, best-of-N has nothing to select on, and the correct
    behaviour is to fall back to a single sample from the stronger model —
    which is what the C9 gate does when it sees `ran=False`.
    """
    name = "null"

    def verify(self, text: str) -> Verdict:
        return Verdict.inconclusive(
            "no_verifier",
            "no verifier configured for this project; best-of-N cannot select")


# --------------------------------------------------------------------------
# The per-project contract
# --------------------------------------------------------------------------

BUILTIN = {
    "python-syntax": PythonSyntaxVerifier,
    "xml-wellformed": XMLWellFormedVerifier,
}


def find_spec(start: Path | str = ".") -> Path | None:
    """Search upward for verifiers.json, so subdirectories inherit the root."""
    cur = Path(start).resolve()
    for d in [cur, *cur.parents]:
        candidate = d / SPEC_FILENAME
        if candidate.is_file():
            return candidate
    return None


def load_verifier(spec_path: Path | None, name: str | None = None):
    """Build a verifier from a project's verifiers.json.

    Shape:
        {
          "default": "tests",
          "verifiers": {
            "tests":  {"chain": ["python-syntax", "pytest"]},
            "pytest": {"command": "python3 -m pytest -q {file}",
                       "extract": "code:python", "suffix": ".py",
                       "timeout_s": 120},
            "puml":   {"command": "plantuml -failfast2 {file}",
                       "extract": "code", "suffix": ".puml"}
          }
        }

    Returns NullVerifier when there is no spec, which is the honest answer and
    the one the gate knows how to handle.
    """
    if spec_path is None or not Path(spec_path).is_file():
        return NullVerifier()
    spec = json.loads(Path(spec_path).read_text())
    table = spec.get("verifiers") or {}
    want = name or spec.get("default")
    if not want or want not in table:
        return NullVerifier()

    def build(key: str, depth: int = 0):
        if depth > 5:
            raise ValueError(f"verifier chain too deep at {key!r}")
        if key in BUILTIN:
            return BUILTIN[key]()
        cfg = table.get(key)
        if cfg is None:
            raise ValueError(f"unknown verifier {key!r} in {spec_path}")
        if "chain" in cfg:
            return ChainVerifier(key, [build(k, depth + 1) for k in cfg["chain"]])
        return CommandVerifier(
            name=key,
            command=cfg["command"],
            extract_mode=cfg.get("extract", "code"),
            suffix=cfg.get("suffix", ".txt"),
            timeout_s=float(cfg.get("timeout_s", DEFAULT_TIMEOUT_S)),
            shell=bool(cfg.get("shell", False)),
            env=cfg.get("env") or {},
        )

    return build(want)
