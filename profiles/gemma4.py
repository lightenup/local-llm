"""Profiles for `gemma-4-31B-it-qat-OptiQ-4bit` — candidate quality tier.

The first non-Qwen checkpoint in this lineup, which is most of what makes it
worth testing and all of what makes it risky. Nothing measured about the Qwen
family transfers: not the sampling, not the tokenizer arithmetic, not the
thinking defaults, not the context verification.

WHY IT IS HERE. It is the only candidate found whose claimed advantage clears
the resolution floor §28 established. On omlx.ai, against this repo's measured
numbers for the shipped 8-bit:

    mmlu_pro   75.0  vs  64.0      +11.0   <- above the ~6-point floor
    mmlu       87.0  vs  80.0      + 7.0
    gsm8k      96.0  vs  93.6
    mbpp       87.0  vs  80.4
    humaneval  95.0  vs  93.9

and it does that at **21.88 GB resident** (measured,
results/candidate_gate_gemma.json) against the 8-bit's 28.2 GB — smaller AND
apparently better, which no previous candidate managed.

READ THOSE NUMBERS WITH §28 IN MIND. The same model on the same board also
posts humaneval 8.0% and mathqa 19.0%. Those runs carry no sampling settings at
all while the 95.0% runs carry explicit ones, so they are configuration
artifacts, not capability — but they are a standing demonstration that a
leaderboard row is a report about someone's config. None of the figures above
are measured here. That is what these profiles are for.

QAT, NOT JUST POST-HOC QUANTISATION. The checkpoint is quantisation-aware
trained *and* OptiQ-allocated, so FINDINGS §19's "4-bit costs reasoning,
p = 0.0028" should not apply by construction. Should. It is a reason to test,
not a reason to believe.

FOUR THINGS THAT DIFFER FROM EVERY OTHER PROFILE SET HERE, all silent:

  1. **top_k is 64, not 20.** From the checkpoint's own generation_config
     (temperature 1.0, top_k 64, top_p 0.95). Copying the Qwen family's top_k 20
     across would be precisely the mistake oq4e.py records avoiding in the other
     direction with Coder-Next's top_k 40.
  2. **The tokenizer is ~9% less efficient on code.** The identical corpus
     measured 18,196 tokens here against 16,663 for the Qwen build
     (results/candidate_gate*.json). Context windows below are in TOKENS, so the
     same window holds ~9% less repository. A 131072 cap here is worth roughly a
     119k Qwen cap in source text.
  3. **The template defaults thinking OFF** (`thinking_default=False`), the
     inverse of the Qwen checkpoints. Every profile still states it explicitly,
     because "the default is currently safe" is not a property to rely on.
  4. **No MTP.** The config has no MTP heads and no side-car, so there is no
     speculative decoding and no 1.97x. It must win on quality per byte or not
     at all. Measured 15.7 s for 256 tokens on a 16k prompt; the Qwen OptiQ
     build did 11.2 s on the same prompt, part of which is item 2 rather than
     the model.

It loads as `vlm` and carries a vision_config, like the shipped 8-bit.
"""

from __future__ import annotations

# MEASURED 2026-09-17 (FINDINGS §32), and the declared number is not usable.
# bench/longctx_quality.py: 6/6 retrieval at 65,215 prompt tokens across five
# depths plus multi-hop, and HARD FAILURE at 131k and 262k -- on memory, with
# the machine to itself and nothing else resident:
#
#   131072  peak ~98.49 GB = weights 23.05 + KV 20.76 + min-chunk transient 54.6
#   262144  peak ~118.41 GB, KV+SDPA 95.33 GB, against a 102.14 GB cap
#
# The killer is the single-chunk prefill transient, not the KV. The shipped
# 8-bit serves 131k and was verified to 261,826 (B2), so this is a property of
# gemma-4's attention workspace in this runtime, not of the machine.
#
# 65536 was what §32 verified. RE-MEASURED 2026-10-19 (FINDINGS §40) on oMLX
# 0.7.1rc1: the guard admits up to 126,976 with chunked_prefill off, and
# longctx_quality.py retrieved 5/5 at 122,556 prompt tokens. Repeat questions
# reuse the prefix (3-19 s) only with ssd_cache_max_size above 100 GB; at
# 100 GB each paid an ~8 min prefill. 131,072 needs the global chunked_prefill,
# unmeasured on the other models, so it is not taken.
VERIFIED_CONTEXT = 122880
CHECKPOINT_CONTEXT = 262144

# From the checkpoint's generation_config.json. Named rather than inlined so a
# future edit cannot quietly drift toward the Qwen family's numbers.
VENDOR_TOP_K = 64
VENDOR_TOP_P = 0.95

