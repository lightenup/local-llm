"""Trial profiles for `gemma-4-26B-A4B-it-oQ6e-mtp`, a fast-slot challenger.

Backlog A10 step 2, 2026-10-19. The question is whether this checkpoint can
take the fast slot from `Qwen3.8-27B-oQ4e-mtp` (oq4e.py): the slot behind the
`fast` alias and opencode's summary/compaction/explore agents.

What it is: a gemma-4 mixture-of-experts (128 experts, ~4B active, 30 layers),
mixed-precision 6-bit (oQ6e), with an MTP head in the checkpoint
(`model-mtp.safetensors`). oMLX reports it `mtp_compatible` and serves it on
the mlx_lm MTP path, the same one oQ4e uses and the one §39 found honours the
sampler. That is still checked per profile (doctor "profile samples"), not
assumed.

D3 gate PASSED 2026-10-19 (results/a10_gate_g4moe.json): +22.37 GiB resident,
serves an 18k prompt with the 8-bit, oQ4e and gemma-31B all resident.

Mirrors oq4e.py's two fast-slot profiles, code and agent, at gemma-4's vendor
sampling (top_k 64, from the checkpoint's generation_config.json). No reason
profile: the fast slot does not reason, and gemma-31B's g4-reason is the
family's reasoner candidate.

Context measured 2026-10-20: retrieval 5/5 at 65,536 and 131,072
(results/a10_g4moe_longctx.json; one multi-hop miss at 131k, which oQ4e
passes), and the prefill guard admits up to 258,048 with retrieval 3/3 at
248,704 (results/a10d3_g4moe_bisect.json). The cap is the fast slot's
declared 131,072, not the admitted ceiling: nothing routes longer prompts
here, and the multi-hop miss is the first sign of degradation.

WIRED 2026-10-20: g4m-agent is the `fast` alias (FINDINGS §41). Known weakness
(taskbench, 5 repeats): it loosens return-type contracts, a generator for
"lists of at most n items" and tuples for interval lists, 0/5 on py-diff and
hpy-intervals where oQ4e, the 8-bit and gemma-31B pass. That is why it holds
`fast` and not `coder`.
"""

from __future__ import annotations

VERIFIED_CONTEXT = 65536
CHECKPOINT_CONTEXT = 262144

VENDOR_TOP_K = 64
VENDOR_TOP_P = 0.95

PROFILES: list[dict] = [
    {
        "name": "g4mcode",
        "api_name": "g4m-code",
        "display_name": "gemma-4-26B-A4B oQ6e — Code",
        "description": (
            "Code profile for the fast-slot model, thinking off. Not wired: "
            "`coder` stays on the 8-bit (backlog A10, FINDINGS §41)."
        ),
        "settings": {
            "temperature": 0.7,
            "top_p": 0.8,
            "top_k": VENDOR_TOP_K,
            "force_sampling": True,
            "min_p": 0.0,
            "enable_thinking": False,
            "chat_template_kwargs": {"enable_thinking": False},
            "max_context_window": VERIFIED_CONTEXT,
            "max_tokens": 32768,
        },
    },
    {
        "name": "g4magent",
        "api_name": "g4m-agent",
        "display_name": "gemma-4-26B-A4B oQ6e — Agent (fast)",
        "description": (
            "Behind the `fast` alias since 2026-10-20: summary, compaction, "
            "explore. Low temperature for tool-call arguments."
        ),
        "settings": {
            "temperature": 0.3,
            "top_p": 0.8,
            "top_k": VENDOR_TOP_K,
            "force_sampling": True,
            "min_p": 0.0,
            "enable_thinking": False,
            "chat_template_kwargs": {"enable_thinking": False},
            "max_context_window": VERIFIED_CONTEXT,
            "max_tokens": 8192,
            "max_tool_result_tokens": 16384,
        },
    },
]

# oMLX disables MTP for any request that needs a logits processor (oq4e.py), so
# these must stay neutral or the speed being measured is not the MTP speed.
MTP_NEUTRAL = {
    "repetition_penalty": 1.0,
    "presence_penalty": 0.0,
    "frequency_penalty": 0.0,
}

ENGINE_LEVEL_FIELDS = (
    "mtp_enabled", "vlm_mtp_enabled", "vlm_mtp_draft_model",
    "vlm_mtp_draft_block_size", "dflash_enabled", "dflash_draft_model",
    "specprefill_enabled", "turboquant_kv_enabled",
    "qwen35_ane_prefill_enabled",
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
            problems.append(f"{n}: enable_thinking must be explicit")
        ctk = s.get("chat_template_kwargs") or {}
        if "enable_thinking" in ctk and ctk["enable_thinking"] != s.get("enable_thinking"):
            problems.append(f"{n}: chat_template_kwargs.enable_thinking contradicts enable_thinking")
        if s.get("top_k") != VENDOR_TOP_K:
            problems.append(f"{n}: top_k={s.get('top_k')} is not gemma-4's vendor {VENDOR_TOP_K}")
        for field, neutral in MTP_NEUTRAL.items():
            if field in s and s[field] != neutral:
                problems.append(f"{n}: {field}={s[field]} would take requests off the MTP path")
        if s.get("guided_grammar_enabled"):
            problems.append(f"{n}: guided grammar takes requests off the MTP path")
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
        print(f"{p['api_name']:<12} {p['display_name']}")
    print()
    if probs:
        print("PROBLEMS:")
        for p in probs:
            print(f"  - {p}")
        raise SystemExit(1)
    print("ok")
