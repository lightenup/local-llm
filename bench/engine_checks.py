#!/usr/bin/env python3
"""Behaviour checks for an engine switch, saved as results/check_<label>.json.

What changes when the same profiles move to another engine (Splash, say) is
not whether they answer but whether each profile's policy still holds:

    client_temperature  the profile's sampling wins over a client's
                        temperature: 4 requests at temperature 0 to qwen-code
                        must still differ (force_sampling, FINDINGS §7)
    thinking            code/agent/long return no reasoning; reason does
    budget              qwen-reason's 8,192-token thinking budget holds on the
                        prompt that overran it unbudgeted (thinking_budget_ab)

    python3 bench/engine_checks.py splash_8bit
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine_info import engine_snapshot, engine_tag  # noqa: E402
from omlx_client import MODEL, Omlx  # noqa: E402
from thinking_budget_ab import PROMPTS, run_one  # noqa: E402

THINK_RE = re.compile(r"<think>|</think>", re.I)
RIVER = [{"role": "user", "content":
          "Invent a name for a fictional river and describe it in one sentence."}]
# Budget plus the closing tokens the engine lets through after it.
BUDGET_SLACK = 256


def served_ids(o: Omlx, model: str) -> dict[str, str]:
    return {p["name"]: p["model_id"] for p in o.list_profiles(model)}


def check_client_temperature(o: Omlx, ids: dict) -> dict:
    outs = [
        (o.chat(RIVER, model=ids["code"], max_tokens=40, temperature=0.0)
         ["choices"][0]["message"].get("content") or "")
        for _ in range(4)
    ]
    distinct = len(set(outs))
    return {"ok": distinct >= 2, "distinct": distinct, "of": len(outs)}


def check_thinking(o: Omlx, ids: dict) -> dict:
    rows = {}
    for name in ("code", "agent", "longctx", "reason"):
        resp = o.chat(
            [{"role": "user", "content": "What is 17 * 23? Answer briefly."}],
            model=ids[name], max_tokens=2000,
        )
        msg = resp["choices"][0]["message"]
        reasoning = msg.get("reasoning_content") or ""
        content = msg.get("content") or ""
        thought = bool(reasoning) or bool(THINK_RE.search(content))
        want = name == "reason"
        rows[name] = {
            "ok": thought == want and "391" in content,
            "reasoning_chars": len(reasoning),
            "think_tag_in_content": bool(THINK_RE.search(content)),
            "answer_has_391": "391" in content,
            "completion_tokens": (resp.get("usage") or {}).get("completion_tokens"),
        }
    return {"ok": all(r["ok"] for r in rows.values()), "profiles": rows}


def check_budget(o: Omlx, ids: dict, repeats: int, budget: int) -> dict:
    runs = []
    for i in range(repeats):
        r = run_one(o, ids["reason"], PROMPTS["hard_geometry"]["text"], 32768)
        runs.append({k: r[k] for k in ("reasoning_tokens", "reasoning_tokens_reported",
                                       "completion_tokens", "finish_reason",
                                       "tok_per_s", "answer_chars")})
        print(f"   budget rep{i}: think={r['reasoning_tokens']} "
              f"total={r['completion_tokens']} finish={r['finish_reason']} "
              f"{r['tok_per_s']} t/s", flush=True)
    worst = max(r["reasoning_tokens"] for r in runs)
    answered = all(r["answer_chars"] > 0 for r in runs)
    return {"ok": worst <= budget + BUDGET_SLACK and answered,
            "budget": budget, "max_reasoning_tokens": worst,
            "all_answered": answered, "runs": runs}


def check_bare(o: Omlx, model: str) -> dict:
    """For a model with no profiles: its own sampling, and thinking off on request.

    The accuracy suites call the bare id with enable_thinking false, so that
    switch has to hold on the engine under test.
    """
    outs = [
        (o.chat(RIVER, model=model, max_tokens=40,
                chat_template_kwargs={"enable_thinking": False})
         ["choices"][0]["message"].get("content") or "")
        for _ in range(4)
    ]
    sampling = {"ok": len(set(outs)) >= 2, "distinct": len(set(outs)), "of": len(outs)}
    rows = {}
    for name, kwargs in (("thinking_off", {"chat_template_kwargs": {"enable_thinking": False}}),
                         ("default", {})):
        resp = o.chat([{"role": "user", "content": "What is 17 * 23? Answer briefly."}],
                      model=model, max_tokens=4000, **kwargs)
        msg = resp["choices"][0]["message"]
        reasoning = msg.get("reasoning_content") or ""
        content = msg.get("content") or ""
        rows[name] = {
            "thought": bool(reasoning) or bool(THINK_RE.search(content)),
            "reasoning_chars": len(reasoning),
            "answer_has_391": "391" in content,
            "completion_tokens": (resp.get("usage") or {}).get("completion_tokens"),
        }
    off = rows["thinking_off"]
    # "default" is recorded, not judged: it is the template's own choice.
    return {
        "sampling": sampling,
        "thinking": {"ok": not off["thought"] and off["answer_has_391"], **rows},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("label")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--budget-repeats", type=int, default=2)
    ap.add_argument("--skip-budget", action="store_true")
    args = ap.parse_args()

    o = Omlx()
    ids = served_ids(o, args.model)
    if not ids:
        print(f"== bare-model checks '{args.label}' on {args.model} (no profiles)")
        checks = check_bare(o, args.model)
        for n, c in checks.items():
            print(f"   {n}: {c}")
        payload = {
            "label": args.label,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "model_id": args.model,
            "engine": engine_snapshot(o, args.model),
            "profiles": {},
            "checks": checks,
            "ok": all(c["ok"] for c in checks.values()),
        }
        Omlx.save(f"check_{args.label}", payload)
        print(f"   engine={engine_tag(payload)}  all ok={payload['ok']}")
        return 0 if payload["ok"] else 1
    reason = next(p for p in o.list_profiles(args.model) if p["name"] == "reason")
    budget = (reason.get("settings") or {}).get("thinking_budget_tokens") or 8192

    checks = {}
    print(f"== engine checks '{args.label}' on {args.model}")
    checks["client_temperature"] = check_client_temperature(o, ids)
    print(f"   client_temperature: {checks['client_temperature']}")
    checks["thinking"] = check_thinking(o, ids)
    for n, r in checks["thinking"]["profiles"].items():
        print(f"   thinking {n:<8} {'ok' if r['ok'] else 'FAIL'} {r}")
    if not args.skip_budget:
        checks["budget"] = check_budget(o, ids, args.budget_repeats, budget)
        print(f"   budget: ok={checks['budget']['ok']} "
              f"max={checks['budget']['max_reasoning_tokens']} of {budget}")

    payload = {
        "label": args.label,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model_id": args.model,
        "engine": engine_snapshot(o, args.model),
        "profiles": ids,
        "checks": checks,
        "ok": all(c["ok"] for c in checks.values()),
    }
    Omlx.save(f"check_{args.label}", payload)
    print(f"   engine={engine_tag(payload)}  all ok={payload['ok']}")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
