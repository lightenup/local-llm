#!/usr/bin/env python3
"""Profile definitions for Qwen3.8-27B-8bit on oMLX.

DESIGN RULE — read before editing
---------------------------------
Every profile here sets ONLY "universal" profile fields (sampling, thinking,
context, token caps). Universal fields are merged per request at zero cost.

Engine-level fields (ANE prefill, TurboQuant KV, MTP/DFlash) are deliberately
absent: they are part of oMLX's load-time variant signature
(omlx/engine_pool.py::_variant_signature), so a profile that differed on any of
them would force a full ~28 GB engine reload on every switch. They live once on
the base model instead - see bench/set_variant.py - so all four profiles share
one resident engine and switching is free.

CONSTRAINT — MTP exclusivity
----------------------------
The shared engine variant enables vlm_mtp (≈1.8x decode). oMLX disables vlm_mtp
for any request that needs a logits processor, which means these must stay unset
or neutral in every profile (omlx/model_settings.py::vlm_mtp_processor_conflicts):
    repetition_penalty  (must be None or 1.0)
    presence_penalty    (must be None or 0.0)
    guided_grammar_enabled (must be False)
oMLX's docstring further claims thinking_budget_enabled is SAFE, because the MTP
path applies it at verify time via MTPProcessingSampler. That holds for
correctness but NOT for throughput: measured on the reason profile it costs the
entire MTP win — 17.6 t/s with it against 31.9 t/s without, at 400 generated
tokens each. preserve_thinking has no measurable cost (17.7 t/s). See
docs/FINDINGS.md section 7.

Sampling values start from Qwen's published guidance and the checkpoint's own
generation_config (top_k=20, top_p=0.95): thinking mode 0.6/0.95/20, non-thinking
0.7/0.8/20. Qwen explicitly warns against greedy decoding in thinking mode, so no
profile uses temperature 0.

CONSTRAINT — force_sampling is required, not optional
----------------------------------------------------
oMLX resolves sampling as request > model settings > global
(omlx/server.py::_effective_sampling_params), so a client that sends a
temperature silently overrides everything chosen above. opencode always sends
one: its documented default is 0 for most models and 0.55 for Qwen models, so
without force_sampling all four profiles collapse to whatever the client picked
and the temperature column below becomes fiction.

force_sampling=True flips that branch so profile temperature/top_p/top_k win and
the request value is ignored. That is the intended contract here: the profile *is*
the sampling policy, and callers choose behaviour by selecting a profile rather
than by sending knobs. It is a universal field, so it costs nothing, and it is not
in vlm_mtp_processor_conflicts, so MTP is unaffected.

Note this is deliberately NOT set on the base model, because the benchmark
harness targets the base model and needs its --sampling deterministic mode to
keep working.
"""

from __future__ import annotations

# Set by bench/context.py; the long-context profile uses the measured boundary
# rather than the model's theoretical 262144.
MEASURED_CONTEXT_CEILING = 262144

