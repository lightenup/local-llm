#!/usr/bin/env python3
"""Which lever, if any, turns flint's thinking off or bounds it in oMLX (FINDINGS §42).

Five arms on flint-mixed-3-4bit, three short prompts each: bare, enable_thinking
false as a profile and as a request kwarg, a 64-token thinking budget, and a
system prompt. Temporary profiles are deleted and the model unloaded on exit.
"""
import json, sys, time
from pathlib import Path
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "bench"))
from omlx_client import Omlx

M = "flint-mixed-3-4bit"
PROMPTS = {
    "arith": ("What is 17*23? Answer with the number only.", "391"),
    "code": ("Write a Python one-liner that reverses a string s. Code only.", "[::-1]"),
    "logic": ("Alice is taller than Bob. Bob is taller than Carol. Who is shortest? One word.", "carol"),
}
PROFILES = {
    "fprobeoff": {"enable_thinking": False},
    "fprobebudget": {"thinking_budget_enabled": True, "thinking_budget_tokens": 64},
}
o = Omlx()
results = []
try:
    for name, s in PROFILES.items():
        o.create_profile(name, name, s, api_name=name, model_id=M)
    arms = {
        "bare": (M, {}, None),
        "profile_off": (f"{M}:fprobeoff", {}, None),
        "request_kwarg_off": (M, {"chat_template_kwargs": {"enable_thinking": False}}, None),
        "budget_64": (f"{M}:fprobebudget", {}, None),
        "system_no_think": (M, {}, "Answer directly. Do not think step by step or reason before answering."),
    }
    for arm, (model, kw, system) in arms.items():
        for pid, (text, expect) in PROMPTS.items():
            msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": text}]
            t0 = time.time()
            try:
                r = o.chat(msgs, model=model, max_tokens=2000, **kw)
                m = r["choices"][0]["message"]
                u = r.get("usage") or {}
                content = (m.get("content") or "").strip()
                reasoning = m.get("reasoning_content") or ""
                rec = {"arm": arm, "prompt": pid, "ok": expect in content.lower() if pid == "logic" else expect in content,
                       "completion": u.get("completion_tokens"),
                       "reasoning_tokens": (u.get("completion_tokens_details") or {}).get("reasoning_tokens"),
                       "reasoning_chars": len(reasoning), "finish": r["choices"][0].get("finish_reason"),
                       "content": content[:60], "wall_s": round(time.time() - t0, 1)}
            except Exception as e:
                rec = {"arm": arm, "prompt": pid, "error": str(e)[:200]}
            results.append(rec)
            print(json.dumps(rec, ensure_ascii=False), flush=True)
finally:
    for name in PROFILES:
        try:
            o.delete_profile(name, model_id=M)
        except Exception as e:
            print(f"!! could not delete profile {name}: {e}")
    try:
        o.unload(M)
    except Exception:
        pass
    left = [p["name"] for p in o.list_profiles(M)]
    print("profiles left on", M, left)
    (REPO / "results/flint_thinking_probe.json").write_text(json.dumps(results, indent=2, ensure_ascii=False))
