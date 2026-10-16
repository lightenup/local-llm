#!/usr/bin/env python3
"""Measure what happens when several clients hit this server at the same time.

Every throughput number in this repo so far is either a single request or the
oMLX bench harness's own synthetic batching, where the harness controls the
batch. Neither answers the question that actually matters for daily use: if two
agents, or an agent plus an editor completion, arrive together, what does each
one feel, and does the box get more total work done?

The server is configured with scheduler.max_concurrent_requests=8,
decode_fairness=true, prefill_priority=context, so up to 8 requests can be
in flight and decode slots are meant to be shared fairly rather than
first-come-first-served. This script tests that claim from the outside.

Two experiments:

  scaling  N identical-shape requests to one profile, for N in --levels.
           Reports batch wall time, median and p95 per-request latency,
           aggregate completion tokens/sec, and the ratio of aggregate
           throughput against level 1. A ratio near N means the decode batch
           is close to free and the server is prefill/bandwidth bound with
           headroom left. A ratio near 1 with latency growing linearly in N
           means requests are effectively serialised and concurrency buys
           nothing but queueing.

  mixed    One request per profile fired simultaneously, four in flight, each
           a different profile. docs/FINDINGS.md section 6 argues profiles are
           free to switch because they carry universal fields only and share
           one engine variant, but that was measured switching sequentially.
           Concurrently is a different question: if oMLX has to apply
           per-profile state to the batch it may split the batch by profile or,
           worse, reload. Each profile's concurrent latency is reported next to
           its alone latency so the degradation is visible, and the
           `VLMBatchedEngine loaded` count in the server log is read before and
           after. A non-zero delta is a significant finding: it would mean the
           shared-variant design does not hold under concurrency.

Prompts are ~2000 tokens of filler by default so prefill is a real share of the
work rather than a rounding error, and each client gets a distinct leading
nonce plus its own question. That is deliberate: with an identical prefix the
first request would populate the prefix cache and the rest would measure cache
hits, which flatters concurrency for the wrong reason.

This script changes no server setting. It only reads settings and sends chat
requests, so it is safe to leave unattended: every request is wrapped, a failed
request is recorded as an error row, and a failed level does not stop the rest.

    python3 bench/concurrency.py
    python3 bench/concurrency.py --experiment scaling --levels 1 2 4 8 16
    python3 bench/concurrency.py --experiment mixed
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from omlx_client import MODEL, Omlx, OmlxError  # noqa: E402

# Same intent as bench/throughput.py::VARIANT_KEYS, by prefix rather than by an
# explicit list, because a concurrency result has to be readable against
# whatever variant happened to be live and the list drifts.
VARIANT_PREFIXES = (
    "vlm_mtp",
    "mtp_",
    "turboquant",
    "dflash",
    "specprefill",
    "qwen35_ane",
)

REQUEST_TIMEOUT = 1800.0

# Measured chars/token on this checkpoint is ~3.1 (see bench/thinking_budget_ab.py),
# used only to size the filler; the real prompt_tokens is read back from usage.
CHARS_PER_TOKEN = 3.1

FILLER = (
    "The maintenance record for this period lists routine inspection of the "
    "pumps, valves, seals and instrumentation loops, with the measured values "
    "logged against their acceptance bands and no deviation reported. "
)

# One question per concurrent client, cycled. Short, self-contained, and with a
# predictable answer length so per-request work is comparable across clients.
QUESTIONS = (
    "Ignoring the log above, name the capital of Norway and say why it is on a fjord.",
    "Ignoring the log above, explain in a short paragraph what a hash table is.",
    "Ignoring the log above, explain in a short paragraph what a deadlock is.",
    "Ignoring the log above, explain in a short paragraph what a B-tree is for.",
    "Ignoring the log above, explain in a short paragraph what TCP backpressure is.",
    "Ignoring the log above, explain in a short paragraph what a race condition is.",
    "Ignoring the log above, explain in a short paragraph what a bloom filter is.",
    "Ignoring the log above, explain in a short paragraph what copy-on-write is.",
)


def variant_snapshot(o: Omlx, model_id: str) -> dict:
    try:
        s = o.settings(model_id)
    except (OmlxError, OSError) as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}
    return {
        k: v for k, v in s.items() if any(k.startswith(p) for p in VARIANT_PREFIXES)
    }


def scheduler_snapshot(o: Omlx) -> dict:
    try:
        return o.global_settings().get("scheduler") or {}
    except (OmlxError, OSError) as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}


def reload_count() -> int:
    """Engine load events in the server log, same technique as bench/smoke.py.

    A delta across a concurrent batch means profiles forced a variant rebuild.
    """
    log = Path.home() / ".omlx" / "logs" / "server.log"
    if not log.exists():
        return 0
    try:
        return log.read_text(errors="replace").count("VLMBatchedEngine loaded")
    except OSError:
        return 0


def build_prompt(client: int, prompt_tokens: int) -> str:
    """Filler sized to prompt_tokens, with a per-client nonce leading it.

    The nonce goes first on purpose: prefix caching keys on the leading blocks,
    so a distinct first block keeps clients from sharing a cached prefill and
    turning a concurrency measurement into a cache-hit measurement.
    """
    nonce = f"[run {client:04d} nonce {client * 7919 + 13:08d}]"
    target_chars = max(0, int(prompt_tokens * CHARS_PER_TOKEN) - len(nonce))
    repeats = max(1, target_chars // len(FILLER))
    question = QUESTIONS[client % len(QUESTIONS)]
    return f"{nonce}\n{FILLER * repeats}\n\n{question}"


def one_request(
    o: Omlx, model: str, client: int, prompt_tokens: int, max_tokens: int
) -> dict:
    """Send one chat request and time it locally.

    Timed here rather than with Omlx.timed_chat because the interesting number
    is the latency this client observed while others were in flight, and the
    caller also needs the start/end offsets to see whether requests overlapped
    at all or were queued back to back.
    """
    messages = [{"role": "user", "content": build_prompt(client, prompt_tokens)}]
    t0 = time.perf_counter()
    try:
        resp = o.chat(
            messages, model=model, max_tokens=max_tokens, timeout=REQUEST_TIMEOUT
        )
    except (OmlxError, OSError) as e:
        return {
            "client": client,
            "model": model,
            "t_start": t0,
            "t_end": time.perf_counter(),
            "error": f"{type(e).__name__}: {str(e)[:300]}",
        }
    except Exception as e:  # noqa: BLE001
        # Unattended run: an unexpected client-side failure must become a row,
        # not the end of the benchmark.
        return {
            "client": client,
            "model": model,
            "t_start": t0,
            "t_end": time.perf_counter(),
            "error": f"unexpected {type(e).__name__}: {str(e)[:300]}",
        }
    t1 = time.perf_counter()

    usage = resp.get("usage") or {}
    choice = (resp.get("choices") or [{}])[0]
    wall = t1 - t0
    completion = usage.get("completion_tokens", 0)
    return {
        "client": client,
        "model": model,
        "t_start": t0,
        "t_end": t1,
        "latency_s": round(wall, 3),
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": completion,
        "cached_tokens": (usage.get("prompt_tokens_details") or {}).get(
            "cached_tokens", 0
        ),
        "tok_per_s": round(completion / wall, 2) if wall else 0.0,
        "finish_reason": choice.get("finish_reason"),
    }


def run_batch(
    o: Omlx,
    jobs: list[tuple[int, str]],
    prompt_tokens: int,
    max_tokens: int,
) -> tuple[list[dict], float]:
    """Fire every job at once and return the rows plus the batch wall time.

    One shared Omlx instance across threads: it holds a single admin cookie, but
    /v1/chat/completions goes down the admin=False path and uses only the API
    key, so chat requests share no mutable state. Threads rather than asyncio
    because the client is blocking urllib and each request spends its time in a
    socket read.
    """
    if not jobs:
        return [], 0.0
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = [
            pool.submit(one_request, o, model, client, prompt_tokens, max_tokens)
            for client, model in jobs
        ]
        rows = []
        for f in futures:
            try:
                rows.append(f.result())
            except Exception as e:  # noqa: BLE001
                rows.append({"error": f"future {type(e).__name__}: {str(e)[:300]}"})
    wall = time.perf_counter() - t0

    origin = min((r.get("t_start") or t0) for r in rows)
    for r in rows:
        if "t_start" in r:
            r["start_offset_s"] = round(r.pop("t_start") - origin, 3)
            r["end_offset_s"] = round(r.pop("t_end") - origin, 3)
    return rows, wall


def pctl(values: list[float], q: float) -> float:
    """Nearest-rank percentile. Levels here are 1 to 8 samples, far too few for
    interpolation to mean anything, so take an observed value."""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def level_stats(rows: list[dict], wall: float) -> dict:
    ok = [r for r in rows if "error" not in r]
    lat = [r["latency_s"] for r in ok]
    completion = sum(r["completion_tokens"] for r in ok)
    return {
        "requested": len(rows),
        "ok": len(ok),
        "failed": len(rows) - len(ok),
        "wall_s": round(wall, 3),
        "median_latency_s": round(statistics.median(lat), 3) if lat else None,
        "p95_latency_s": round(pctl(lat, 0.95), 3) if lat else None,
        "min_latency_s": round(min(lat), 3) if lat else None,
        "max_latency_s": round(max(lat), 3) if lat else None,
        "completion_tokens": completion,
        "prompt_tokens": sum(r["prompt_tokens"] for r in ok),
        "cached_tokens": sum(r["cached_tokens"] for r in ok),
        "aggregate_tok_per_s": round(completion / wall, 2) if wall else 0.0,
    }


# ---------------- experiments ----------------


def experiment_scaling(
    o: Omlx,
    model: str,
    levels: list[int],
    prompt_tokens: int,
    max_tokens: int,
) -> dict:
    print(f"-- scaling on {model}")
    print(f"   levels={levels} prompt~{prompt_tokens}tok max_tokens={max_tokens}\n")

    runs: dict[str, dict] = {}
    summary: list[dict] = []
    client = 0
    for n in levels:
        jobs = []
        for _ in range(n):
            jobs.append((client, model))
            client += 1
        try:
            rows, wall = run_batch(o, jobs, prompt_tokens, max_tokens)
        except Exception as e:  # noqa: BLE001
            print(f"   n={n:<3} LEVEL FAILED {type(e).__name__}: {str(e)[:160]}")
            runs[str(n)] = {"error": f"{type(e).__name__}: {str(e)[:300]}"}
            continue
        st = level_stats(rows, wall)
        st["concurrency"] = n
        runs[str(n)] = {"stats": st, "rows": rows}
        summary.append(st)
        print(
            f"   n={n:<3} wall={st['wall_s']:>8}s  med={st['median_latency_s']}s  "
            f"p95={st['p95_latency_s']}s  agg={st['aggregate_tok_per_s']:>7} t/s  "
            f"ok={st['ok']}/{st['requested']}"
        )

    base = next((s for s in summary if s["concurrency"] == min(levels)), None)
    base_tps = (base or {}).get("aggregate_tok_per_s") or 0.0
    base_lat = (base or {}).get("median_latency_s") or 0.0
    for s in summary:
        s["throughput_ratio_vs_base"] = (
            round(s["aggregate_tok_per_s"] / base_tps, 2) if base_tps else None
        )
        s["scaling_efficiency"] = (
            round(
                (s["aggregate_tok_per_s"] / base_tps) / (s["concurrency"] / base["concurrency"]),
                3,
            )
            if base_tps and base
            else None
        )
        s["latency_ratio_vs_base"] = (
            round(s["median_latency_s"] / base_lat, 2)
            if base_lat and s["median_latency_s"]
            else None
        )
    print()
    return {
        "model": model,
        "levels": levels,
        "baseline_concurrency": min(levels) if levels else None,
        "summary": summary,
        "runs": runs,
    }


def experiment_mixed(
    o: Omlx,
    profiles: list[dict],
    prompt_tokens: int,
    max_tokens: int,
) -> dict:
    """One request per profile alone, then all profiles at once."""
    ids = [(p["name"], p["model_id"]) for p in profiles]
    print(f"-- mixed profiles: {[n for n, _ in ids]}")
    print(f"   prompt~{prompt_tokens}tok max_tokens={max_tokens}\n")

    alone: dict[str, dict] = {}
    client = 1000
    for name, model in ids:
        rows, wall = run_batch(o, [(client, model)], prompt_tokens, max_tokens)
        client += 1
        row = rows[0] if rows else {"error": "no row"}
        alone[name] = row
        if "error" in row:
            print(f"   alone {name:<10} FAILED {row['error'][:120]}")
        else:
            print(
                f"   alone {name:<10} {row['latency_s']:>8}s  "
                f"{row['completion_tokens']:>5}tok  {row['tok_per_s']:>7} t/s "
                f"(batch wall {wall:.2f}s)"
            )

    print()
    loads_before = reload_count()
    jobs = []
    concurrent_of: dict[int, str] = {}
    for name, model in ids:
        jobs.append((client, model))
        concurrent_of[client] = name
        client += 1
    try:
        rows, wall = run_batch(o, jobs, prompt_tokens, max_tokens)
    except Exception as e:  # noqa: BLE001
        print(f"   concurrent batch FAILED {type(e).__name__}: {str(e)[:160]}")
        return {
            "profiles": [n for n, _ in ids],
            "alone": alone,
            "error": f"{type(e).__name__}: {str(e)[:300]}",
        }
    loads_after = reload_count()

    concurrent: dict[str, dict] = {}
    for r in rows:
        name = concurrent_of.get(r.get("client"), f"client{r.get('client')}")
        concurrent[name] = r

    rowsum: list[dict] = []
    for name, _ in ids:
        a, c = alone.get(name, {}), concurrent.get(name, {})
        a_lat, c_lat = a.get("latency_s"), c.get("latency_s")
        rowsum.append(
            {
                "profile": name,
                "alone_latency_s": a_lat,
                "concurrent_latency_s": c_lat,
                "slowdown": round(c_lat / a_lat, 2) if a_lat and c_lat else None,
                "alone_tok_per_s": a.get("tok_per_s"),
                "concurrent_tok_per_s": c.get("tok_per_s"),
                "error": c.get("error") or a.get("error"),
            }
        )
        if c_lat is None:
            print(f"   conc  {name:<10} FAILED {str(c.get('error'))[:120]}")
        else:
            slow = rowsum[-1]["slowdown"]
            print(
                f"   conc  {name:<10} {c_lat:>8}s  "
                f"{c.get('completion_tokens'):>5}tok  "
                f"{c.get('tok_per_s'):>7} t/s  slowdown={slow}"
            )

    st = level_stats(rows, wall)
    alone_ok = [r for r in alone.values() if "error" not in r]
    serial_wall = sum(r["latency_s"] for r in alone_ok)
    delta = loads_after - loads_before
    verdict = (
        f"profiles reloaded the engine {delta}x under concurrency: "
        "the shared-variant design does NOT hold in parallel"
        if delta > 0
        else "no engine load during the concurrent batch: profiles share the batch"
    )
    print(
        f"\n   engine loads during concurrent batch: {delta} "
        f"({loads_before} -> {loads_after})"
    )
    print(f"   {verdict}")
    print(
        f"   concurrent wall {st['wall_s']}s against {round(serial_wall, 3)}s "
        f"run one at a time\n"
    )

    return {
        "profiles": [n for n, _ in ids],
        "stats": st,
        "serial_wall_s": round(serial_wall, 3),
        "speedup_vs_serial": (
            round(serial_wall / st["wall_s"], 2) if st["wall_s"] else None
        ),
        "engine_loads_before": loads_before,
        "engine_loads_after": loads_after,
        "engine_loads_delta": delta,
        "verdict": verdict,
        "per_profile": rowsum,
        "alone": alone,
        "concurrent": concurrent,
        "rows": rows,
    }


# ---------------- reporting ----------------


def print_scaling_table(summary: list[dict]) -> None:
    hdr = (
        f"  {'n':>3} {'wall s':>9} {'med lat s':>10} {'p95 lat s':>10} "
        f"{'agg t/s':>9} {'tps x':>6} {'eff':>6} {'lat x':>6} {'fail':>5}"
    )
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for s in summary:
        print(
            f"  {s['concurrency']:>3} {str(s['wall_s']):>9} "
            f"{str(s['median_latency_s']):>10} {str(s['p95_latency_s']):>10} "
            f"{str(s['aggregate_tok_per_s']):>9} "
            f"{str(s['throughput_ratio_vs_base']):>6} "
            f"{str(s['scaling_efficiency']):>6} "
            f"{str(s['latency_ratio_vs_base']):>6} {str(s['failed']):>5}"
        )


def print_mixed_table(mixed: dict) -> None:
    hdr = (
        f"  {'profile':<10} {'alone s':>9} {'conc s':>9} {'slowdown':>9} "
        f"{'alone t/s':>10} {'conc t/s':>10}"
    )
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for r in mixed.get("per_profile", []):
        print(
            f"  {r['profile']:<10} {str(r['alone_latency_s']):>9} "
            f"{str(r['concurrent_latency_s']):>9} {str(r['slowdown']):>9} "
            f"{str(r['alone_tok_per_s']):>10} {str(r['concurrent_tok_per_s']):>10}"
        )


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Multi-client concurrency benchmark for the oMLX server."
    )
    ap.add_argument(
        "--experiment", choices=["scaling", "mixed", "all"], default="all"
    )
    ap.add_argument(
        "--levels",
        type=int,
        nargs="+",
        default=[1, 2, 4, 8],
        help="concurrency levels for the scaling experiment",
    )
    ap.add_argument(
        "--profile",
        default="agent",
        help="profile name used by the scaling experiment (default: agent)",
    )
    ap.add_argument("--max-tokens", type=int, default=200)
    ap.add_argument(
        "--prompt-tokens", type=int, default=2000, help="approximate filler size"
    )
    ap.add_argument("--model", default=MODEL)
    args = ap.parse_args()

    levels = sorted({n for n in args.levels if n > 0})
    if not levels:
        print("!! --levels must contain at least one positive integer")
        return 1

    o = Omlx()
    try:
        profiles = [
            {"name": p["name"], "model_id": p.get("model_id")}
            for p in o.list_profiles(args.model)
            if p.get("model_id")
        ]
    except (OmlxError, OSError) as e:  # noqa: BLE001
        print(f"!! cannot list profiles on {args.model}: {type(e).__name__}: {e}")
        return 1
    if not profiles:
        print(f"!! no profiles with a served model_id on {args.model}")
        return 1

    by_name = {p["name"]: p["model_id"] for p in profiles}
    target = by_name.get(args.profile)
    if args.experiment in ("scaling", "all") and not target:
        print(f"!! profile '{args.profile}' not served; have {sorted(by_name)}")
        return 1

    variant = variant_snapshot(o, args.model)
    scheduler = scheduler_snapshot(o)

    print(f"== concurrency on {args.model}")
    print(f"   profiles={sorted(by_name)}")
    print(f"   scheduler={scheduler}")
    active = {k: v for k, v in variant.items() if v not in (None, False, "", 0)}
    print(f"   variant={json.dumps(active)}")
    print()

    payload: dict = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model_id": args.model,
        "request": {
            "experiment": args.experiment,
            "levels": levels,
            "profile": args.profile,
            "max_tokens": args.max_tokens,
            "prompt_tokens_target": args.prompt_tokens,
            "request_timeout_s": REQUEST_TIMEOUT,
        },
        "profiles": by_name,
        "variant": variant,
        "scheduler": scheduler,
    }

    if args.experiment in ("scaling", "all"):
        try:
            payload["scaling"] = experiment_scaling(
                o, target, levels, args.prompt_tokens, args.max_tokens
            )
        except Exception as e:  # noqa: BLE001
            print(f"!! scaling experiment aborted: {type(e).__name__}: {e}")
            payload["scaling"] = {"error": f"{type(e).__name__}: {str(e)[:300]}"}

    if args.experiment in ("mixed", "all"):
        try:
            payload["mixed"] = experiment_mixed(
                o, profiles, args.prompt_tokens, args.max_tokens
            )
        except Exception as e:  # noqa: BLE001
            print(f"!! mixed experiment aborted: {type(e).__name__}: {e}")
            payload["mixed"] = {"error": f"{type(e).__name__}: {str(e)[:300]}"}

    Omlx.save("concurrency", payload)

    scaling = payload.get("scaling") or {}
    if scaling.get("summary"):
        print(f"\n  scaling ({scaling.get('model')})")
        print_scaling_table(scaling["summary"])
    mixed = payload.get("mixed") or {}
    if mixed.get("per_profile"):
        print("\n  mixed profiles in flight together")
        print_mixed_table(mixed)
        print(
            f"\n  engine loads delta: {mixed.get('engine_loads_delta')}  "
            f"concurrent wall {mixed.get('stats', {}).get('wall_s')}s vs serial "
            f"{mixed.get('serial_wall_s')}s (speedup {mixed.get('speedup_vs_serial')})"
        )
        print(f"  verdict: {mixed.get('verdict')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
