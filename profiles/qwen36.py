"""Trial profiles for `Qwen3.6-35B-A3B-MLX-8bit`, a candidate for the 8-bit's roles.

Backlog A12, 2026-11-11. FINDINGS §46: on the bare id with thinking off this
checkpoint matches the 8-bit on code, reason, general and safety and decodes
5-6x faster on MLX. These profiles exist so the roles can be measured the way
clients use them (taskbench, agent sessions). Not wired to any alias.

Mirrors definitions.py, profile for profile and value for value, so a
difference between the arms is the model and not the policy. The names
(code/agent/reason/longctx) match the 8-bit's because engine_checks.py looks
profiles up by name; the served ids are `q36-*`.

Thinking defaults ON in this template, and a bare request spends its whole
budget reasoning (§46: empty answers in the gate and the first long-context
run). So every profile states `enable_thinking` twice: the settings field and
the `chat_template_kwargs` the template reads.

Sampling is the 27B's, Qwen's published values for the family with
presence_penalty 0. The checkpoint has no MTP weights, so the 27B's MTP
constraints do not bind here; they are kept for comparability, not necessity.

Context: 64k and 131k retrieve 10/10 on Splash, and 262k retrieves 5/5 there
once the base `max_context_window` is 262144. Nothing is verified on MLX above
the suites' prompt sizes yet, so every profile caps at 131,072.
"""

from __future__ import annotations

VERIFIED_CONTEXT = 131072
CHECKPOINT_CONTEXT = 262144

PROFILES: list[dict] = [
    {
        "name": "code",
        "api_name": "q36-code",
        "display_name": "Qwen3.6 35B-A3B — Code",
        "description": "Trial (backlog A12). Thinking off, Qwen non-thinking sampling.",
        "settings": {
            "temperature": 0.7,
            "top_p": 0.8,
            "top_k": 20,
            "force_sampling": True,
            "min_p": 0.0,
            "enable_thinking": False,
            "chat_template_kwargs": {"enable_thinking": False},
            "max_context_window": VERIFIED_CONTEXT,
            "max_tokens": 32768,
        },
    },
    {
        "name": "agent",
        "api_name": "q36-agent",
        "display_name": "Qwen3.6 35B-A3B — Agent",
        "description": "Trial (backlog A12). Low temperature for tool-call arguments, thinking off.",
        "settings": {
            "temperature": 0.3,
            "top_p": 0.8,
            "top_k": 20,
            "force_sampling": True,
            "min_p": 0.0,
            "enable_thinking": False,
            "chat_template_kwargs": {"enable_thinking": False},
            "max_context_window": VERIFIED_CONTEXT,
            "max_tokens": 8192,
            "max_tool_result_tokens": 16384,
        },
    },
    {
        "name": "reason",
        "api_name": "q36-reason",
        "display_name": "Qwen3.6 35B-A3B — Reason",
        "description": "Trial (backlog A12). Thinking on with the 27B's 8,192-token budget.",
        "settings": {
            "temperature": 0.6,
            "top_p": 0.95,
            "top_k": 20,
            "force_sampling": True,
            "min_p": 0.0,
            "enable_thinking": True,
            "preserve_thinking": True,
            "chat_template_kwargs": {"enable_thinking": True, "preserve_thinking": True},
            "thinking_budget_enabled": True,
            "thinking_budget_tokens": 8192,
            "max_context_window": VERIFIED_CONTEXT,
            "max_tokens": 32768,
        },
    },
    {
        "name": "longctx",
        "api_name": "q36-long",
        "display_name": "Qwen3.6 35B-A3B — Long context",
        "description": "Trial (backlog A12). Thinking off; capped at the verified 131,072.",
        "settings": {
            "temperature": 0.3,
            "top_p": 0.9,
            "top_k": 20,
            "force_sampling": True,
            "min_p": 0.0,
            "enable_thinking": False,
            "chat_template_kwargs": {"enable_thinking": False},
            "max_context_window": VERIFIED_CONTEXT,
            "max_tokens": 32768,
        },
    },
]

ENGINE_LEVEL_FIELDS = (
    "mtp_enabled", "vlm_mtp_enabled", "vlm_mtp_draft_model",
    "vlm_mtp_draft_block_size", "dflash_enabled", "dflash_draft_model",
    "specprefill_enabled", "turboquant_kv_enabled",
    "qwen35_ane_prefill_enabled", "external_engine",
)


def validate() -> list[str]:
    """Return a list of problems; empty means the set is internally consistent."""
    problems: list[str] = []
    names = [p["name"] for p in PROFILES]
    apis = [p["api_name"] for p in PROFILES]
    if len(set(names)) != len(names) or len(set(apis)) != len(apis):
        problems.append("duplicate profile name or api_name")
    for p in PROFILES:
        n, s = p["name"], p["settings"]
        if not s.get("force_sampling"):
            problems.append(f"{n}: force_sampling must be set or clients override it")
        if "enable_thinking" not in s:
            problems.append(f"{n}: enable_thinking must be explicit; the template defaults it on")
        ctk = s.get("chat_template_kwargs") or {}
        if ctk.get("enable_thinking") != s.get("enable_thinking"):
            problems.append(f"{n}: chat_template_kwargs.enable_thinking must match enable_thinking")
        if s.get("enable_thinking") and s.get("temperature") == 0:
            problems.append(f"{n}: greedy decoding in thinking mode is discouraged")
        if s.get("thinking_budget_enabled") and not s.get("thinking_budget_tokens"):
            problems.append(f"{n}: thinking budget enabled with no token count")
        for field in ENGINE_LEVEL_FIELDS:
            if field in s:
                problems.append(f"{n}: {field} is engine-level; on a profile it forces a reload")
        if (s.get("max_context_window") or 0) > VERIFIED_CONTEXT:
            problems.append(
                f"{n}: max_context_window {s['max_context_window']} is above the "
                f"verified cap {VERIFIED_CONTEXT}"
            )
    return problems


if __name__ == "__main__":
    probs = validate()
    for p in PROFILES:
        print(f"{p['api_name']:<11} {p['display_name']}")
    print()
    if probs:
        print("PROBLEMS:")
        for p in probs:
            print(f"  - {p}")
        raise SystemExit(1)
    print("validation: OK")