PROFILES: list[dict] = [
    {
        "name": "g4code",
        "api_name": "g4-code",
        "display_name": "gemma-4-31B QAT OptiQ — Code",
        "description": (
            "Code writing, thinking off. Candidate: mbpp 87.0 and humaneval "
            "95.0 on omlx.ai against this repo's 80.4 and 93.9, both UNMEASURED "
            "here. No livecodebench exists for it on the board at all, which is "
            "the suite that actually separated the last three candidates."
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
        "name": "g4agent",
        "api_name": "g4-agent",
        "display_name": "gemma-4-31B QAT OptiQ — Agent",
        "description": (
            "Agentic tool use, thinking off, low temperature for reliable "
            "tool-call arguments. Tool-call behaviour is wholly unknown for "
            "this family here — the toolcall and toolchain categories in "
            "taskbench are the first thing to point at it."
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
    {
        "name": "g4reason",
        "api_name": "g4-reason",
        "display_name": "gemma-4-31B QAT OptiQ — Reason",
        "description": (
            "Thinking on at the vendor's own sampling. This is where the +11 "
            "mmlu_pro claim lives and the only claim on this checkpoint that "
            "clears the resolution floor, so it is the profile to measure "
            "first. Paired against acc_ship_reason."
        ),
        "settings": {
            "temperature": 1.0,
            "top_p": VENDOR_TOP_P,
            "top_k": VENDOR_TOP_K,
            "force_sampling": True,
            "min_p": 0.0,
            "enable_thinking": True,
            "preserve_thinking": True,
            "chat_template_kwargs": {
                "enable_thinking": True,
                "preserve_thinking": True,
            },
            "max_context_window": VERIFIED_CONTEXT,
            "max_tokens": 32768,
        },
    },
]

ENGINE_LEVEL_FIELDS = (
    "mtp_enabled", "vlm_mtp_enabled", "vlm_mtp_draft_model",
    "vlm_mtp_draft_block_size", "dflash_enabled", "dflash_draft_model",
    "specprefill_enabled", "turboquant_kv_enabled",
    "qwen35_ane_prefill_enabled",
)

# Held neutral for the same reason as optiq.py: a paired comparison must differ
# in the checkpoint and nothing else.
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
        if not s.get("force_sampling"):
            problems.append(f"{n}: force_sampling must be set or clients override it")

        if "enable_thinking" not in s:
            problems.append(
                f"{n}: enable_thinking must be explicit — relying on this "
                f"template currently defaulting OFF is how the Qwen sets got "
                f"caught when their default was ON"
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

        # The whole point of a separate module for this family. top_k 20 is the
        # Qwen number and would silently narrow this checkpoint's sampling.
        if s.get("top_k") != VENDOR_TOP_K:
            problems.append(
                f"{n}: top_k={s.get('top_k')} is not gemma-4's vendor value "
                f"{VENDOR_TOP_K} — do not copy the Qwen family's 20"
            )

        for field, neutral in COMPARABILITY_NEUTRAL.items():
            if field in s and s[field] != neutral:
                problems.append(
                    f"{n}: {field}={s[field]} would confound the paired "
                    f"comparison against the 8-bit"
                )
        for field in ENGINE_LEVEL_FIELDS:
            if field in s:
                problems.append(
                    f"{n}: {field} is engine-level; on a profile it forces a "
                    f"full reload on every switch"
                )

        mcw = s.get("max_context_window")
        if mcw and mcw > VERIFIED_CONTEXT:
            # A profile may exceed the cap only by declaring that testing the
            # cap is its purpose. The g4long verification arm that used this
            # has been deleted -- it measured 65536 and its own docstring said
            # to delete rather than lower it, because a `g4-long` serving 65k
            # under a name promising 262k is the trap §26 was written about.
            # The branch stays for the next candidate.
            if not p.get("verification_only"):
                problems.append(
                    f"{n}: max_context_window {mcw} exceeds the VERIFIED ceiling "
                    f"{VERIFIED_CONTEXT}. The checkpoint claims "
                    f"{CHECKPOINT_CONTEXT}; 131k and 262k were both REFUSED by "
                    f"the prefill guard with the machine to itself "
                    f"(FINDINGS §32), and this tokenizer holds ~9% less "
                    f"source per token besides"
                )
            elif mcw > CHECKPOINT_CONTEXT:
                problems.append(
                    f"{n}: even a verification profile cannot exceed the "
                    f"checkpoint's declared {CHECKPOINT_CONTEXT}"
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
