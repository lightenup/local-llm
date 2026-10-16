#!/usr/bin/env python3
"""Categorise livecodebench failures instead of counting them.

FINDINGS section 13 recorded Coder-Next 16.6 points below the 27B on
livecodebench, p=0.0, and could not say what the failures *were*. That mattered
because the suite does not check reasoning about code, it executes the code: it
extracts a block from the response, runs it in a subprocess against the first
three public test cases, and compares stdout exactly. A model can therefore lose
points for writing a LeetCode-style function instead of a stdin/stdout script,
for guessing the wrong input convention, for printing "true" where the case
expects "True", or for being slow -- none of which is being worse at the
algorithm.

The audit needs no GPU and no re-run. oMLX truncates `predicted` to 200
characters but stores the whole generation in `raw_response`, so every answer is
already on disk. This replays oMLX's own extraction and execution against its own
bundled dataset and sorts each failure into a bucket.

    python3 bench/lcb_audit.py acc_ship_code acc_codernext_code

SECURITY: this executes model-generated code, exactly as oMLX already did when
the suite ran. Same mitigations as its harness -- subprocess, wall-clock timeout,
address-space cap, temp file cleanup, minimal env. Do not point it at results
from a model you would not already have run here.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import resource
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from omlx_client import RESULTS_DIR  # noqa: E402

# Mirrors omlx/eval/livecodebench.py. Kept as constants rather than imported
# because the app bundle is not importable from this interpreter.
OMLX_DATA = Path(
    "/Applications/oMLX.app/Contents/Resources/omlx/eval/data/livecodebench.jsonl"
)
EXEC_TIMEOUT_SECONDS = 30
EXEC_MEMORY_LIMIT_BYTES = 256 * 1024 * 1024
TEST_CASES_CHECKED = 3  # check_answer() uses item["inputs"][:3]


def extract_last_code_block(response: str) -> str:
    """Byte-for-byte the logic in omlx/eval/base.py::_extract_last_code_block."""
    response = response.strip()
    blocks = re.findall(r"```python\s*\n(.*?)```", response, re.DOTALL)
    if blocks:
        return blocks[-1].strip()
    blocks = re.findall(r"```\s*\n(.*?)```", response, re.DOTALL)
    if blocks:
        return blocks[-1].strip()
    lines = response.split("\n")
    code_lines: list[str] = []
    in_code = False
    for line in lines:
        if not in_code and (
            line.startswith("def ")
            or line.startswith("class ")
            or line.startswith("import ")
            or line.startswith("from ")
            or line.startswith("#")
        ):
            in_code = True
        if in_code:
            code_lines.append(line)
    return "\n".join(code_lines) if code_lines else response.strip()


def _limits() -> None:
    for what, cap in (
        (resource.RLIMIT_AS, EXEC_MEMORY_LIMIT_BYTES),
        (resource.RLIMIT_CPU, EXEC_TIMEOUT_SECONDS + 5),
    ):
        try:
            resource.setrlimit(what, (cap, cap))
        except (ValueError, OSError):
            pass


def run_once(code: str, stdin_input: str) -> tuple[str, bool, str]:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(code)
        path = f.name
    try:
        p = subprocess.run(
            ["python3", path],
            input=stdin_input,
            capture_output=True,
            text=True,
            timeout=EXEC_TIMEOUT_SECONDS,
            preexec_fn=_limits,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/usr/local/bin"),
                "HOME": os.environ.get("HOME", "/tmp"),
                "LANG": "en_US.UTF-8",
            },
        )
        return p.stdout, p.returncode == 0, p.stderr[:500]
    except subprocess.TimeoutExpired:
        return "", False, "Execution timed out"
    except Exception as exc:  # noqa: BLE001
        return "", False, str(exc)[:500]
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def classify(code: str, item: dict) -> tuple[str, str]:
    """Return (bucket, detail) for one answer against its test cases."""
    if not code.strip():
        return "no_code_extracted", ""

    for inp, want in zip(item["inputs"][:TEST_CASES_CHECKED],
                         item["outputs"][:TEST_CASES_CHECKED]):
        out, ok, err = run_once(code, inp if isinstance(inp, str) else str(inp))
        expected = (want if isinstance(want, str) else str(want)).strip()
        if not ok:
            if "timed out" in err:
                return "timeout", err
            # The exception type is the whole point of the taxonomy: an unpack
            # error is a stdin-convention mismatch, a NameError is usually a
            # bare function that was never called.
            exc = re.findall(r"^(\w*Error|\w*Exception)", err, re.M)
            kind = exc[-1] if exc else "nonzero_exit"
            if kind == "ValueError" and "unpack" in err:
                return "stdin_format_unpack", err.strip().splitlines()[-1][:160]
            if kind in ("EOFError",):
                return "stdin_format_eof", err.strip().splitlines()[-1][:160]
            return f"crash_{kind}", err.strip().splitlines()[-1][:160]

        got = out.strip()
        if got != expected:
            if not got:
                return "no_output", f"expected {expected[:40]!r}"
            if got.lower() == expected.lower():
                return "case_mismatch", f"{got[:30]!r} vs {expected[:30]!r}"
            if got.split() == expected.split():
                return "whitespace_mismatch", f"{got[:30]!r} vs {expected[:30]!r}"
            return "wrong_output", f"{got[:40]!r} vs {expected[:40]!r}"
    return "passes_now", ""


def load_dataset() -> dict[str, dict]:
    items: dict[str, dict] = {}
    if not OMLX_DATA.exists():
        raise SystemExit(f"dataset not found: {OMLX_DATA}")
    for line in OMLX_DATA.read_text().splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        tc = d.get("public_test_cases", "[]")
        if isinstance(tc, str):
            try:
                tc = json.loads(tc)
            except (json.JSONDecodeError, TypeError):
                tc = []
        if not isinstance(tc, list) or not tc:
            continue
        items[str(d.get("question_id"))] = {
            "inputs": [t.get("input", "") for t in tc],
            "outputs": [t.get("output", "") for t in tc],
        }
    return items


def suite(label: str) -> dict:
    name = label if label.startswith("acc_") else f"acc_{label}"
    data = json.loads((RESULTS_DIR / f"{name}.json").read_text())
    for r in data["results"]:
        if r["benchmark"] == "livecodebench":
            return r
    raise SystemExit(f"{name}: no livecodebench result")


def audit(label: str, ds: dict[str, dict], limit: int | None) -> dict:
    res = suite(label)
    rows = []
    failures = [q for q in res["question_results"] if not q["correct"]]
    if limit:
        failures = failures[:limit]
    print(f"\n== {label}: {len(failures)} failures to classify "
          f"(of {res['total']}, accuracy {res['accuracy']:.3f})")
    for n, q in enumerate(failures, 1):
        item = ds.get(str(q["id"]))
        if not item:
            rows.append((q["id"], "dataset_missing", ""))
            continue
        code = extract_last_code_block(q.get("raw_response") or "")
        bucket, detail = classify(code, item)
        rows.append((q["id"], bucket, detail))
        if n % 25 == 0:
            print(f"   {n}/{len(failures)}", flush=True)
    return {"label": label, "accuracy": res["accuracy"], "total": res["total"],
            "rows": rows}


def report(audits: list[dict]) -> None:
    keys = sorted({b for a in audits for _, b, _ in a["rows"]})
    width = max(len(k) for k in keys) + 2
    print(f"\n  {'bucket':<{width}}" + "".join(f"{a['label'][-14:]:>16}" for a in audits))
    print("  " + "-" * (width + 16 * len(audits)))
    counts = [Counter(b for _, b, _ in a["rows"]) for a in audits]
    for k in keys:
        print(f"  {k:<{width}}" + "".join(f"{c.get(k, 0):>16}" for c in counts))
    print(f"  {'TOTAL failures':<{width}}" + "".join(f"{sum(c.values()):>16}" for c in counts))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("labels", nargs="+", help="accuracy result labels to audit")
    ap.add_argument("--limit", type=int, help="classify only the first N failures")
    ap.add_argument("--json", action="store_true",
                    help=f"write {RESULTS_DIR / 'lcb_audit.json'}")
    args = ap.parse_args()

    ds = load_dataset()
    print(f"dataset: {len(ds)} problems with public test cases")
    audits = [audit(l, ds, args.limit) for l in args.labels]
    report(audits)

    if args.json:
        out = RESULTS_DIR / "lcb_audit.json"
        out.write_text(json.dumps(audits, indent=2) + "\n")
        print(f"\n  wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
