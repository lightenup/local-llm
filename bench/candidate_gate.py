#!/usr/bin/env python3
"""D3 candidate gate: the cheap checks, in order, before any GPU hours.

Backlog D3 turned three retirements' worth of hindsight into a checklist.
This runs the parts of it that are mechanical. It deliberately answers the
questions in cost order and STOPS at the first failure, because every one of
Coder-Next, the GGUF MoEs and Flash-Next was retired after the expensive
measurement rather than before it.

  1. architecture is supported by the bundled mlx_lm or mlx_vlm (oMLX serves
     most checkpoints here through its vlm engine, so an mlx_lm-only check
     rejects models that load; a model_type absent from both will not load)
  2. it loads, and what it actually costs resident -- the on-disk figure is not
     the resident figure
  3. THE ONE THAT MATTERS: it serves a realistic prompt WHILE THE PINNED LINEUP
     IS STILL RESIDENT. Flash-Next loaded fine at 101.50 GiB and then refused
     every 16k call, taking the shipped model down with it (FINDINGS §27). A
     candidate that only works alone is not a candidate.
  4. completion_tokens on every generation, because §27's headline turned out to
     be a decode rate computed over two tokens

Deliberately NOT here: accuracy suites. Those cost hours and §28's arithmetic
says most of them cannot resolve the gaps being asked about anyway. Read the
leaderboard first; this script is what you run after that comes back promising.

Read the `head` field as evidence of *liveness*, not of quality. It calls the
bare model id, with no profile, so a qwen3_5 checkpoint answers with its template
default -- thinking on and no reasoning parser -- and leaks internal monologue
into content ("We need answer user's request: ..."). The shipped 8-bit does the
identical thing called this way, so it is a property of bare calls, not of the
candidate. It is also, per §28, the most likely reason a leaderboard score is
depressed: uploaders call bare model ids too.

Leaves the lineup exactly as it found it.
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from omlx_client import Omlx, OmlxError  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
GIB = 2**30
MLX_MODELS = Path(
    "/Applications/oMLX.app/Contents/Resources/Python/framework-mlx-base"
    "/lib/python3.11/site-packages/mlx_lm/models"
)
# The prefill guard rejects against this, not against final_ceiling. FINDINGS §27.
DYNAMIC_CEILING_GB = 91.29


MLX_VLM_MODELS = MLX_MODELS.parent.parent / "mlx_vlm" / "models"


def supported(model_type: str) -> bool:
    return ((MLX_MODELS / f"{model_type}.py").exists()
            or (MLX_VLM_MODELS / model_type).is_dir())


def drain(o: Omlx, model_id: str, timeout: float = 180.0) -> None:
    """Unload and wait for the pool to actually release it.

    An unload is not synchronous (FINDINGS §26). Without this wait the next
    probe reads the previous candidate as still-loaded, counts it as part of the
    lineup, and then reports the pool's perfectly correct LRU eviction of it as
    a coexistence failure. That is exactly what happened on 2026-09-16 and it
    produced a false reject for gemma-4-31B.
    """
    try:
        o.unload(model_id)
    except Exception:
        pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        if model_id not in [m["id"] for m in o.models() if m.get("loaded")]:
            return
        time.sleep(2)
    print(f"  !! {model_id} still resident after {timeout}s; later probes are suspect")


def probe(o: Omlx, model_id: str, prompt_tokens: int, keep_lineup: bool) -> dict:
    r: dict = {"model_id": model_id, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    entry = next((m for m in o.models() if m["id"] == model_id), None)
    if entry is None:
        r["verdict"] = "not present -- download it first"
        return r

    r["disk_gib"] = round((entry.get("estimated_size") or 0) / GIB, 2)
    r["config_model_type"] = entry.get("config_model_type")
    r["context"] = entry.get("model_context_length")
    r["mtp_compatible"] = entry.get("mtp_compatible")
    r["mtp_reason"] = (entry.get("mtp_compatibility_reason") or "")[:120]

    # ---- step 1: architecture
    mt = r["config_model_type"]
    r["arch_supported"] = supported(mt) if mt else None
    print(f"  arch           {mt} -> {'supported' if r['arch_supported'] else 'NOT SUPPORTED'}")
    if not r["arch_supported"]:
        r["verdict"] = f"reject: neither mlx_lm nor mlx_vlm has {mt}"
        return r

    # ---- step 2: load, and what it really costs
    lineup = [m["id"] for m in o.models() if m.get("loaded")]
    r["lineup"] = lineup
    before = o.health()["engine_pool"]["current_model_memory"]
    try:
        t0 = time.time()
        o.load(model_id)
        r["load_s"] = round(time.time() - t0, 1)
    except OmlxError as e:
        r["verdict"] = "reject: will not load beside the lineup"
        r["load_error"] = str(e)[:400]
        print(f"  load           REFUSED after {round(time.time()-t0,1)}s")
        print(f"                 {str(e)[:300]}")
        return r
    after = o.health()["engine_pool"]["current_model_memory"]
    r["resident_gib"] = round((after - before) / GIB, 2)
    r["pool_gib"] = round(after / GIB, 2)
    print(f"  load           {r['load_s']}s, +{r['resident_gib']} GiB resident, "
          f"pool now {r['pool_gib']} GiB")
    print(f"  mtp            {r['mtp_compatible']} {r['mtp_reason']}")

    # ---- step 3: serve, with the lineup still up
    still = [m["id"] for m in o.models() if m.get("loaded")]
    r["lineup_survived_load"] = all(m in still for m in lineup)
    if not r["lineup_survived_load"]:
        r["evicted_on_load"] = [m for m in lineup if m not in still]
        print(f"  !! loading it EVICTED {r['evicted_on_load']}")

    text = corpus(prompt_tokens)
    r["calls"] = []
    for i in range(2):
        try:
            t0 = time.time()
            resp = o.chat(
                [{"role": "user", "content": text}],
                model=model_id,
                max_tokens=256,
            )
            u = resp.get("usage") or {}
            call = {
                "ok": True,
                "completion_tokens": u.get("completion_tokens"),
                "prompt_tokens": u.get("prompt_tokens"),
                "wall_s": round(time.time() - t0, 1),
                "finish": (resp.get("choices") or [{}])[0].get("finish_reason"),
                "head": (((resp.get("choices") or [{}])[0].get("message") or {})
                         .get("content") or "")[:90],
            }
        except Exception as e:
            call = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"}
        r["calls"].append(call)
        if call["ok"]:
            print(f"  serve rep{i}    {call['completion_tokens']} tok  "
                  f"prompt={call['prompt_tokens']}  {call['wall_s']}s  "
                  f"finish={call['finish']}  {call['head'][:50]!r}")
        else:
            print(f"  serve rep{i}    FAILED {call['error'][:200]}")
        # step 4 / §27: a short generation makes any rate meaningless
        if call.get("ok") and (call.get("completion_tokens") or 0) < 16:
            call["short_generation"] = True
            print("                 !! short generation -- do not read a rate off this")

    served = [c for c in r["calls"] if c.get("ok")]
    after_serving = [m["id"] for m in o.models() if m.get("loaded")]
    r["lineup_survived_serving"] = all(m in after_serving for m in lineup)
    r["coexists"] = bool(
        served
        and len(served) == len(r["calls"])
        and r["lineup_survived_load"]
        and r["lineup_survived_serving"]
    )
    r["verdict"] = (
        "PASS -- coexists and serves" if r["coexists"]
        else "reject: cannot serve while the lineup is resident"
    )
    return r


def corpus(approx_tokens: int) -> str:
    want = int(approx_tokens * 3.6)
    parts, total = [], 0
    for p in sorted((ROOT / "bench").glob("*.py")):
        t = p.read_text()
        parts.append(t)
        total += len(t)
        if total >= want:
            break
    body = "".join(parts)[:want]
    return body + "\n\nDescribe what the code above does, module by module."


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("models", nargs="+")
    ap.add_argument("--prompt-tokens", type=int, default=16384)
    ap.add_argument("--label", default="candidate_gate")
    args = ap.parse_args()

    o = Omlx()
    baseline = [m["id"] for m in o.models() if m.get("loaded")]
    print(f"lineup: {baseline}")
    print(f"pool:   {o.health()['engine_pool']['current_model_memory']/GIB:.2f} GiB")
    print(f"dynamic prefill ceiling ~{DYNAMIC_CEILING_GB} GB (FINDINGS §27)\n")

    out = []
    try:
        for m in args.models:
            print(f"== {m}")
            r = probe(o, m, args.prompt_tokens, keep_lineup=True)
            print(f"  VERDICT        {r['verdict']}\n")
            out.append(r)
            drain(o, m)
    finally:
        for m in baseline:
            try:
                o.load(m)
            except Exception as e:
                print(f"  restore {m}: {str(e)[:120]}")
        # Anything this run loaded that was not in the baseline has to go back
        # out, or the "restored" line reports success while the pool is wrong.
        for m in [x["id"] for x in o.models() if x.get("loaded") and x["id"] not in baseline]:
            drain(o, m)
        now = [m["id"] for m in o.models() if m.get("loaded")]
        print(f"restored: {now}  (drift: {sorted(set(baseline) ^ set(now)) or 'none'})")
        path = ROOT / f"results/{args.label}.json"
        path.write_text(json.dumps(out, indent=2))
        print(f"saved -> {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
