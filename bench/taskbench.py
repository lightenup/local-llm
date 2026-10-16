#!/usr/bin/env python3
"""D1 — task-shaped metrics: does a model do *this* job better?

The accuracy harness scores public suites. That was right for settling the
accuracy floor and it is wrong for choosing between models for this workload,
for two reasons FINDINGS already established:

  - A 500-sample suite cannot resolve a difference under ~2 points (§8), so
    suite scores have to be read as paired per-item comparisons or not at all.
  - oMLX uploads any suite result with >=100 questions to omlx.ai with a machine
    hash and model settings, with no opt-out (§11). A purpose-built harness
    sidesteps that entirely.

So this scores the things actually wanted from the models here, each with a
**deterministic verifier** rather than a judge:

    puml       does the diagram render, AND does it contain what was asked?
    drawio     is the XML usable, AND does it have the vertices, edges,
               geometry and styles the prompt specified?
    toolcall   is the tool call usable?      name correct, arguments parse,
                                             required keys present, VALUES
                                             matching the declared types/enums
    toolchain  did it USE what the tool returned, across two turns?
    pycode     does the code do the job?     asserted behaviour, exit 0,
                                             and any forbidden construct absent

VERIFY CONTENT, NOT JUST VALIDITY. The first two revisions of this file graded
diagrams on whether they compiled. They did not check whether the diagram was
the one requested, so `Alice -> Bob : hi` passed a C4 task specifying a
System_Boundary, a LAYOUT_WITH_LEGEND directive, a Person, a System_Ext and
typed Rel edges, and a three-cell stub passed a task demanding five
geometry-bearing vertices and four orthogonal edges. Every arm scored 100% on
both categories and they measured nothing (FINDINGS §27). Thirteen of the
twenty-seven tasks could not fail. Prompts were never the problem and making
them harder never helped; the verifiers were.

Every verifier is a `bench/verifier.py` object, so the same runner that C9 used
as a gate is used here as a scorer — one implementation, two readings.

PAIRED, NOT AGGREGATE. Two models are run over the identical task list and
compared item by item with McNemar, exactly as `bench/paired_compare.py` does
for the public suites. Comparing two independent pass rates at this sample size
would resolve nothing.

A CATEGORY AT 100% FOR BOTH ARMS IS NOT A TIE, IT IS A DEAD INSTRUMENT. Read
the per-category table in `compare` before reading its p-value.

SAMPLING IS NOT FIXED AT ZERO. The models are served through profiles that set
`force_sampling`, so oMLX ignores client sampling entirely and each profile's own
temperature applies. That is deliberate: the question is how the *configured
profile* behaves, not how the raw checkpoint behaves. It does mean repeats can
differ for non-zero-temperature profiles; `--repeats` exists for that.

    python3 bench/taskbench.py run --model coder --label coder
    python3 bench/taskbench.py run --model fast  --label fast
    python3 bench/taskbench.py compare coder fast
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verifier import (ChainVerifier, CommandVerifier, PythonSyntaxVerifier,  # noqa: E402
                      Verdict, XMLWellFormedVerifier, extract)

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results"
PROXY = os.environ.get("PROXY_BASE", "http://127.0.0.1:4000")
PLANTUML_JAR = Path.home() / ".local/share/plantuml/plantuml.jar"


def _key() -> str:
    env = ROOT / "proxy" / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("LITELLM_MASTER_KEY="):
                return line.split("=", 1)[1].strip()
    return os.environ.get("LITELLM_MASTER_KEY", "")


# --------------------------------------------------------------------------
# Verifiers beyond the substrate's built-ins
# --------------------------------------------------------------------------

class DrawioVerifier:
    """Well-formed XML is necessary and nowhere near sufficient.

    `XMLWellFormedVerifier` would pass `<a/>`. A draw.io file has to carry an
    mxGraphModel with actual cells, which is the difference between "parses" and
    "opens". Checked structurally rather than by rendering, because the draw.io
    CLI needs a display and is not safe to depend on unattended.

    STRUCTURAL REQUIREMENTS ARE THE POINT, NOT AN EXTRA. Until 2026-09-17 this
    checked only "is it well-formed and does it have >=2 cells", while the
    prompts asked for specific counts, geometry and styles. A three-cell stub
    with no geometry and no edges passed `hdio-geom`, which demands five
    geometry-bearing vertices and four orthogonal edges. Every arm scored
    drawio 100% and the category measured nothing (FINDINGS §27). The optional
    arguments below assert what the prompt actually asked for.
    """
    name = "drawio-structure"

    def __init__(self, min_vertices: int = 1, min_edges: int = 0,
                 geometry_on_vertices: bool = False,
                 edge_style_re: str | None = None,
                 edge_relative: bool = False,
                 vertex_style_re: str | None = None,
                 vertex_style_min: int = 1,
                 require_nesting: bool = False,
                 edges_connect_vertices: bool = False,
                 no_overlap: bool = False,
                 require_labels: list[str] | None = None,
                 exact_vertices: int | None = None):
        self.min_vertices = min_vertices
        self.min_edges = min_edges
        self.geometry_on_vertices = geometry_on_vertices
        self.edge_style_re = edge_style_re
        self.edge_relative = edge_relative
        self.vertex_style_re = vertex_style_re
        # How many vertices must carry that style, NOT all of them. The first
        # cut used min_vertices as the threshold, which failed `dio-swim` for
        # correctly giving a rhombus style to the one decision diamond and plain
        # styles to the two outcomes.
        self.vertex_style_min = vertex_style_min
        self.require_nesting = require_nesting
        # Added 2026-09-17 to get this category off the ceiling. With counts and
        # geometry alone every arm scored drawio 100%, because a diagram can
        # satisfy all of that and still be unopenable or meaningless:
        #   edges_connect_vertices -- an edge whose source/target names no real
        #     cell renders as a floating stub. Nothing checked this before.
        #   no_overlap -- `hdio-geom` asked for "non-overlapping" in its PROMPT
        #     and verified nothing of the sort, so a model could stack all five
        #     vertices at the same coordinates and pass.
        #   require_labels -- the prompt names the boxes; checking they exist is
        #     the difference between "a diagram" and "the requested diagram".
        self.edges_connect_vertices = edges_connect_vertices
        self.no_overlap = no_overlap
        self.require_labels = require_labels or []
        self.exact_vertices = exact_vertices

    def verify(self, text: str) -> Verdict:
        import re
        import xml.etree.ElementTree as ET
        art = extract(text, "xml")
        if art is None:
            return Verdict.inconclusive("no_artifact", "no XML found")
        t0 = time.perf_counter()

        def fail(reason, detail):
            return Verdict.failed(reason, detail, time.perf_counter() - t0)

        try:
            root = ET.fromstring(art)
        except ET.ParseError as e:
            return fail("malformed", str(e))
        model = root if root.tag == "mxGraphModel" else root.find(".//mxGraphModel")
        if model is None:
            return fail("no_mxGraphModel", f"root is <{root.tag}>")
        cells = model.findall(".//mxCell")
        if len(cells) < 2:      # the two implicit root cells alone are an empty page
            return fail("empty_graph", f"{len(cells)} mxCell")

        verts = [c for c in cells if c.get("vertex") == "1"]
        edges = [c for c in cells if c.get("edge") == "1"]
        if len(verts) < self.min_vertices:
            return fail("too_few_vertices", f"{len(verts)} < {self.min_vertices}")
        if len(edges) < self.min_edges:
            return fail("too_few_edges", f"{len(edges)} < {self.min_edges}")

        if self.geometry_on_vertices:
            bad = []
            for c in verts:
                g = c.find("mxGeometry")
                if g is None or not all(g.get(k) for k in ("x", "y", "width", "height")):
                    bad.append(c.get("id"))
            if bad:
                return fail("vertex_geometry_missing",
                            f"{len(bad)}/{len(verts)} without x/y/w/h: {bad[:4]}")

        if self.vertex_style_re:
            n = sum(1 for c in verts
                    if re.search(self.vertex_style_re, c.get("style") or ""))
            if n < self.vertex_style_min:
                return fail("vertex_style_missing",
                            f"{n} vertices match /{self.vertex_style_re}/, "
                            f"want {self.vertex_style_min}")

        if self.edge_style_re:
            n = sum(1 for c in edges
                    if re.search(self.edge_style_re, c.get("style") or ""))
            if n < self.min_edges:
                return fail("edge_style_missing",
                            f"{n}/{len(edges)} match /{self.edge_style_re}/")

        if self.edge_relative:
            n = sum(1 for c in edges
                    if (c.find("mxGeometry") is not None
                        and c.find("mxGeometry").get("relative") == "1"))
            if n < self.min_edges:
                return fail("edge_relative_missing",
                            f"{n}/{len(edges)} have mxGeometry relative=1")

        if self.require_nesting:
            # A child is a cell whose parent is another *vertex*, not the page.
            vids = {c.get("id") for c in verts}
            page = {c.get("id") for c in cells if c.get("vertex") != "1"}
            nested = [c for c in verts
                      if c.get("parent") in vids - page and c.get("parent") is not None]
            if not nested:
                return fail("no_nesting", "no vertex has another vertex as parent")

        if self.exact_vertices is not None and len(verts) != self.exact_vertices:
            return fail("wrong_vertex_count",
                        f"{len(verts)} vertices, asked for exactly "
                        f"{self.exact_vertices}")

        if self.edges_connect_vertices:
            vids = {c.get("id") for c in verts}
            dangling = [c.get("id") for c in edges
                        if c.get("source") not in vids or c.get("target") not in vids]
            if dangling:
                return fail("dangling_edge",
                            f"{len(dangling)}/{len(edges)} edges reference a "
                            f"non-vertex source/target: {dangling[:3]}")

        if self.require_labels:
            vals = " | ".join((c.get("value") or "") for c in cells).lower()
            missing = [lbl for lbl in self.require_labels if lbl.lower() not in vals]
            if missing:
                return fail("missing_labels", ",".join(missing))

        if self.no_overlap:
            boxes = []
            for c in verts:
                g = c.find("mxGeometry")
                if g is None:
                    continue
                try:
                    x, y = float(g.get("x") or 0), float(g.get("y") or 0)
                    w, h = float(g.get("width") or 0), float(g.get("height") or 0)
                except ValueError:
                    continue
                # Children are positioned relative to their parent, so mixing
                # them with page-level cells would report false overlaps.
                if c.get("parent") in {v.get("id") for v in verts}:
                    continue
                boxes.append((c.get("id"), x, y, w, h))
            for i in range(len(boxes)):
                for j in range(i + 1, len(boxes)):
                    a, b = boxes[i], boxes[j]
                    if (a[1] < b[1] + b[3] and b[1] < a[1] + a[3]
                            and a[2] < b[2] + b[4] and b[2] < a[2] + a[4]):
                        return fail("overlapping_vertices",
                                    f"{a[0]} and {b[0]} overlap")

        return Verdict.passed(
            f"{len(verts)}v/{len(edges)}e", "", time.perf_counter() - t0)


class ContentVerifier:
    """Assert the artifact contains what the prompt asked for.

    Chained AFTER a syntax verifier, never instead of one. The split matters:
    `plantuml -checkonly` answers "does it compile", this answers "is it the
    diagram that was requested", and conflating them is what let a two-line
    `Alice -> Bob : hi` score a pass on a C4 task specifying a System_Boundary,
    a LAYOUT_WITH_LEGEND directive, a Person, a System_Ext and typed Rel edges.

    `forbid` exists because instruction-following includes negative
    instructions, and those are where models here actually fail.

    WRITE PATTERNS THAT ACCEPT EVERY CORRECT NOTATION. A verifier that encodes
    one spelling of the requirement measures notation preference, not
    capability. `puml-class` first demanded /abstract\\s+class\\s+Shape/ and
    failed the shipped 27B for writing `class Shape { <<abstract>> }`, which is
    equally valid PlantUML. When a task fails, check the artifact before
    believing the model got it wrong.
    """
    name = "content"

    def __init__(self, require_re: dict[str, str] | None = None,
                 min_counts: dict[str, int] | None = None,
                 forbid_re: dict[str, str] | None = None):
        self.require_re = require_re or {}
        self.min_counts = min_counts or {}
        self.forbid_re = forbid_re or {}

    def verify(self, text: str) -> Verdict:
        import re
        art = extract(text, "code") or text
        t0 = time.perf_counter()
        missing = [lbl for lbl, pat in self.require_re.items()
                   if not re.search(pat, art, re.I | re.M)]
        if missing:
            return Verdict.failed("missing_required", ",".join(missing[:5]),
                                  time.perf_counter() - t0)
        for lbl, (pat, want) in ((k, v) for k, v in self.min_counts.items()):
            got = len(re.findall(pat, art, re.I | re.M))
            if got < want:
                return Verdict.failed("too_few", f"{lbl}: {got} < {want}",
                                      time.perf_counter() - t0)
        present = [lbl for lbl, pat in self.forbid_re.items()
                   if re.search(pat, art, re.I | re.M)]
        if present:
            return Verdict.failed("forbidden_present", ",".join(present[:5]),
                                  time.perf_counter() - t0)
        return Verdict.passed("content_ok", "", time.perf_counter() - t0)


def puml_content(require_re=None, min_counts=None, forbid_re=None) -> ChainVerifier:
    """Compiles AND says what it was asked to say."""
    return ChainVerifier("puml+content", [
        PlantUmlVerifier(),
        ContentVerifier(require_re=require_re, min_counts=min_counts,
                        forbid_re=forbid_re),
    ])



class PlantUmlVerifier:
    """Syntax-check via PlantUML itself.

    `-checkonly` renders nothing, so this costs ~1 s instead of a full image,
    and `-failfast2` exits non-zero on the first error rather than emitting an
    image *containing the word 'error'*, which is how PlantUML reports failure
    by default.

    NETWORK FAILURES ARE INCONCLUSIVE, NOT FAILURES. A model that writes
    `!include https://.../C4_Container.puml` makes the verifier depend on
    GitHub being reachable and on that exact path still existing. Observed: a
    404 from raw.githubusercontent.com scored as a model failure when the
    diagram source was fine. A benchmark whose verdicts move with someone
    else's CDN is not a benchmark, so those are separated out.
    """
    name = "plantuml"

    NETWORK_SIGNS = ("raw.githubusercontent.com", "java.io.IOException",
                     "HTTP error", "UnknownHostException", "ConnectException",
                     "SSLHandshakeException", "ExecutionException")

    # A remote include that PlantUML could not fetch does not always name the
    # host in its error -- on 2026-09-17 `hpuml-c4bound` came back a bare
    # exit_100 "Some diagram description contains errors", which the signs above
    # do not match, and it scored as a model failure. The same prompt passed on
    # re-run minutes later. Rather than widen the signs (exit_100 is also how a
    # genuine syntax error reports, and C4 syntax is exactly what that task
    # tests), the URL is probed at verify time and ONLY on failure: unreachable
    # now means inconclusive, reachable means the model really did get it wrong.
    INCLUDE_RE = r"!include(?:url)?\s+(https?://\S+)"

    @staticmethod
    def _reachable(url: str, timeout: float = 8.0) -> bool:
        import urllib.error
        import urllib.request
        try:
            req = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return 200 <= getattr(r, "status", 200) < 400
        except Exception:
            return False

    def __init__(self):
        self._cmd = CommandVerifier(
            name="plantuml",
            command=f'java -DPLANTUML_LIMIT_SIZE=16384 -jar {PLANTUML_JAR} '
                    f'-failfast2 -checkonly {{file}}',
            extract_mode="code", suffix=".puml", timeout_s=90)

    def verify(self, text: str) -> Verdict:
        import re
        v = self._cmd.verify(text)
        if not v.ok and v.ran and any(sign in v.detail for sign in self.NETWORK_SIGNS):
            return Verdict.inconclusive("network_include", v.detail[:200],
                                        v.duration_s)
        if not v.ok and v.ran:
            art = extract(text, "code") or ""
            urls = re.findall(self.INCLUDE_RE, art)
            if urls:
                # A transient fetch failure does not always name the host and
                # does not reliably repeat. On 2026-09-17 the 8-bit's
                # hpuml-c4bound came back exit_100 "No diagram found" in the
                # same run where five sibling attempts were classified
                # network_include -- the CDN was flaky, the reachability probe
                # happened to succeed, and a good diagram scored as a failure.
                # Retry once before believing it.
                v2 = self._cmd.verify(text)
                if v2.ok:
                    return v2
                if any(s in v2.detail for s in self.NETWORK_SIGNS):
                    return Verdict.inconclusive("network_include", v2.detail[:200],
                                                v2.duration_s)
                unreachable = [u for u in urls if not self._reachable(u)]
                if unreachable:
                    return Verdict.inconclusive(
                        "network_include_unreachable",
                        f"{unreachable[0][:90]} not fetchable now; "
                        f"plantuml said: {v2.detail[:90]}",
                        v2.duration_s)
                return v2
        return v


def puml_verifier() -> "PlantUmlVerifier":
    return PlantUmlVerifier()


def pycode_verifier(assertion: str, forbid_re: dict[str, str] | None = None
                    ) -> ChainVerifier:
    stages = [PythonSyntaxVerifier()]
    # Negative constraints are checked BEFORE the behaviour, so "passed by
    # cheating" reports as the constraint violation rather than as a pass.
    if forbid_re:
        stages.append(ContentVerifier(forbid_re=forbid_re))
    stages.append(CommandVerifier(
        name="behaviour",
        command=f"python3 -c \"exec(open('{{file}}').read());{assertion}\"",
        extract_mode="code:python", suffix=".py", timeout_s=30))
    return ChainVerifier("pycode", stages)


class ToolCallVerifier:
    """Graded from the API response, not from text, so it is constructed with
    the required argument names and applied to a parsed tool_calls list."""
    name = "toolcall"

    def __init__(self, fn: str, required: list[str],
                 arg_types: dict[str, type] | None = None,
                 enums: dict[str, list] | None = None,
                 arg_re: dict[str, str] | None = None):
        self.fn, self.required = fn, required
        self.arg_types = arg_types or {}
        self.enums = enums or {}
        self.arg_re = arg_re or {}

    def verify_response(self, msg: dict) -> Verdict:
        import re
        calls = msg.get("tool_calls") or []
        if not calls:
            body = (msg.get("content") or "")[:160]
            return Verdict.failed("degraded_to_prose", body)
        f = calls[0].get("function", {})
        if f.get("name") != self.fn:
            return Verdict.failed("wrong_function", str(f.get("name")))
        try:
            args = json.loads(f.get("arguments") or "{}")
        except json.JSONDecodeError as e:
            return Verdict.failed("args_not_json", str(e))
        missing = [k for k in self.required if k not in args]
        if missing:
            return Verdict.failed("missing_args", ",".join(missing))
        # Presence is the easy half. A schema is only honoured if the VALUES fit
        # it too -- an integer where a string belongs, or a free-text string
        # where an enum was declared, is a call the caller cannot execute.
        for k, want in self.arg_types.items():
            if k in args and not isinstance(args[k], want):
                return Verdict.failed(
                    "wrong_arg_type",
                    f"{k}={args[k]!r} is {type(args[k]).__name__}, want {want.__name__}")
        for k, allowed in self.enums.items():
            if k in args and args[k] not in allowed:
                return Verdict.failed("enum_violation", f"{k}={args[k]!r}")
        for k, pat in self.arg_re.items():
            if k in args and not re.search(pat, str(args[k])):
                return Verdict.failed("arg_shape", f"{k}={str(args[k])[:60]!r}")
        return Verdict.passed("usable")


class ToolChainVerifier:
    """Multi-turn: did it call the right tool SECOND, given the first result?

    Single-turn tool tasks ask "can it emit a tool call". That is not the
    agentic question, and it is the half that saturates -- every arm scored
    >=80% on the single-turn set while the whole category carried the only
    signal in the benchmark (FINDINGS §27). The real question is whether the
    model uses what a tool returned: reads a search result and edits the file
    the search actually found, rather than a path it invented.

    `expect_fn` is the second call's name; `derived` maps argument name to a
    literal that must appear in it, and those literals are present ONLY in the
    injected tool result, never in the prompt. So a model that ignores the tool
    output cannot satisfy them by guessing from the question.
    """
    name = "toolchain"

    def __init__(self, expect_fn: str, required: list[str],
                 derived: dict[str, str]):
        self.expect_fn, self.required, self.derived = expect_fn, required, derived

    def verify_response(self, msg: dict) -> Verdict:
        calls = msg.get("tool_calls") or []
        if not calls:
            return Verdict.failed("degraded_to_prose",
                                  (msg.get("content") or "")[:160])
        f = calls[0].get("function", {})
        if f.get("name") != self.expect_fn:
            return Verdict.failed("wrong_second_call", str(f.get("name")))
        try:
            args = json.loads(f.get("arguments") or "{}")
        except json.JSONDecodeError as e:
            return Verdict.failed("args_not_json", str(e))
        missing = [k for k in self.required if k not in args]
        if missing:
            return Verdict.failed("missing_args", ",".join(missing))
        ignored = [k for k, lit in self.derived.items()
                   if lit.lower() not in str(args.get(k, "")).lower()]
        if ignored:
            return Verdict.failed(
                "ignored_tool_result",
                "; ".join(f"{k} lacks {self.derived[k]!r}, got "
                          f"{str(args.get(k))[:40]!r}" for k in ignored[:2]))
        return Verdict.passed("used_tool_result")



# --------------------------------------------------------------------------
# The task set
# --------------------------------------------------------------------------

EDIT_TOOL = {
    "type": "function",
    "function": {
        "name": "edit_file",
        "description": "Replace an exact string in a file.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "old_string": {"type": "string"},
            "new_string": {"type": "string"}},
            "required": ["path", "old_string", "new_string"]}}}

SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "search_code",
        "description": "Search the repository for a regex.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string"}, "glob": {"type": "string"},
            "case_sensitive": {"type": "boolean"}},
            "required": ["pattern", "glob"]}}}

# Schema-hard tools. The existing two accept any string for anything, so a call
# is "usable" the moment the keys are present. These declare enums, integers,
# booleans and a nested object -- the places where a model that has pattern-
# matched the shape of a tool call rather than read the schema comes apart.
RUN_TOOL = {
    "type": "function",
    "function": {
        "name": "run_task",
        "description": "Run a build or test task in a workspace.",
        "parameters": {"type": "object", "properties": {
            "task": {"type": "string", "enum": ["build", "test", "lint", "typecheck"]},
            "timeout_s": {"type": "integer", "minimum": 1, "maximum": 3600},
            "fail_fast": {"type": "boolean"},
            "env": {"type": "object", "properties": {
                "name": {"type": "string"},
                "value": {"type": "string"}}, "required": ["name", "value"]}},
            "required": ["task", "timeout_s", "fail_fast"]}}}

DEPLOY_TOOL = {
    "type": "function",
    "function": {
        "name": "deploy",
        "description": "Deploy a built artifact to an environment.",
        "parameters": {"type": "object", "properties": {
            "environment": {"type": "string",
                            "enum": ["dev", "staging", "production"]},
            "version": {"type": "string",
                        "description": "semver, e.g. 1.4.2"},
            "replicas": {"type": "integer"}},
            "required": ["environment", "version", "replicas"]}}}


PUML = "Return ONLY a fenced code block containing PlantUML source, no prose. "
DIO = ("Return ONLY a fenced code block containing draw.io XML with an "
       "mxGraphModel root, no prose. ")
PY = "Return ONLY a fenced ```python code block, no prose. "


def build_tasks() -> list[dict]:
    return [
        # ---- diagrams: the deterministic-verifier case the E lane rests on ----
        # Content assertions are light here on purpose. The easy tier's job is to
        # catch a model that cannot emit the format at all; the hard tier below
        # does the discriminating. But "light" is not "absent" -- before
        # 2026-09-17 these checked syntax only, and a diagram of the wrong kind
        # entirely scored a pass.
        {"id": "puml-seq", "cat": "puml",
         "verifier": puml_content(
             min_counts={"participants": (r"^\s*(participant|actor)\b", 3),
                         "messages": (r"->", 4)}),
         "prompt": PUML + "A sequence diagram: a Browser calls an API Gateway, "
                          "which calls an Auth service and then a Database, and "
                          "returns to the Browser. Declare each participant."},
        {"id": "puml-c4", "cat": "puml",
         "verifier": puml_content(
             require_re={"include": r"!include", "person": r"\bPerson\s*\(",
                         "container": r"\bContainer\w*\s*\("}),
         "prompt": PUML + "A C4 container diagram for a web app: a Person 'User' "
                          "uses a 'Web App' container, which reads from a "
                          "'Postgres' database container. Use the C4-PlantUML "
                          "includes."},
        {"id": "puml-class", "cat": "puml",
         "verifier": puml_content(
             # Both notations are correct PlantUML; accept either.
             require_re={"abstract": r"abstract\s+(class\s+)?Shape"
                                     r"|class\s+Shape[\s\S]{0,120}?<<\s*abstract\s*>>",
                         "area": r"area\s*\("},
             min_counts={"inheritance": (r"<\|--|--\|>", 2)}),
         "prompt": PUML + "A class diagram: abstract class Shape with area(); "
                          "Circle and Rectangle extend it; Rectangle has width "
                          "and height."},
        {"id": "puml-state", "cat": "puml",
         "verifier": puml_content(
             require_re={"cancelled": r"Cancelled"},
             min_counts={"transitions": (r"-->", 4)}),
         "prompt": PUML + "A state diagram for an order: Pending -> Paid -> "
                          "Shipped -> Delivered, with Pending -> Cancelled."},
        {"id": "puml-act", "cat": "puml",
         "verifier": puml_content(
             require_re={"branch": r"^\s*if\s*\(", "else": r"^\s*else"},
             min_counts={"activities": (r"^\s*:", 4)}),
         "prompt": PUML + "An activity diagram for CI: checkout, build, test; "
                          "if tests pass deploy, otherwise notify."},

        # ---- draw.io XML validity ----
        {"id": "dio-boxes", "cat": "drawio",
         "verifier": DrawioVerifier(min_vertices=3, min_edges=2,
                                    edges_connect_vertices=True,
                                    require_labels=["Client", "Server", "Database"]),
         "prompt": DIO + "Three boxes left to right labelled Client, Server, "
                         "Database, connected by arrows. Every edge must have "
                         "source and target attributes naming real cell ids."},
        {"id": "dio-layers", "cat": "drawio",
         "verifier": DrawioVerifier(min_vertices=2, min_edges=1,
                                    edges_connect_vertices=True,
                                    require_labels=["Frontend", "Backend", "HTTPS"]),
         "prompt": DIO + "A two-layer diagram: a 'Frontend' box above a "
                         "'Backend' box, with a labelled arrow 'HTTPS' between "
                         "them. The arrow must carry the label as its value and "
                         "connect the two boxes by id."},
        {"id": "dio-swim", "cat": "drawio",
         "verifier": DrawioVerifier(min_vertices=3, min_edges=2,
                                    vertex_style_re=r"rhombus|shape=",
                                    edges_connect_vertices=True,
                                    require_labels=["Valid?", "Accept", "Reject"]),
         "prompt": DIO + "A flowchart with a decision diamond 'Valid?' going to "
                         "'Accept' and 'Reject'. The diamond must use a rhombus "
                         "style and both edges must connect real cell ids."},



        # ---- code that has to actually work ----
        {"id": "py-merge", "cat": "pycode",
         "verifier": pycode_verifier(
             "assert merge_sorted([1,3,5],[2,4,6])==[1,2,3,4,5,6];"
             "assert merge_sorted([],[])==[]"),
         "prompt": PY + "Write `merge_sorted(a, b)` merging two sorted lists."},
        {"id": "py-rle", "cat": "pycode",
         "verifier": pycode_verifier(
             "assert rle('aaabbc')==[('a',3),('b',2),('c',1)];assert rle('')==[]"),
         "prompt": PY + "Write `rle(s)` returning run-length encoding as a list "
                        "of (char, count) tuples."},
        {"id": "py-balanced", "cat": "pycode",
         "verifier": pycode_verifier(
             "assert balanced('([{}])');assert not balanced('([)]');"
             "assert balanced('')"),
         "prompt": PY + "Write `balanced(s)` returning True if brackets ()[]{} "
                        "are balanced."},
        {"id": "py-roman", "cat": "pycode",
         "verifier": pycode_verifier(
             "assert to_roman(1994)=='MCMXCIV';assert to_roman(4)=='IV';"
             "assert to_roman(2024)=='MMXXIV'"),
         "prompt": PY + "Write `to_roman(n)` converting 1..3999 to a Roman "
                        "numeral string."},
        {"id": "py-diff", "cat": "pycode",
         "verifier": pycode_verifier(
             "assert chunk([1,2,3,4,5],2)==[[1,2],[3,4],[5]];"
             "assert chunk([],3)==[]"),
         "prompt": PY + "Write `chunk(xs, n)` splitting a list into lists of at "
                        "most n items."},

        # ---- HARD tier ---------------------------------------------------
        # The first pass of this file scored 16/16 on BOTH tiers, which measured
        # nothing: a task set every candidate passes has no discriminating
        # power, and reporting it as "no significant difference" would have been
        # a statement about the tasks, not the models. These are sized at the
        # edge of capability instead - precise syntax recall, multi-constraint
        # output, and contracts that must be honoured exactly.

        {"id": "hpuml-c4bound", "cat": "puml",
         "verifier": puml_content(
             require_re={"include": r"!include", "boundary": r"System_Boundary\s*\(",
                         "person": r"\bPerson\s*\(", "ext": r"System_Ext\s*\(",
                         "db": r"ContainerDb\s*\(", "legend": r"LAYOUT_WITH_LEGEND"},
             min_counts={"Container()": (r"\bContainer\s*\(", 2),
                         "Rel() edges": (r"\bRel\w*\s*\(", 4)}),
         "prompt": PUML + "A C4 Container diagram using C4-PlantUML with "
                          "!include. Wrap the containers in a "
                          "System_Boundary(app, 'Shop'). Inside it: "
                          "Container(web,'Web','React'), "
                          "Container(api,'API','Go'), "
                          "ContainerDb(db,'Orders','Postgres'). Outside it a "
                          "Person(user,'Customer') and "
                          "System_Ext(pay,'Stripe'). Add Rel() edges with "
                          "technology labels for every connection, and a "
                          "LAYOUT_WITH_LEGEND() directive."},
        {"id": "hpuml-altloop", "cat": "puml",
         "verifier": puml_content(
             require_re={"autonumber": r"^\s*autonumber", "loop": r"^\s*loop\b",
                         "alt": r"^\s*alt\b", "else": r"^\s*else\b",
                         "opt": r"^\s*opt\b", "group": r"^\s*group\b",
                         "note over": r"note\s+over",
                         "activation": r"^\s*(activate\b|deactivate\b)|\+\+|--"}),
         "prompt": PUML + "A sequence diagram with activation bars, a loop "
                          "block over 3 retries containing an alt/else on HTTP "
                          "200 vs 500, an opt block, a group block, and a note "
                          "over two participants. Use autonumber."},
        # NOTE: deliberately does NOT ask for par/and. The locally installed
        # PlantUML 1.2026.7 REJECTS `par ... and ... end` (exit 200) while
        # accepting `par ... end`, `group`, `opt` and `break`. That is a real
        # property of the verifier on this machine, not a model failure - but a
        # task no candidate can pass measures exactly as little as one every
        # candidate passes, so it is not asked for here. Recorded in
        # docs/backlog.md §D1.
        {"id": "hpuml-deploy", "cat": "puml",
         "verifier": puml_content(
             require_re={"artifact": r"^\s*artifact\b", "database": r"^\s*database\b",
                         "cloud": r"^\s*cloud\b", "skinparam": r"skinparam\s+\w*"
                                                              r"componentBackgroundColor"},
             min_counts={"node blocks": (r"^\s*node\b", 2)}),
         "prompt": PUML + "A deployment diagram with two nested node blocks "
                          "(a 'Region' node containing an 'AZ' node), an "
                          "artifact and a database inside, a cloud element, and "
                          "a skinparam changing componentBackgroundColor."},

        {"id": "hdio-geom", "cat": "drawio",
         "verifier": DrawioVerifier(min_vertices=5, min_edges=4,
                                    exact_vertices=5,
                                    geometry_on_vertices=True,
                                    edge_style_re=r"edgeStyle=orthogonalEdgeStyle",
                                    edge_relative=True,
                                    edges_connect_vertices=True,
                                    no_overlap=True),
         "prompt": DIO + "Exactly five vertices in a pipeline with EXPLICIT "
                         "mxGeometry x/y/width/height on every one, and their "
                         "boxes must NOT overlap each other, plus four edges "
                         "each connecting two of those vertices by id, each "
                         "with an mxGeometry relative=1 and a style containing "
                         "edgeStyle=orthogonalEdgeStyle."},

        {"id": "hdio-swimlane", "cat": "drawio",
         "verifier": DrawioVerifier(min_vertices=6, min_edges=1,
                                    require_nesting=True),
         "prompt": DIO + "Two horizontal swimlanes (style containing "
                         "'swimlane') labelled 'Client' and 'Server', each "
                         "containing two child vertices whose parent attribute "
                         "is the lane's id, and one edge crossing between "
                         "lanes."},


        {"id": "hpy-intervals", "cat": "pycode",
         "verifier": pycode_verifier(
             "assert merge_intervals([[1,3],[2,6],[8,10],[15,18]])"
             "==[[1,6],[8,10],[15,18]];"
             "assert merge_intervals([])==[];"
             "assert merge_intervals([[1,4],[4,5]])==[[1,5]]"),
         "prompt": PY + "Write `merge_intervals(xs)` merging overlapping "
                        "intervals, returning them sorted by start. Touching "
                        "intervals like [1,4],[4,5] merge."},
        {"id": "hpy-lru", "cat": "pycode",
         "verifier": pycode_verifier(
             "c=LRUCache(2);c.put(1,1);c.put(2,2);assert c.get(1)==1;"
             "c.put(3,3);assert c.get(2)==-1;c.put(4,4);assert c.get(1)==-1;"
             "assert c.get(3)==3 and c.get(4)==4"),
         "prompt": PY + "Write class `LRUCache` with __init__(capacity), "
                        "get(key) returning -1 if absent, and put(key,value), "
                        "evicting least-recently-used at capacity. get counts "
                        "as a use."},
        {"id": "hpy-wordbreak", "cat": "pycode",
         "verifier": pycode_verifier(
             "assert word_break('leetcode',['leet','code']) is True;"
             "assert word_break('catsandog',['cats','dog','sand','and','cat'])"
             " is False;assert word_break('',[]) is True"),
         "prompt": PY + "Write `word_break(s, words)` returning True if s can "
                        "be segmented into a space-separated sequence of one or "
                        "more dictionary words. Words may be reused."},
        {"id": "hpy-stdin", "cat": "pycode",
         "verifier": pycode_verifier(
             "import io,sys;_o=sys.stdout;sys.stdin=io.StringIO('3\\n1 2 3\\n');"
             "sys.stdout=io.StringIO();main();_v=sys.stdout.getvalue();"
             "sys.stdout=_o;assert _v.strip()=='6', repr(_v)"),
         "prompt": PY + "Write a program with a function `main()` that reads "
                        "from stdin: first line an integer N, second line N "
                        "space-separated integers. Print their sum to stdout "
                        "and nothing else. Define main() but do not call it at "
                        "import time."},

        # ---- tool-call adherence ----
        {"id": "tool-edit", "cat": "toolcall",
         "tools": [EDIT_TOOL],
         "verifier": ToolCallVerifier("edit_file",
                                      ["path", "old_string", "new_string"]),
         "prompt": "In /srv/app/config.py the line `DEBUG = True` must become "
                   "`DEBUG = False`. Use the edit_file tool. Do not explain."},
        {"id": "tool-search", "cat": "toolcall",
         "tools": [SEARCH_TOOL],
         "verifier": ToolCallVerifier("search_code", ["pattern", "glob"]),
         "prompt": "Find every TODO comment in Python files. Use the "
                   "search_code tool. Do not explain."},
        {"id": "htool-nested", "cat": "toolcall",
         "tools": [SEARCH_TOOL, EDIT_TOOL],
         "verifier": ToolCallVerifier("search_code", ["pattern", "glob"]),
         "prompt": "Two tools are available. Before any edit can be made, every "
                   "deprecated call site must be located. Find calls to "
                   "`legacy_fetch(` in Python files under src/. Call the right "
                   "tool. Do not explain."},
        {"id": "htool-escape", "cat": "toolcall",
         "tools": [EDIT_TOOL],
         "verifier": ToolCallVerifier("edit_file",
                                      ["path", "old_string", "new_string"]),
         "prompt": "In app/main.py replace the line "
                   "`msg = \"hello \\\"world\\\"\"` with "
                   "`msg = f\"hello {name}\"`. The strings contain quotes and "
                   "backslashes that must survive exactly. Use edit_file. Do "
                   "not explain."},

        {"id": "tool-edit2", "cat": "toolcall",
         "tools": [EDIT_TOOL],
         "verifier": ToolCallVerifier("edit_file",
                                      ["path", "old_string", "new_string"]),
         "prompt": "In README.md, the heading '## Setup' should read "
                   "'## Installation'. Use the edit_file tool. Do not explain."},

        # ---- schema adherence: values, not just keys ----
        # ToolCallVerifier used to check only that required keys existed, so a
        # call with task="run the build", timeout_s="thirty" passed. These grade
        # enum membership and JSON types, which is what decides whether the
        # caller can actually execute the call.
        {"id": "hschema-enum", "cat": "toolcall",
         "tools": [RUN_TOOL],
         "verifier": ToolCallVerifier(
             "run_task", ["task", "timeout_s", "fail_fast"],
             arg_types={"timeout_s": int, "fail_fast": bool, "task": str},
             enums={"task": ["build", "test", "lint", "typecheck"]}),
         "prompt": "Run the type checker on this workspace. Give it ten minutes "
                   "and do not stop at the first error. Use run_task. Do not "
                   "explain."},
        {"id": "hschema-nested", "cat": "toolcall",
         "tools": [RUN_TOOL],
         "verifier": ToolCallVerifier(
             "run_task", ["task", "timeout_s", "fail_fast", "env"],
             arg_types={"timeout_s": int, "fail_fast": bool, "env": dict},
             enums={"task": ["build", "test", "lint", "typecheck"]}),
         "prompt": "Run the test suite with a two minute limit, stopping at the "
                   "first failure, with the environment variable CI set to the "
                   "string 'true'. Use run_task. Do not explain."},
        {"id": "hschema-semver", "cat": "toolcall",
         "tools": [DEPLOY_TOOL],
         "verifier": ToolCallVerifier(
             "deploy", ["environment", "version", "replicas"],
             arg_types={"replicas": int},
             enums={"environment": ["dev", "staging", "production"]},
             arg_re={"version": r"^\d+\.\d+\.\d+$"}),
         "prompt": "Ship release one point four point two to the production "
                   "environment with three replicas. Use deploy. Do not "
                   "explain."},

        # ---- multi-turn: does it USE what the tool returned? ----
        # The literals the verifier looks for (the exact path, the exact line)
        # appear ONLY in the injected tool result, never in the prompt, so a
        # model that ignores the tool output cannot guess its way to a pass.
        # This is the agentic axis the single-turn tasks above do not reach.
        {"id": "chain-edit", "cat": "toolchain",
         "tools": [SEARCH_TOOL, EDIT_TOOL],
         "first_call": {"name": "search_code",
                        "arguments": '{"pattern":"TIMEOUT","glob":"**/*.py"}'},
         "tool_result": json.dumps({"matches": [
             {"path": "src/net/pool_driver.py", "line": 118,
              "text": "TIMEOUT = 30  # seconds"}]}),
         "verifier": ToolChainVerifier(
             "edit_file", ["path", "old_string", "new_string"],
             derived={"path": "src/net/pool_driver.py",
                      "old_string": "TIMEOUT = 30"}),
         "prompt": "The connection timeout constant needs to be 60 instead of "
                   "30. Find it, then make the change. Do not explain."},
        {"id": "chain-path", "cat": "toolchain",
         "tools": [SEARCH_TOOL, EDIT_TOOL],
         "first_call": {"name": "search_code",
                        "arguments": '{"pattern":"legacy_fetch","glob":"**/*.py"}'},
         "tool_result": json.dumps({"matches": [
             {"path": "lib/adapters/zeta_legacy.py", "line": 42,
              "text": "result = legacy_fetch(url, retries=3)"}]}),
         "verifier": ToolChainVerifier(
             "edit_file", ["path", "old_string", "new_string"],
             derived={"path": "lib/adapters/zeta_legacy.py",
                      "old_string": "legacy_fetch"}),
         "prompt": "Replace the deprecated legacy_fetch call with fetch. Locate "
                   "it first, then edit it. Do not explain."},
        {"id": "chain-noresult", "cat": "toolchain",
         "tools": [SEARCH_TOOL, EDIT_TOOL],
         "first_call": {"name": "search_code",
                        "arguments": '{"pattern":"DEPRECATED","glob":"**/*.py"}'},
         "tool_result": json.dumps({"matches": []}),
         "verifier": ToolChainVerifier(
             "search_code", ["pattern", "glob"], derived={}),
         "prompt": "Remove every function marked DEPRECATED. Search for them "
                   "first. If the search finds nothing, widen it and search "
                   "again rather than editing anything. Do not explain."},

        # ---- BRUTAL tier ------------------------------------------------
        # Calibration on 2026-09-17: with only the tiers above, the shipped 27B
        # scored 32/33. A reference model at 97% leaves no headroom to rank
        # anything, which is the same failure this file has now hit twice. These
        # target failure modes actually OBSERVED in this repo rather than
        # "harder versions of the same thing":
        #   - negative constraints (forbid_re was written and unused)
        #   - a tool result that CONTRADICTS the prompt's premise
        #   - a tool result with distractors, where one match is correct
        #   - exact-contract code where the obvious implementation is wrong

        {"id": "bpy-nosort", "cat": "pycode",
         "verifier": pycode_verifier(
             "assert kth_smallest([7,10,4,3,20,15],3)==7;"
             "assert kth_smallest([2,1],1)==1;assert kth_smallest([5],1)==5",
             forbid_re={"sorted()": r"\bsorted\s*\(", "list.sort": r"\.sort\s*\(",
                        "heapq": r"\bimport\s+heapq|\bheapq\.",
                        "Counter": r"\bCounter\s*\("}),
         "prompt": PY + "Write `kth_smallest(xs, k)` returning the k-th "
                        "smallest value (k is 1-based). You MUST NOT use "
                        "sorted(), .sort(), heapq, or collections.Counter — "
                        "implement the selection yourself."},
        {"id": "bpy-stable", "cat": "pycode",
         "verifier": pycode_verifier(
             "r=rank_by_score([('a',5),('b',7),('c',5),('d',7)]);"
             "assert r==[('b',1),('d',1),('a',3),('c',3)], r"),
         "prompt": PY + "Write `rank_by_score(pairs)` taking (name, score) "
                        "pairs and returning (name, rank) sorted by score "
                        "descending. Ties share the lowest rank and COMPETITION "
                        "ranking is used, so two tied at rank 1 are followed by "
                        "rank 3, not 2. Within a tie keep the original input "
                        "order."},
        {"id": "bpy-lazy", "cat": "pycode",
         "verifier": pycode_verifier(
             "import inspect;g=windows(iter([1,2,3,4]),2);"
             "assert inspect.isgenerator(g) or hasattr(g,'__next__');"
             "assert next(g)==[1,2];assert next(g)==[2,3];"
             "assert list(windows(iter([1,2,3,4]),2))[-1]==[3,4];"
             "assert list(windows(iter([1]),2))==[]"),
         "prompt": PY + "Write `windows(it, n)` yielding successive overlapping "
                        "windows of length n from an ITERATOR as lists. It must "
                        "be lazy — do not materialise the input — and must "
                        "yield nothing if the iterator is shorter than n."},

        {"id": "bpuml-forbid", "cat": "puml",
         "verifier": puml_content(
             require_re={"start": r"@startuml", "participants": r"participant"},
             min_counts={"messages": (r"->", 3)},
             forbid_re={"autonumber": r"autonumber", "colour": r"#[0-9A-Fa-f]{6}",
                        "skinparam": r"skinparam"}),
         "prompt": PUML + "A sequence diagram of a login flow with three "
                          "declared participants and at least three messages. "
                          "Constraints: do NOT use autonumber, do NOT set any "
                          "skinparam, and do NOT use any hex colour codes."},

        {"id": "bchain-distractor", "cat": "toolchain",
         "tools": [SEARCH_TOOL, EDIT_TOOL],
         "first_call": {"name": "search_code",
                        "arguments": '{"pattern":"MAX_RETRIES","glob":"**/*.py"}'},
         "tool_result": json.dumps({"matches": [
             {"path": "tests/fixtures/legacy_conf.py", "line": 8,
              "text": "MAX_RETRIES = 5  # fixture, do not edit"},
             {"path": "src/core/retry_policy.py", "line": 23,
              "text": "MAX_RETRIES = 5"},
             {"path": "docs/tuning.md", "line": 61,
              "text": "MAX_RETRIES = 5"}]}),
         "verifier": ToolChainVerifier(
             "edit_file", ["path", "old_string", "new_string"],
             derived={"path": "src/core/retry_policy.py"}),
         "prompt": "Raise the retry limit from 5 to 8 in the application source. "
                   "Do not touch test fixtures or documentation. Find it first, "
                   "then edit exactly the right file. Do not explain."},
        {"id": "bchain-contradict", "cat": "toolchain",
         "tools": [SEARCH_TOOL, EDIT_TOOL],
         "first_call": {"name": "search_code",
                        "arguments": '{"pattern":"use_tls","glob":"**/*.py"}'},
         "tool_result": json.dumps({"matches": [
             {"path": "src/net/client.py", "line": 12,
              "text": "use_tls = True  # already enabled"}]}),
         # The premise is false: it is already True. Editing it to True anyway is
         # the failure mode, so the pass condition is "do not call edit_file".
         "verifier": ToolChainVerifier(
             "search_code", ["pattern", "glob"], derived={}),
         "prompt": "TLS is disabled and must be turned on. Search for the "
                   "setting, then act. If the search shows it is ALREADY "
                   "enabled, do not edit anything — search for a different "
                   "setting name instead. Do not explain."},

        # ---- CEILING tier -----------------------------------------------
        # Added 2026-09-17. With everything above, coder and fast both scored
        # 100% on puml, drawio and toolchain, so three of five categories
        # carried no signal and the set could not resolve the ~5 points the
        # candidate models actually differ by. These are built to be failed:
        # each requires combining a tool result with arithmetic, honouring
        # several constraints at once, or noticing that the obvious action is
        # the wrong one.

        {"id": "cpuml-multi", "cat": "puml",
         "verifier": puml_content(
             require_re={"title": r"^\s*title\s+\S", "legend": r"^\s*legend",
                         "endlegend": r"^\s*endlegend",
                         "footer": r"^\s*(footer|header)\s+\S",
                         "newpage-free": r"@enduml"},
             min_counts={"participants": (r"^\s*participant\b", 4),
                         "notes": (r"^\s*note\s+(left|right|over)", 2),
                         "refs": (r"^\s*ref\s+over", 1)},
             forbid_re={"autonumber": r"autonumber", "skinparam": r"skinparam"}),
         "prompt": PUML + "A sequence diagram with a title line, exactly four "
                          "declared participants, at least two notes, one "
                          "`ref over` block, and a legend...endlegend section "
                          "plus a footer. Do NOT use autonumber and do NOT use "
                          "any skinparam."},
        {"id": "cpuml-json", "cat": "puml",
         "verifier": puml_content(
             require_re={"startjson": r"@startjson", "endjson": r"@endjson"},
             forbid_re={"startuml": r"@startuml"}),
         "prompt": "Return ONLY a fenced code block containing a PlantUML JSON "
                   "diagram (@startjson/@endjson, not @startuml) rendering an "
                   "object with keys name, version and a nested array deps of "
                   "three strings. No prose."},

        {"id": "cdio-grid", "cat": "drawio",
         "verifier": DrawioVerifier(min_vertices=6, exact_vertices=6, min_edges=5,
                                    geometry_on_vertices=True,
                                    edges_connect_vertices=True,
                                    no_overlap=True,
                                    require_labels=["A", "B", "C", "D", "E", "F"]),
         "prompt": DIO + "Exactly six vertices labelled A B C D E F laid out as "
                         "a 3x2 grid with explicit non-overlapping mxGeometry, "
                         "and exactly five edges forming a chain A->B->C->D->E->F "
                         "where every edge names real source and target ids."},

        {"id": "cchain-compute", "cat": "toolchain",
         "tools": [SEARCH_TOOL, EDIT_TOOL],
         "first_call": {"name": "search_code",
                        "arguments": '{"pattern":"POOL_SIZE","glob":"**/*.py"}'},
         "tool_result": json.dumps({"matches": [
             {"path": "src/db/pool.py", "line": 31,
              "text": "POOL_SIZE = 12"}]}),
         # The new value is not in the prompt or the result; it has to be
         # computed from the result (12 * 3 = 36). A model that pattern-matches
         # the tool result without reading it will write 12, or echo "triple".
         "verifier": ToolChainVerifier(
             "edit_file", ["path", "old_string", "new_string"],
             derived={"path": "src/db/pool.py", "new_string": "36"}),
         "prompt": "Triple the database pool size. Find the current value "
                   "first, then write the new one explicitly as a number. Do "
                   "not explain."},
        {"id": "cchain-pick", "cat": "toolchain",
         "tools": [SEARCH_TOOL, EDIT_TOOL],
         "first_call": {"name": "search_code",
                        "arguments": '{"pattern":"def handler","glob":"**/*.py"}'},
         "tool_result": json.dumps({"matches": [
             {"path": "src/api/v1/routes.py", "line": 10,
              "text": "def handler(req):  # deprecated in v1"},
             {"path": "src/api/v2/routes.py", "line": 14,
              "text": "def handler(req):"},
             {"path": "src/api/v3/routes.py", "line": 9,
              "text": "def handler(req):  # experimental, do not modify"}]}),
         "verifier": ToolChainVerifier(
             "edit_file", ["path", "old_string", "new_string"],
             derived={"path": "src/api/v2/routes.py"}),
         "prompt": "Rename the request handler to `handle_request` in the "
                   "current supported API version. v1 is deprecated and v3 is "
                   "experimental and must not be modified. Search first, then "
                   "edit the one correct file. Do not explain."},
    ]





# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------

def complete(model: str, prompt: str, tools=None, max_tokens=2048, timeout=1800.0,
             messages=None):
    body = {"model": model, "max_tokens": max_tokens,
            "messages": messages or [{"role": "user", "content": prompt}]}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    req = urllib.request.Request(
        f"{PROXY}/v1/chat/completions", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {_key()}"})
    t0 = time.perf_counter()
    try:
        d = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    except urllib.error.HTTPError as e:
        return None, {"http_error": e.code,
                      "detail": e.read().decode(errors="replace")[:200]}, \
               time.perf_counter() - t0
    return d["choices"][0]["message"], (d.get("usage") or {}), \
        time.perf_counter() - t0


def chain_messages(task: dict) -> list[dict]:
    """Build the second turn of a tool conversation.

    The model's own first call is SCRIPTED rather than elicited. That is
    deliberate: eliciting it would conflate two abilities -- picking the right
    first tool, which the single-turn tasks already measure -- with using the
    result, which nothing currently measures. Scripting the first call isolates
    the second. It also makes the task deterministic, so a model is not graded
    against a tool result that depends on what it happened to ask for.
    """
    call_id = "call_0"
    fc = task["first_call"]
    return [
        {"role": "user", "content": task["prompt"]},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": call_id, "type": "function", "function": fc}]},
        {"role": "tool", "tool_call_id": call_id, "name": fc["name"],
         "content": task["tool_result"]},
    ]



def cmd_run(args) -> int:
    tasks = build_tasks()
    if args.only:
        tasks = [t for t in tasks if t["cat"] in args.only or t["id"] in args.only]
    print(f"model={args.model}  tasks={len(tasks)}  repeats={args.repeats}\n")

    rows = []
    for t in tasks:
        for rep in range(args.repeats):
            msgs = chain_messages(t) if "first_call" in t else None
            msg, usage, wall = complete(args.model, t["prompt"], t.get("tools"),
                                        args.max_tokens, messages=msgs)
            if msg is None:
                v = Verdict.inconclusive("http_error", str(usage)[:160])
                toks = 0
            else:
                v = (t["verifier"].verify_response(msg)
                     if hasattr(t["verifier"], "verify_response")
                     else t["verifier"].verify(msg.get("content") or ""))
                toks = usage.get("completion_tokens", 0)
            rows.append({"id": t["id"], "cat": t["cat"], "rep": rep,
                         "ok": v.ok, "ran": v.ran, "reason": v.reason,
                         "detail": v.detail[:200], "completion_tokens": toks,
                         "wall_s": round(wall, 2)})
            flag = "PASS" if v.ok else ("FAIL" if v.ran else "INCONC")
            print(f"  {t['id']:14s} {t['cat']:9s} {flag:6s} {wall:6.1f}s "
                  f"{toks:>5} tok  {v.reason[:44]}")

    by_cat: dict[str, list] = {}
    for r in rows:
        by_cat.setdefault(r["cat"], []).append(r)
    print(f"\n  {'category':10s} {'pass':>8}  {'rate':>6}  {'tok/pass':>9}")
    for cat, rs in sorted(by_cat.items()):
        n_ok = sum(r["ok"] for r in rs)
        toks = [r["completion_tokens"] for r in rs if r["ok"]]
        tp = f"{statistics.median(toks):.0f}" if toks else "-"
        print(f"  {cat:10s} {n_ok:>4}/{len(rs):<3} {n_ok/len(rs):>6.0%}  {tp:>9}")
    total = sum(r["ok"] for r in rows)
    print(f"  {'TOTAL':10s} {total:>4}/{len(rows):<3} {total/len(rows):>6.0%}")

    out = {"label": args.label, "model": args.model,
           "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "repeats": args.repeats, "rows": rows}
    RESULTS.mkdir(exist_ok=True)
    p = RESULTS / f"task_{args.label}.json"
    p.write_text(json.dumps(out, indent=2))
    print(f"\nsaved -> {p.relative_to(ROOT)}")
    return 0


def cmd_compare(args) -> int:
    """McNemar per category and pooled, matching bench/paired_compare.py."""
    from math import comb

    def load(lbl):
        return json.loads((RESULTS / f"task_{lbl}.json").read_text())

    A, B = load(args.a), load(args.b)
    # Collapse repeats to "passed at least once" so a flip means a real
    # capability difference rather than one unlucky sample.
    #
    # INCONCLUSIVE IS NOT FAILURE. A verdict with ran=False means the verifier
    # could not judge -- overwhelmingly a PlantUML `!include` the CDN would not
    # serve. Counting it as ok=False would score a model down for GitHub being
    # unreachable, and on 2026-09-17 that would have cost gemma four puml tasks
    # in a comparison it was otherwise level on. A task that was inconclusive
    # for EITHER arm is dropped from the pairing and reported, because a pair is
    # only meaningful when both halves were actually judged.
    def collapse(d):
        acc: dict[str, dict] = {}
        for r in d["rows"]:
            e = acc.setdefault(r["id"], {"cat": r["cat"], "ok": False,
                                         "judged": False, "toks": []})
            if r.get("ran", True):
                e["judged"] = True
                e["ok"] |= r["ok"]
            if r["ok"]:
                e["toks"].append(r["completion_tokens"])
        return acc

    a, b = collapse(A), collapse(B)
    both = set(a) & set(b)
    dropped = sorted(i for i in both if not (a[i]["judged"] and b[i]["judged"]))
    shared = sorted(i for i in both if i not in dropped)
    print(f"== task-shaped comparison: A={args.a} ({A['model']})  "
          f"B={args.b} ({B['model']})")
    print(f"   {len(shared)} shared tasks"
          + (f", {len(dropped)} dropped as inconclusive: "
             f"{', '.join(dropped[:4])}" if dropped else ""))
    print()

    def mcnemar(n01, n10):
        n = n01 + n10
        if n == 0:
            return 1.0
        k = min(n01, n10)
        tail = sum(comb(n, i) for i in range(k + 1)) / (2 ** n)
        return min(1.0, 2 * tail)

    cats = sorted({a[i]["cat"] for i in shared})
    print(f"  {'category':10s} {'A':>5} {'B':>5} {'A only':>7} {'B only':>7} "
          f"{'p':>8}")
    tot01 = tot10 = 0
    for c in cats:
        ids = [i for i in shared if a[i]["cat"] == c]
        n01 = sum(1 for i in ids if a[i]["ok"] and not b[i]["ok"])
        n10 = sum(1 for i in ids if b[i]["ok"] and not a[i]["ok"])
        tot01 += n01
        tot10 += n10
        pa = sum(a[i]["ok"] for i in ids) / len(ids)
        pb = sum(b[i]["ok"] for i in ids) / len(ids)
        print(f"  {c:10s} {pa:>5.0%} {pb:>5.0%} {n01:>7} {n10:>7} "
              f"{mcnemar(n01, n10):>8.4f}")

    p = mcnemar(tot01, tot10)
    print(f"\n  pooled over {len(shared)} tasks: {tot01} flips to A, "
          f"{tot10} to B")
    print(f"  pooled McNemar p = {p:.5f}")
    if p < 0.05:
        print(f"  verdict: {'A' if tot01 > tot10 else 'B'} is better (p={p:.5f})")
    else:
        lead = "A" if tot01 > tot10 else ("B" if tot10 > tot01 else "neither")
        print(f"  verdict: no significant difference (p={p:.5f}); "
              f"{lead} leads but within chance")

    # CAPABILITY vs RELIABILITY. The comparison above collapses repeats to
    # "passed at least once", which asks whether the model CAN do the task. That
    # is the right question for a capability claim and the wrong one for daily
    # use, where a model that succeeds one try in two is materially worse. Both
    # are reported, because on this task set they disagree.
    def attempts(d):
        n = len(d["rows"])
        return sum(r["ok"] for r in d["rows"]), n

    oka, na = attempts(A)
    okb, nb = attempts(B)
    print(f"\n  capability  (passed >=1 of {A['repeats']}):  "
          f"A {sum(a[i]['ok'] for i in shared)}/{len(shared)}   "
          f"B {sum(b[i]['ok'] for i in shared)}/{len(shared)}")
    print(f"  reliability (every attempt)      :  "
          f"A {oka}/{na} = {oka/na:.0%}   B {okb}/{nb} = {okb/nb:.0%}")

    # PER-ATTEMPT PAIRING. The capability collapse above asks "can it ever do
    # this", which saturates the moment both models can: on 2026-09-17 it
    # reported 42/42 vs 42/42 and zero discordant pairs while the 8-bit had two
    # genuine failures gemma did not have, because the 8-bit passed each of them
    # on its other repeat. Pairing attempt-by-attempt keeps that signal and
    # doubles the sample at repeats=2. It is a reliability claim, not a
    # capability one, so it is reported alongside rather than instead.
    ra = {(r["id"], r["rep"]): r for r in A["rows"]}
    rb = {(r["id"], r["rep"]): r for r in B["rows"]}
    keys = [k for k in ra if k in rb
            and ra[k].get("ran", True) and rb[k].get("ran", True)]
    a01 = sum(1 for k in keys if ra[k]["ok"] and not rb[k]["ok"])
    a10 = sum(1 for k in keys if rb[k]["ok"] and not ra[k]["ok"])
    pa = mcnemar(a01, a10)
    print(f"\n  per-attempt pairing over {len(keys)} judged attempts: "
          f"{a01} to A, {a10} to B")
    print(f"  per-attempt McNemar p = {pa:.5f}"
          + ("" if a01 or a10 else "  (no attempt differed either way)"))

    # Power, stated rather than left for the reader to be misled by. McNemar on
    # a handful of discordant pairs cannot resolve a few points, exactly as §8
    # found for 500-sample public suites.
    #
    # Derived from the run rather than hardcoded. The figure was "roughly 15
    # points" while the set was 27 tasks; changing the tasks without changing
    # the caveat would have quietly made the caveat wrong. McNemar needs about 6
    # discordant pairs all falling one way to clear p<0.05 (2*(1/2)^6 = 0.031),
    # so that is the smallest detectable effect expressed over the task count.
    disc = tot01 + tot10
    min_detect = 6 / len(shared) if shared else 1.0
    print(f"\n  discordant pairs: {disc}. A difference of "
          f"{abs(oka/na - okb/nb):.0%} on {len(shared)} tasks needs about 6 "
          f"one-way\n  flips to reach p<0.05, i.e. roughly "
          f"{min_detect:.0%} of the set. Treat anything smaller as\n  "
          f"unmeasured rather than as absent.")
    if disc == 0:
        print("  NOTE: zero discordant pairs means the TASKS did not "
              "discriminate,\n  not that the models are equivalent. Check the "
              "per-category rates above:\n  a category at 100% for both arms "
              "is carrying no signal at all.")

    # tokens-to-result: the D1 metric that is not a pass rate.
    ta = [t for i in shared for t in a[i]["toks"]]
    tb = [t for i in shared for t in b[i]["toks"]]
    if ta and tb:
        print(f"\n  median completion tokens on PASSING tasks: "
              f"A {statistics.median(ta):.0f}   B {statistics.median(tb):.0f}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="score one model over the task set")
    r.add_argument("--model", required=True, help="role alias or served model id")
    r.add_argument("--label", required=True)
    r.add_argument("--repeats", type=int, default=1)
    r.add_argument("--max-tokens", type=int, default=2048)
    r.add_argument("--only", nargs="+", help="restrict to categories or ids")
    r.set_defaults(func=cmd_run)

    c = sub.add_parser("compare", help="paired McNemar between two runs")
    c.add_argument("a")
    c.add_argument("b")
    c.set_defaults(func=cmd_compare)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
