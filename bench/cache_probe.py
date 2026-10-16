#!/usr/bin/env python3
"""B1a — prefix-cache hit rate under a real opencode session.

Two instruments, because they answer different questions:

`mark` / `report` sample the server's cumulative counters and report the
**delta** between marks. That is the measurement B1a actually wants. The
cumulative `cache_efficiency` on /admin/api/stats is not usable on its own: at
the time of writing it read 81.4% over 729 requests, but this server has served
~40 h of benchmark traffic, which is pathologically repetitive (the same suite
prompts, over and over) and inflates the figure. Only traffic generated between
two marks is attributable to the session under test.

`prefix` classifies a stored prompt's blocks as hot-SSD / disk-SSD / cold
without sending it. That is the diagnostic half: when the hit rate is bad,
this says whether the prefix is landing cold because it is genuinely new or
because something upstream perturbed it (B1b's open question — whether the
injected skills list reorders between sessions).

Usage:
    python3 bench/cache_probe.py mark session-start
    ...do real work against the local model...
    python3 bench/cache_probe.py mark after-first-task
    python3 bench/cache_probe.py report

    python3 bench/cache_probe.py prefix --file /tmp/opencode_prefix.txt
    python3 bench/cache_probe.py prefix --file ... --label second-session
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from omlx_client import MODEL, Omlx, OmlxError  # noqa: E402

STATE = Path(__file__).resolve().parent.parent / "results" / "b1a_cache.json"

# Counters carried from /admin/api/stats. Kept explicit so a server-side schema
# change shows up as a KeyError here rather than as a silently missing delta.
COUNTERS = (
    "total_requests",
    "total_prompt_tokens",
    "total_cached_tokens",
    "total_completion_tokens",
    "total_tokens_served",
)


def _load() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"marks": [], "prefix_probes": []}


def _save(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def cmd_mark(args) -> int:
    o = Omlx()
    stats = o.get("/admin/api/stats")
    mark = {
        "label": args.label,
        "at": _now(),
        "uptime_s": stats["uptime_seconds"],
        "cumulative_efficiency_pct": stats["cache_efficiency"],
        "avg_prefill_tps": stats.get("avg_prefill_tps"),
        "avg_generation_tps": stats.get("avg_generation_tps"),
        **{k: stats[k] for k in COUNTERS},
    }
    state = _load()
    state["marks"].append(mark)
    _save(state)

    print(f"mark '{args.label}' at {mark['at']}")
    print(f"  requests={mark['total_requests']}  "
          f"prompt={mark['total_prompt_tokens']:,}  "
          f"cached={mark['total_cached_tokens']:,}  "
          f"cumulative={mark['cumulative_efficiency_pct']}%")
    if len(state["marks"]) >= 2:
        print()
        _report_window(state["marks"][-2], mark)
    return 0


def _report_window(a: dict, b: dict) -> None:
    """Print the delta between two marks, or say why it is not attributable."""
    label = f"{a['label']} -> {b['label']}"
    if b["uptime_s"] < a["uptime_s"]:
        print(f"{label}: SERVER RESTARTED between marks — counters reset, "
              f"window not attributable.")
        return

    d = {k: b[k] - a[k] for k in COUNTERS}
    reqs = d["total_requests"]
    if reqs == 0:
        print(f"{label}: no requests in window "
              f"({b['uptime_s'] - a['uptime_s']:.0f}s elapsed) — nothing to "
              f"attribute. Was the traffic pointed at the local model?")
        return

    prompt, cached = d["total_prompt_tokens"], d["total_cached_tokens"]
    eff = 100.0 * cached / prompt if prompt else 0.0
    print(f"{label}  ({b['uptime_s'] - a['uptime_s']:.0f}s, {reqs} requests)")
    print(f"  prompt tokens      {prompt:>12,}")
    print(f"  cached tokens      {cached:>12,}")
    print(f"  completion tokens  {d['total_completion_tokens']:>12,}")
    print(f"  tokens/request     {prompt / reqs:>12,.0f}")
    print(f"  WINDOW HIT RATE    {eff:>11.1f}%   "
          f"(cumulative reads {b['cumulative_efficiency_pct']}%)")
    # The number that converts a hit rate into wall-clock: cold prefill at 16k
    # is ~12.6s against ~3.5s warm (FINDINGS §10.1), so ~9.1s per 16k block.
    cold = prompt - cached
    print(f"  cold tokens        {cold:>12,}  "
          f"~{cold / 16384 * 9.1:.0f}s of prefill not avoided")


def cmd_report(args) -> int:
    state = _load()
    marks = state["marks"]
    if len(marks) < 2:
        print(f"{len(marks)} mark(s) recorded — need 2 to form a window.")
        for m in marks:
            print(f"  {m['at']}  {m['label']}")
        return 1

    print("=== B1a: per-window hit rate ===\n")
    for a, b in zip(marks, marks[1:]):
        _report_window(a, b)
        print()

    print("=== whole session ===\n")
    _report_window(marks[0], marks[-1])

    probes = state.get("prefix_probes", [])
    if probes:
        print("\n=== prefix block classification ===\n")
        for p in probes:
            print(f"  {p['at']}  {p['label']}: {p['total_tokens']:,} tok, "
                  f"{p['blocks_cold']}/{p['total_blocks']} blocks cold, "
                  f"sha={p['sha256'][:12]}")
        shas = {p["sha256"] for p in probes}
        if len(shas) > 1:
            print(f"\n  {len(shas)} distinct prefixes across {len(probes)} probes "
                  f"— the prefix is NOT stable, which caps the hit rate "
                  f"regardless of cache behaviour (B1b).")
        else:
            print("\n  prefix byte-identical across all probes.")
    return 0


def cmd_prefix(args) -> int:
    import hashlib

    text = Path(args.file).read_text()
    o = Omlx()
    msgs = [{"role": "system", "content": text},
            {"role": "user", "content": "ping"}]
    try:
        res = o.cache_probe(msgs, model_id=args.model)
    except OmlxError as e:
        print(f"probe failed: {e}", file=sys.stderr)
        return 1

    rec = {
        "label": args.label,
        "at": _now(),
        "file": str(args.file),
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
        "chars": len(text),
        **{k: res[k] for k in
           ("total_tokens", "total_blocks", "blocks_ssd_hot",
            "blocks_ssd_disk", "blocks_cold", "ssd_hit_tokens", "cold_tokens")},
    }
    state = _load()
    state["prefix_probes"].append(rec)
    _save(state)

    hot = rec["blocks_ssd_hot"] + rec["blocks_ssd_disk"]
    print(f"prefix '{args.label}': {rec['total_tokens']:,} tokens, "
          f"{rec['total_blocks']} blocks")
    print(f"  cached blocks  {hot}/{rec['total_blocks']}")
    print(f"  cold tokens    {rec['cold_tokens']:,}")
    print(f"  sha256         {rec['sha256'][:16]}")
    return 0


def cmd_watch(args) -> int:
    """Mark on a fixed interval until interrupted — the hands-off form."""
    n = 0
    print(f"marking every {args.every}s; ctrl-c to stop")
    try:
        while True:
            cmd_mark(argparse.Namespace(label=f"{args.label}-{n:02d}"))
            print()
            n += 1
            time.sleep(args.every)
    except KeyboardInterrupt:
        print("\nstopped.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("mark", help="snapshot the server counters")
    m.add_argument("label")
    m.set_defaults(func=cmd_mark)

    r = sub.add_parser("report", help="hit rate per window and overall")
    r.set_defaults(func=cmd_report)

    p = sub.add_parser("prefix", help="classify a stored prompt's blocks")
    p.add_argument("--file", required=True, type=Path)
    p.add_argument("--label", default="prefix")
    p.add_argument("--model", default=MODEL)
    p.set_defaults(func=cmd_prefix)

    w = sub.add_parser("watch", help="mark on an interval until interrupted")
    w.add_argument("--every", type=float, default=2700.0)
    w.add_argument("--label", default="auto")
    w.set_defaults(func=cmd_watch)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
