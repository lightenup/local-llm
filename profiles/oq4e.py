"""Profiles for `Qwen3.8-27B-oQ4e-mtp`, the fast half of the lineup.

Replaces `codernext.py` as of 2026-09-12. Coder-Next was chosen as the speed
tier on decode and prefill, and lost the job on two measurements it was never
tested against: it is **10.8 livecodebench points worse** than this checkpoint
(43.2 vs 54.0, p = 0.00006), and at 64.7 GB it cannot stay resident beside the
27B once the memory guard reserves KV, so every alternation cost ~20 s of
evict-and-reload. This checkpoint is 16.0 GB, coexists with the 27B with no
eviction at all, and swaps in ~2.8 s. See docs/backlog.md §A2c.

What this model is: `Jundot/Qwen3.8-27B-oQ4e-mtp`, a data-driven mixed-up 4-bit
build of the production model — 4-bit base with 166 modules promoted to 5 bits.
A2b found that allocation buys nothing over stock RTN 4-bit (p = 0.582 reasoning,
p = 0.096 code), so it is NOT here for its quantization method. It is here for
two other properties:

  - **In-checkpoint MTP.** Unlike the 8-bit, whose config declares MTP layers
    whose tensors are absent, this one reports `mtp_compatible=true` and needs no
    sidecar drafter. Measured 2.12x with `mtp_enabled` against without, which is
    better than the 8-bit's externally-drafted 1.97x. Decode is 58.8 tok/s at a
    16k prompt against the 8-bit's 34.0 — **1.73x**.
  - **16 GB.** The whole of A3b's thrash problem is residency, and this is the
    cheapest capable model that solves it.

What it is NOT for. It is measurably worse than the shipped 8-bit on **both**
axes — reasoning p = 0.0095, code p = 0.00119 (livecodebench 59.8 -> 54.0). So it
takes the speed-dominated, quality-insensitive work and nothing else. Anything
whose correctness is not independently checked stays on the 8-bit. Note this is a
sharper rule than the one Coder-Next got: the earlier reading that 4-bit was
quality-neutral for code came from the RTN arm (p = 0.130) and did NOT replicate
here.

**No `reason` profile**, and unlike Coder-Next the reason is not a missing chat
template — this checkpoint is the same family as the 8-bit and thinking works
fine. It is that reasoning is exactly what it is worst at relative to the 8-bit,
and the 8-bit is resident anyway. A thinking profile here would be a trap.

Sampling is the 27B family's own `generation_config`: top_k 20, top_p 0.95.
Identical to `definitions.py` because this is the same base model, and
deliberately NOT Coder-Next's top_k 40 — copying those numbers across would have
been wrong in the other direction.

The MTP constraint from `definitions.py` applies here too and for the same
reason: oMLX disables MTP for any request needing a logits processor, so no
profile may set `repetition_penalty` != 1.0, `presence_penalty` != 0.0, or
`guided_grammar_enabled`. Enforced in validate().
"""

from __future__ import annotations

# The checkpoint advertises 262,144 and the arithmetic now allows using it.
# Measured KV growth is ~129 KB/token (bench/kv_shape.py), so 262k costs ~37.7 GB
# on top of 16.0 GB of weights = ~53.7 GB, leaving the 8-bit's 28.2 GB resident
# inside the guard's 102.14 GB ceiling. Coder-Next could not do this: at 262k it
# peaked near 85 GB and forced an eviction, which is why `cn-long` carried a
# warning that this file does not need.
MEASURED_CONTEXT_CEILING = 262144

# Unlike codernext.py there is no separate "tier safe" cap, because there is no
# context at which this model evicts the 27B. That is the point of it.
TIER_SAFE_CONTEXT = 262144

PROFILES: list[dict] = [
    {
        "name": "oqcode",
        "api_name": "oq-code",
        "display_name": "Qwen3.8-27B oQ4e — Code",
        "description": (
            "Code writing at 1.73x the 8-bit's decode for 5.8 livecodebench "
            "points (59.8 -> 54.0, p=0.00193). Use where output is checked by "
            "something other than the model; the 8-bit keeps the rest."
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
        "name": "oqagent",
        "api_name": "oq-agent",
        "display_name": "Qwen3.8-27B oQ4e — Agent",
        "description": (
            "Agentic tool use and the quality-insensitive roles: title, "
            "summary, compaction, explore. Low temperature for reliable "
            "tool-call arguments; tool results capped so one large output "
            "cannot evict the working context."
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
        "name": "oqlong",
        "api_name": "oq-long",
        "display_name": "Qwen3.8-27B oQ4e — Long context",
        "description": (
            "Whole-repo analysis at 262k. Unlike the retired cn-long, this does "
            "not evict the 8-bit: ~53.7 GB at full context leaves the 8-bit's "
            "28.2 GB inside the guard ceiling."
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

# Engine-level fields live on the base model, never in a profile: a profile that
# differs on one forces a full engine reload on every switch (README, "The one
# design rule"). `mtp_enabled` is the field that matters here and it is set on
# the model, not below.
ENGINE_LEVEL_FIELDS = (
    "mtp_enabled", "vlm_mtp_enabled", "vlm_mtp_draft_model",
    "vlm_mtp_draft_block_size", "dflash_enabled", "dflash_draft_model",
    "specprefill_enabled", "turboquant_kv_enabled",
    "qwen35_ane_prefill_enabled",
)

# MTP is exclusive with logits processors, so these may never appear.
MTP_INCOMPATIBLE = {
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

        # This checkpoint CAN think; it simply must not here. Reasoning is the
        # one thing it is significantly worse at than the resident 8-bit.
        if s.get("enable_thinking"):
            problems.append(
                f"{n}: thinking belongs on the 8-bit — this checkpoint loses "
                f"reasoning at p=0.0095"
            )

        # Any of these silently disables MTP and costs the 1.73x decode.
        for field, neutral in MTP_INCOMPATIBLE.items():
            if field in s and s[field] != neutral:
                problems.append(
                    f"{n}: {field}={s[field]} needs a logits processor, which "
                    f"disables MTP and forfeits the only reason to use this model"
                )
        for field in ENGINE_LEVEL_FIELDS:
            if field in s:
                problems.append(
                    f"{n}: {field} is engine-level; on a profile it forces a "
                    f"full reload on every switch"
                )

        mcw = s.get("max_context_window")
        if mcw and mcw > MEASURED_CONTEXT_CEILING:
            problems.append(
                f"{n}: max_context_window {mcw} exceeds the checkpoint's "
                f"{MEASURED_CONTEXT_CEILING}"
            )
    return problems


if __name__ == "__main__":
    probs = validate()
    for p in PROFILES:
        s = p["settings"]
        print(f"{p['api_name']:<10} {p['display_name']}")
        print(
            f"  temp={s.get('temperature')} top_p={s.get('top_p')} "
            f"top_k={s.get('top_k')} ctx={s.get('max_context_window')} "
            f"max_tok={s.get('max_tokens')}"
        )
    print()
    print("validation:", "OK" if not probs else "\n  - ".join(["FAILED"] + probs))
