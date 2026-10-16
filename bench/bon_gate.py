#!/usr/bin/env python3
"""C9 — best-of-N against a verifier, as a callable gate.

This is the loop that makes the wave 1 rule reachable. §15 measured that
Coder-Next at N=4 recovers 22% of its own failures for 1.60x wall-clock, which
projects livecodebench 43.2 -> ~56.0 against the 27B's 59.8, while still running
1.32x faster than the 27B. That result is unreachable from opencode, which only
ever sends N=1. Here it is as something that can be called.

The loop:

    1. Resolve a verifier for the project. No verifier -> do not sample N.
    2. Sample N candidates from ONE prompt, concurrently. The shared prefix is
       prefilled once, which is why N=4 costs 1.60x rather than 4x (§15) - the
       opposite of §8's prefill-bound concurrency result, which used different
       prompts.
    3. Verify candidates until one passes. Return it.
    4. If none passes, escalate rather than guess.

Step 4 is the part worth arguing about. Returning "the best failing candidate"
would be picking blind, which spends N times the compute to select at random and
is strictly worse than one sample from the stronger model. So the fallback is
the ESCALATION model - the 27B - at N=1. That inverts the usual cost shape
deliberately: the fast model is tried first and the strong model catches what it
drops, rather than the strong model being the default.

    python3 bench/bon_gate.py --prompt-file p.txt --verifier-spec verifiers.json
    python3 bench/bon_gate.py --demo            # end-to-end against the proxy
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verifier import NullVerifier, Verdict, find_spec, load_verifier  # noqa: E402

PROXY = os.environ.get("PROXY_BASE", "http://127.0.0.1:4000")
ENV_FILE = Path(__file__).resolve().parent.parent / "proxy" / ".env"
RESULTS = Path(__file__).resolve().parent.parent / "results"

# The two roles this gate arbitrates between, addressed through C3's aliases
# rather than by model id - so when the lineup moves, this file does not.
SAMPLER = os.environ.get("BON_SAMPLER", "fast")       # cheap, sampled N ways
ESCALATION = os.environ.get("BON_ESCALATION", "coder")  # strong, single shot


def _key() -> str:
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if line.startswith("LITELLM_MASTER_KEY="):
                return line.split("=", 1)[1].strip()
    return os.environ.get("LITELLM_MASTER_KEY", "")


def _complete(model: str, messages: list[dict], temperature: float,
              max_tokens: int, timeout: float = 1800.0) -> tuple[str, dict]:
    import urllib.request
    body = {"model": model, "messages": messages, "temperature": temperature,
            "max_tokens": max_tokens}
    req = urllib.request.Request(
        f"{PROXY.rstrip('/')}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {_key()}"},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        payload = json.loads(r.read())
    return (payload["choices"][0]["message"].get("content") or ""), \
           (payload.get("usage") or {})


@dataclass
class GateResult:
    accepted: bool
    text: str
    source: str            # "sampled" | "escalated" | "none"
    model: str
    n_sampled: int
    n_verified: int        # how many were actually checked before stopping
    verdicts: list
    wall_s: float
    escalated: bool
    why: str


def run_gate(prompt: str, *, n: int = 4, verifier=None, sampler: str = SAMPLER,
             escalation: str = ESCALATION, temperature: float = 0.7,
             max_tokens: int = 4096, system: str | None = None,
             verbose: bool = True) -> GateResult:
    verifier = verifier or NullVerifier()
    messages = ([{"role": "system", "content": system}] if system else []) + \
               [{"role": "user", "content": prompt}]
    t0 = time.perf_counter()

    # --- no verifier: do not sample N at all -----------------------------
    # Sampling without a way to select is pure waste. Go straight to the model
    # that is better at N=1 and say so.
    if isinstance(verifier, NullVerifier):
        if verbose:
            print(f"  no verifier configured -> single shot on {escalation!r} "
                  f"(sampling N would select at random)")
        text, _ = _complete(escalation, messages, 0.0, max_tokens)
        return GateResult(
            accepted=False, text=text, source="escalated", model=escalation,
            n_sampled=1, n_verified=0, verdicts=[],
            wall_s=round(time.perf_counter() - t0, 2), escalated=True,
            why="no verifier: best-of-N cannot select, fell back to N=1")

    # --- sample N off one prompt, verifying AS THEY LAND ------------------
    # The naive shape is pool.map(...) then verify the list, and it is wrong.
    # Wall-clock to all-N-complete is set by the SLOWEST candidate, and at
    # temperature 0.7 output length varies enough that the straggler dominates:
    # measured 2.40x for N=4 at equal token budget against 4.84x when
    # generations run free (results/bon_decode_scaling.json,
    # results/bon_warm_cost.json). Verifying on arrival and returning on the
    # first pass collects the MEDIAN candidate's latency instead of the MAX.
    #
    # Honest limit: this improves time-to-answer, not GPU cost. The abandoned
    # requests keep decoding to completion on the server, so the machine still
    # pays for N. `max_tokens` is the only real lever on that, which is why the
    # straggler is worth capping rather than just outrunning.
    if verbose:
        print(f"  sampling N={n} from {sampler!r} at temperature {temperature}")

    def one(i):
        text, usage = _complete(sampler, messages, temperature, max_tokens)
        return i, text, usage

    ts = time.perf_counter()
    verdicts, checked = [], 0
    winner = None
    pool = ThreadPoolExecutor(max_workers=n)
    try:
        futures = [pool.submit(one, i) for i in range(n)]
        for fut in as_completed(futures):
            i, text, _usage = fut.result()
            v = verifier.verify(text)
            checked += 1
            verdicts.append({"index": i, **asdict(v)})
            if verbose:
                print(f"    candidate {i}: {v}  "
                      f"(+{time.perf_counter() - ts:.1f}s, "
                      f"verify {v.duration_s * 1000:.0f} ms)")
            if v.ok:
                winner = (i, text, v)
                break
    finally:
        # Do not block on the stragglers we no longer need.
        pool.shutdown(wait=False, cancel_futures=True)

    if winner is not None:
        i, text, v = winner
        return GateResult(
            accepted=True, text=text, source="sampled", model=sampler,
            n_sampled=n, n_verified=checked, verdicts=verdicts,
            wall_s=round(time.perf_counter() - t0, 2), escalated=False,
            why=f"candidate {i} passed {v.reason} after {checked} of {n} checked")

    # --- nothing passed: escalate, do not guess --------------------------
    inconclusive = sum(1 for v in verdicts if not v["ran"])
    if inconclusive == len(verdicts):
        why = ("every candidate was INCONCLUSIVE - the verifier never ran, so "
               "this is a configuration fault, not a model result")
    else:
        why = f"0/{n} candidates passed; escalating rather than picking blind"
    if verbose:
        print(f"  {why} -> {escalation!r}")

    text, _ = _complete(escalation, messages, 0.0, max_tokens)
    v = verifier.verify(text)
    verdicts.append({"index": "escalation", **asdict(v)})
    if verbose:
        print(f"    escalation: {v}")
    return GateResult(
        accepted=v.ok, text=text, source="escalated", model=escalation,
        n_sampled=n + 1, n_verified=checked + 1, verdicts=verdicts,
        wall_s=round(time.perf_counter() - t0, 2), escalated=True, why=why)


DEMO_PROMPT = (
    "Write a Python function `merge_sorted(a, b)` that merges two sorted lists "
    "into one sorted list. Return ONLY a fenced ```python code block, no prose. "
    "Include a docstring."
)
DEMO_SPEC = {
    "default": "tests",
    "verifiers": {
        "tests": {"chain": ["python-syntax", "behaviour"]},
        "behaviour": {
            # Appends a real assertion to the candidate and runs it. This is the
            # shape a project's own verifier takes: a command that exits 0.
            "command": "python3 -c "
                       "\"exec(open('{file}').read());"
                       "assert merge_sorted([1,3,5],[2,4,6])==[1,2,3,4,5,6];"
                       "assert merge_sorted([],[])==[]\"",
            "extract": "code:python", "suffix": ".py", "timeout_s": 30,
        },
    },
}


def cmd_demo(args) -> int:
    import tempfile
    print("=== C9 gate, end-to-end through the proxy ===\n")
    with tempfile.TemporaryDirectory() as d:
        spec = Path(d) / "verifiers.json"
        spec.write_text(json.dumps(DEMO_SPEC))

        print("[1] with a verifier -- best-of-N should select")
        v = load_verifier(spec)
        r1 = run_gate(DEMO_PROMPT, n=args.n, verifier=v)
        print(f"  -> accepted={r1.accepted} source={r1.source} "
              f"verified {r1.n_verified}/{r1.n_sampled} in {r1.wall_s}s")
        print(f"     {r1.why}\n")

        print("[2] with NO verifier -- must refuse to sample N")
        r2 = run_gate(DEMO_PROMPT, n=args.n, verifier=NullVerifier())
        print(f"  -> accepted={r2.accepted} source={r2.source} "
              f"escalated={r2.escalated} in {r2.wall_s}s")
        print(f"     {r2.why}\n")

    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "bon_gate_demo.json").write_text(json.dumps(
        {"with_verifier": asdict(r1), "without_verifier": asdict(r2)}, indent=2))
    print("saved -> results/bon_gate_demo.json")
    return 0 if r1.accepted and r2.source == "escalated" else 1


COST_PROMPTS = [
    ("merge_sorted", "Write a Python function `merge_sorted(a, b)` merging two "
     "sorted lists. Return ONLY a fenced ```python block, no prose.",
     "assert merge_sorted([1,3,5],[2,4,6])==[1,2,3,4,5,6];assert merge_sorted([],[])==[]"),
    ("rle", "Write a Python function `rle(s)` returning run-length encoding of a "
     "string as a list of (char, count) tuples. Return ONLY a fenced ```python block.",
     "assert rle('aaabbc')==[('a',3),('b',2),('c',1)];assert rle('')==[]"),
    ("balanced", "Write a Python function `balanced(s)` returning True if brackets "
     "()[]{} in s are balanced. Return ONLY a fenced ```python block.",
     "assert balanced('([{}])');assert not balanced('([)]');assert balanced('')"),
    ("flatten", "Write a Python function `flatten(xs)` that fully flattens a nested "
     "list of ints. Return ONLY a fenced ```python block.",
     "assert flatten([1,[2,[3,[4]]]])==[1,2,3,4];assert flatten([])==[]"),
]


def cmd_cost(args) -> int:
    """What does N actually cost, through the proxy, with verification included?

    §15 measured 1.60x at N=4 against oMLX directly and without a verifier in
    the loop. Both of those change the answer: the hop adds a fixed ~21 ms per
    request (C5), and the verifier adds real time but also SUBTRACTS it by
    short-circuiting - the expected number of verifications is well below N
    whenever the per-sample pass rate is decent. This measures the thing that
    actually ships.
    """
    import statistics
    from verifier import ChainVerifier, CommandVerifier, PythonSyntaxVerifier

    rows = []
    for n in args.ns:
        per_prompt = []
        for name, prompt, assertion in COST_PROMPTS:
            v = ChainVerifier(name, [
                PythonSyntaxVerifier(),
                CommandVerifier(
                    name="behaviour",
                    command=f"python3 -c \"exec(open('{{file}}').read());{assertion}\"",
                    extract_mode="code:python", suffix=".py", timeout_s=30),
            ])
            r = run_gate(prompt, n=n, verifier=v, temperature=args.temperature,
                         max_tokens=1024, verbose=False)
            per_prompt.append(r)
            print(f"  N={n:<2} {name:14s} {r.wall_s:6.2f}s  "
                  f"accepted={r.accepted!s:5s} verified={r.n_verified}/{n} "
                  f"{'ESCALATED' if r.escalated else ''}")
        walls = [r.wall_s for r in per_prompt]
        rows.append({
            "n": n,
            "median_wall_s": round(statistics.median(walls), 2),
            "accepted": sum(r.accepted for r in per_prompt),
            "escalated": sum(r.escalated for r in per_prompt),
            "mean_verified": round(
                statistics.mean(r.n_verified for r in per_prompt), 2),
            "of": len(per_prompt),
        })
        print(f"  -> N={n} median {rows[-1]['median_wall_s']}s  "
              f"accepted {rows[-1]['accepted']}/{rows[-1]['of']}  "
              f"mean verifications {rows[-1]['mean_verified']}\n")

    base = rows[0]["median_wall_s"]
    print("  cost of N relative to N=1 (the number that decides C9):")
    for r in rows:
        print(f"    N={r['n']:<2} {r['median_wall_s'] / base:4.2f}x   "
              f"accepted {r['accepted']}/{r['of']}   "
              f"verified {r['mean_verified']:.2f} of {r['n']}")

    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "bon_gate_cost.json").write_text(json.dumps(
        {"sampler": SAMPLER, "escalation": ESCALATION,
         "temperature": args.temperature, "rows": rows}, indent=2))
    print("\nsaved -> results/bon_gate_cost.json")
    return 0


def cmd_run(args) -> int:
    prompt = Path(args.prompt_file).read_text() if args.prompt_file else args.prompt
    spec = Path(args.verifier_spec) if args.verifier_spec else find_spec(".")
    v = load_verifier(spec, args.verifier)
    print(f"verifier: {getattr(v, 'name', type(v).__name__)} "
          f"(spec: {spec or 'none found'})")
    r = run_gate(prompt, n=args.n, verifier=v, sampler=args.sampler,
                 escalation=args.escalation, temperature=args.temperature,
                 max_tokens=args.max_tokens)
    print(f"\n--- {'ACCEPTED' if r.accepted else 'NOT ACCEPTED'} "
          f"({r.source}, {r.wall_s}s) ---")
    print(r.text)
    return 0 if r.accepted else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("demo", help="prove the loop end-to-end")
    d.add_argument("--n", type=int, default=4)
    d.set_defaults(func=cmd_demo)

    c = sub.add_parser("cost", help="what N costs through the real path")
    c.add_argument("--ns", type=int, nargs="+", default=[1, 2, 4, 8])
    c.add_argument("--temperature", type=float, default=0.7)
    c.set_defaults(func=cmd_cost)

    r = sub.add_parser("run", help="gate one prompt")
    r.add_argument("--prompt")
    r.add_argument("--prompt-file")
    r.add_argument("--verifier-spec")
    r.add_argument("--verifier")
    r.add_argument("--n", type=int, default=4)
    r.add_argument("--sampler", default=SAMPLER)
    r.add_argument("--escalation", default=ESCALATION)
    r.add_argument("--temperature", type=float, default=0.7)
    r.add_argument("--max-tokens", type=int, default=4096)
    r.set_defaults(func=cmd_run)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
