"""Profiles for `Qwen3.8-27B-OptiQ-4bit` — the A2b candidate, UNMEASURED here.

What this checkpoint is, stated precisely because its NAME IS MISLEADING. It is
not a 4-bit build. `optiq/metadata.json` reports:

    method        optiq_mixed_precision
    base_model    mlx-community/Qwen3.8-27B-bf16     (the production model)
    reference     uniform_4bit
    target_bpw    5.0        achieved_bpw   5.141804788213628
    n_high_bits   260 modules at 8-bit
    n_low_bits    237 modules at 4-bit
    metric        kl_divergence_vs_reference

So it is a ~5.14 bits-per-weight mixed build, allocated per layer by measured KL
divergence against the bf16 reference. Calling it "4bit" and comparing it to
`oq4e.py`'s true-4-bit 16.0 GB on memory would be wrong in the candidate's
favour: it is **19.0 GB resident** (measured, results/candidate_gate.json),
which is 68% of the shipped 8-bit's 28.2 GB rather than 57%.

WHY IT IS HERE. Backlog A2b asked whether data-driven bit allocation recovers
the reasoning that 4-bit costs. `oq4e.py` already records one answer and it was
negative: Jundot's allocation (166 modules promoted to 5 bits) bought nothing
over stock RTN, p = 0.582 on reasoning. OptiQ is a *different and more
aggressive* allocation — 8-bit rather than 5-bit for the sensitive half, chosen
by an explicit published sensitivity metric — so it re-opens the question rather
than repeating it. The community leaderboard puts the progression at:

    27B 8-bit           mmlu_pro 64.0   (measured here, FINDINGS §19)
    27B RTN 4-bit       mmlu_pro 59.8   (measured here)
    27B oQ4e mixed      mmlu_pro 61.4   (measured here)
    27B OptiQ mixed     mmlu_pro 65.3   (omlx.ai, n=300, NOT measured here)

If that 65.3 replicates it is the first build to beat the 8-bit on reasoning at
two thirds of its size, and the reason tier stops needing 28.2 GB. Treat it as a
hypothesis: §28 established that leaderboard scores are configuration reports,
and the same checkpoint also appears there at 69.0 on n=100.

**THERE IS NO MTP, AND THERE CANNOT BE.** The checkpoint ships
`optiq/mtp.safetensors` (314 MB) and oMLX reports "MTPLX side-car detected but
not imported". The import endpoint then refuses: it requires
`mtplx_runtime.json`, which is not in this repo and not in ANY mlx-community
OptiQ repo checked (2026-09-17). The weights exist; the runtime contract to
activate them is unpublished. oMLX correctly rejects `mtp_enabled: True` for
this model rather than accepting it as a silent no-op the way DFlash2 did
(FINDINGS §23), so this cannot be mis-shipped — but it means the candidate
competes against the 8-bit's 1.97x and oQ4e's 1.73x with **no speculative
decoding at all**. Decode was 11.2 s for 256 tokens on a 16k prompt against the
8-bit's ~17.5 s for 512, which is not a like-for-like figure and must not be
quoted as one.

Two further consequences of the packaging, both silent:

  - **It loads as `batched`, not `vlm`.** The vision tower ships as
    `optiq/optiq_vision.safetensors`, outside `model.safetensors.index.json`, so
    oMLX sees a text-only model. The shipped 8-bit is `vlm`. Nothing here uses
    vision, but a swap is not feature-neutral.
  - **The chat template defaults thinking ON** (`thinking_default=True`), as the
    8-bit does and unlike gemma-4. "Unset" is not "off", so every profile below
    states `enable_thinking` explicitly and validate() enforces it. This is the
    trap that produced the 69.7-vs-82.8 MMLU-Pro split on omlx.ai for a sibling
    checkpoint (FINDINGS §28).

Sampling is the 27B family's own `generation_config`: temperature 1.0, top_k 20,
top_p 0.95, matching `definitions.py` and `oq4e.py` because it is the same base
model. Deliberately NOT gemma-4's top_k 64.

UNLIKE oq4e.py, THIS SET HAS A `reason` PROFILE. oq4e omits one because
reasoning is exactly what that build is worst at. Reasoning is the entire reason
this build is being tested, so omitting it would leave the candidate's only
claim unmeasurable.
"""

from __future__ import annotations

# The checkpoint declares 262,144 and the same base model verified it at 8-bit
# (FINDINGS §8, 15/15 needles at 261,826). That does NOT transfer: B2's result
# is about a checkpoint, not an architecture, and this one has 237 modules at
# 4 bits where the verified one had none. Capped at half until
# bench/longctx_quality.py says otherwise, exactly as flashnext.py did.
UNVERIFIED_CONTEXT_CAP = 131072
CHECKPOINT_CONTEXT = 262144