PROFILES: list[dict] = [
    {
        "name": "code",
        "api_name": "qwen-code",
        "display_name": "Qwen3.8 27B — Code",
        "description": (
            "Code writing and editing. Thinking off for fast edit loops; "
            "Qwen non-thinking sampling. Validated on humaneval/mbpp/livecodebench."
        ),
        "settings": {
            "temperature": 0.7,
            "top_p": 0.8,
            "top_k": 20,
            "force_sampling": True,
            "min_p": 0.0,
            "enable_thinking": False,
            "max_context_window": 131072,
            "max_tokens": 32768,
        },
    },
    {
        "name": "agent",
        "api_name": "qwen-agent",
        "display_name": "Qwen3.8 27B — Agent",
        "description": (
            "Agentic tool use (Droid/Codex/OpenClaw). Low temperature for reliable "
            "tool-call arguments, thinking off for latency, tool results capped so "
            "one large output cannot evict the working context."
        ),
        "settings": {
            "temperature": 0.3,
            "top_p": 0.8,
            "top_k": 20,
            "force_sampling": True,
            "min_p": 0.0,
            "enable_thinking": False,
            "max_context_window": 131072,
            "max_tokens": 8192,
            "max_tool_result_tokens": 16384,
        },
    },
    {
        "name": "reason",
        "api_name": "qwen-reason",
        "display_name": "Qwen3.8 27B — Reason",
        "description": (
            "Deep reasoning and architecture work. Thinking on with a budget so a "
            "hard prompt cannot spend unbounded tokens. Qwen thinking-mode sampling."
        ),
        "settings": {
            "temperature": 0.6,
            "top_p": 0.95,
            "top_k": 20,
            "force_sampling": True,
            "min_p": 0.0,
            "enable_thinking": True,
            "preserve_thinking": True,
            "thinking_budget_enabled": True,
            "thinking_budget_tokens": 8192,
            # Agent clients reserve their whole output cap out of the context
            # window before deciding to compact, so a 65536 window left only
            # ~33.5k usable and forced a summarisation every few tool calls.
            "max_context_window": 131072,
            "max_tokens": 32768,
        },
    },
    {
        "name": "longctx",
        "api_name": "qwen-long",
        "display_name": "Qwen3.8 27B — Long context",
        "description": (
            "Whole-repo and long-document analysis at the measured context ceiling. "
            "Thinking off because at this prompt size prefill dominates; the "
            "generation cap still allows a full document write."
        ),
        "settings": {
            "temperature": 0.3,
            "top_p": 0.9,
            "top_k": 20,
            "force_sampling": True,
            "min_p": 0.0,
            "enable_thinking": False,
            "max_context_window": MEASURED_CONTEXT_CEILING,
            "max_tokens": 32768,
        },
    },
]

# Fields that would silently disable the shared MTP engine variant.
MTP_HOSTILE = {
    "repetition_penalty": (None, 1.0),
    "presence_penalty": (None, 0.0),
    "guided_grammar_enabled": (None, False),
}


def validate() -> list[str]:
    """Return a list of problems; empty means the set is internally consistent."""
    problems: list[str] = []
    seen_names: set[str] = set()
    seen_api: set[str] = set()
    for p in PROFILES:
        n = p["name"]
        if n in seen_names:
            problems.append(f"duplicate profile name: {n}")
        seen_names.add(n)
        api = p.get("api_name")
        if api in seen_api:
            problems.append(f"duplicate api_name: {api}")
        seen_api.add(api)

        s = p["settings"]
        for field, allowed in MTP_HOSTILE.items():
            if field in s and s[field] not in allowed:
                problems.append(
                    f"{n}: {field}={s[field]!r} disables the shared MTP variant "
                    f"(allowed: {allowed})"
                )
        if s.get("enable_thinking") and s.get("temperature") == 0:
            problems.append(f"{n}: greedy decoding in thinking mode is discouraged")
        mcw = s.get("max_context_window")
        if mcw and mcw > MEASURED_CONTEXT_CEILING:
            problems.append(
                f"{n}: max_context_window {mcw} exceeds measured ceiling "
                f"{MEASURED_CONTEXT_CEILING}"
            )
        if s.get("thinking_budget_enabled") and not s.get("thinking_budget_tokens"):
            problems.append(f"{n}: thinking budget enabled with no token count")
    return problems


if __name__ == "__main__":
    probs = validate()
    for p in PROFILES:
        s = p["settings"]
        print(f"{p['api_name']:<14} {p['display_name']}")
        print(
            f"  temp={s.get('temperature')} top_p={s.get('top_p')} "
            f"top_k={s.get('top_k')} thinking={s.get('enable_thinking')} "
            f"ctx={s.get('max_context_window')} max_tok={s.get('max_tokens')}"
        )
    print()
    print("validation:", "OK" if not probs else "\n  - ".join(["FAILED"] + probs))