PROFILES: list[dict] = [
    {
        "name": "optcode",
        "api_name": "opt-code",
        "display_name": "Qwen3.8-27B OptiQ — Code",
        "description": (
            "Code writing, thinking off. Candidate only: code accuracy is "
            "UNMEASURED here and the leaderboard's full-dataset humaneval for "
            "this build is 92.1, matching RTN 4-bit rather than the 8-bit's "
            "93.9. The allocation claim is about reasoning, not code."
        ),
        "settings": {
            "temperature": 0.7,
            "top_p": 0.8,
            "top_k": 20,
            "force_sampling": True,
            "min_p": 0.0,
            "enable_thinking": False,
            "chat_template_kwargs": {"enable_thinking": False},
            "max_context_window": UNVERIFIED_CONTEXT_CAP,
            "max_tokens": 32768,
        },
    },
    {
        "name": "optagent",
        "api_name": "opt-agent",
        "display_name": "Qwen3.8-27B OptiQ — Agent",
        "description": (
            "Agentic tool use, thinking off, low temperature for reliable "
            "tool-call arguments. The arm taskbench's toolcall and toolchain "
            "categories are pointed at."
        ),
        "settings": {
            "temperature": 0.3,
            "top_p": 0.8,
            "top_k": 20,
            "force_sampling": True,
            "min_p": 0.0,
            "enable_thinking": False,
            "chat_template_kwargs": {"enable_thinking": False},
            "max_context_window": UNVERIFIED_CONTEXT_CAP,
            "max_tokens": 8192,
            "max_tool_result_tokens": 16384,
        },
    },
    {
        "name": "optreason",
        "api_name": "opt-reason",
        "display_name": "Qwen3.8-27B OptiQ — Reason",
        "description": (
            "THE PROFILE THIS CANDIDATE EXISTS FOR. Thinking on at the family's "
            "thinking-mode sampling, to test whether KL-driven allocation "
            "recovers the reasoning 4-bit costs (backlog A2b). Paired against "
            "acc_ship_reason and acc_oq4e_reason, never read alone."
        ),
        "settings": {
            "temperature": 1.0,
            "top_p": 0.95,
            "top_k": 20,
            "force_sampling": True,
            "min_p": 0.0,
            "enable_thinking": True,
            "preserve_thinking": True,
            "chat_template_kwargs": {
                "enable_thinking": True,
                "preserve_thinking": True,
            },
            "max_context_window": UNVERIFIED_CONTEXT_CAP,
            "max_tokens": 32768,
        },
    },
]

# Engine-level fields live on the base model, never in a profile: a profile that
# differs on one forces a full engine reload on every switch (README, "The one
# design rule").
ENGINE_LEVEL_FIELDS = (
    "mtp_enabled", "vlm_mtp_enabled", "vlm_mtp_draft_model",
    "vlm_mtp_draft_block_size", "dflash_enabled", "dflash_draft_model",
    "specprefill_enabled", "turboquant_kv_enabled",
    "qwen35_ane_prefill_enabled",
)

# NOT an MTP constraint here -- there is no MTP to forfeit. Held neutral anyway
# so that a paired comparison against the 8-bit and oQ4e arms differs in the
# checkpoint and nothing else. A penalty on one arm only would confound the
# measurement this candidate exists to produce.
COMPARABILITY_NEUTRAL = {
    "repetition_penalty": 1.0,
    "presence_penalty": 0.0,
    "frequency_penalty": 0.0,
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
        # FINDINGS §7: without this, opencode's own default temperature silently
        # replaces the profile's.
        if not s.get("force_sampling"):
            problems.append(f"{n}: force_sampling must be set or clients override it")

        # thinking_default is True on this checkpoint, so omitting the field
        # does not mean "off" -- it means "on, silently".
        if "enable_thinking" not in s:
            problems.append(
                f"{n}: enable_thinking must be explicit — this template "
                f"defaults thinking ON"
            )
        ctk = s.get("chat_template_kwargs") or {}
        if "enable_thinking" in ctk and ctk["enable_thinking"] != s.get(
            "enable_thinking"
        ):
            problems.append(
                f"{n}: chat_template_kwargs.enable_thinking="
                f"{ctk['enable_thinking']!r} contradicts enable_thinking="
                f"{s.get('enable_thinking')!r}"
            )

        for field, neutral in COMPARABILITY_NEUTRAL.items():
            if field in s and s[field] != neutral:
                problems.append(
                    f"{n}: {field}={s[field]} differs from the 8-bit and oQ4e "
                    f"arms and would confound the A2b comparison"
                )
        for field in ENGINE_LEVEL_FIELDS:
            if field in s:
                problems.append(
                    f"{n}: {field} is engine-level; on a profile it forces a "
                    f"full reload on every switch"
                )

        # FINDINGS §8 measured the 27B losing its whole MTP win to a thinking
        # budget. This build has no MTP to lose, but an unbudgeted runaway is
        # still bounded by max_tokens rather than by a budget, as fnreason is.
        if s.get("thinking_budget_enabled"):
            problems.append(
                f"{n}: thinking_budget_enabled cost the 27B its MTP win "
                f"(FINDINGS §8); bound runaways with max_tokens instead"
            )

        mcw = s.get("max_context_window")
        if mcw and mcw > UNVERIFIED_CONTEXT_CAP:
            problems.append(
                f"{n}: max_context_window {mcw} exceeds the UNVERIFIED cap "
                f"{UNVERIFIED_CONTEXT_CAP}; the checkpoint claims "
                f"{CHECKPOINT_CONTEXT} but nothing here has tested it"
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
    print("profile set is internally consistent")
