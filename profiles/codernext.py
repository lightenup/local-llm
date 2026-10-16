"""Profiles for `Qwen3-Coder-Next-6bit`, the coder half of the tier.

Why a separate module rather than more entries in definitions.py: the two models
do not share a single constraint. The 27B's set is shaped by its MTP drafter,
which oMLX disables for any request needing a logits processor, so no 27B profile
may set `repetition_penalty`, `presence_penalty` or `guided_grammar_enabled`
(FINDINGS section 4). Coder-Next has no drafter and none can exist -- its
checkpoint carries no MTP tensors and no sidecar exists on the Hub (FINDINGS
section 12) -- so that whole constraint is absent here and those fields are
available.

Three profiles, not four. Coder-Next's chat template has no thinking support at
all, so there is no `reason` profile to write. That is not a gap; it is the
reason the tier exists. Reasoning goes to the 27B, which beats Coder-Next on
mmlu_pro by 6.0 points at p=0.007 with no harness artifact to explain it away
(FINDINGS section 14).

Sampling comes from the checkpoint's own `generation_config.json`: top_k 40,
top_p 0.95, temperature 1.0. Note the top_k differs from the 27B's 20, so
copying that model's numbers across would have been wrong. Temperature is lowered
per role from the checkpoint default of 1.0, the same way the 27B's profiles do.

Context is capped deliberately below what the model can do. Coder-Next reaches
262,144 and retrieves 15/15 needles there, but peaks near 85 GB doing it
(FINDINGS section 12), and the 27B needs 29.5 GB alongside it for the tier to
work at all. 131,072 keeps the pair resident together; `cn-long` exists for the
whole-repo case and is understood to need the 27B evicted first.
"""

from __future__ import annotations

# Verified by bench/longctx_quality.py at 261,813 prompt tokens, 15/15 needles
# and 3/3 multi-hop (FINDINGS section 12).
MEASURED_CONTEXT_CEILING = 262144

# Above this, the 27B (29.5 GB) cannot stay resident beside Coder-Next.
# Measured peaks: 61.6 GB at 1k, 73.3 at 131k, 79.2 at 200k, ~85 extrapolated
# at 262k. 73.3 + 29.5 = 102.8 GB of 128 leaves room; 85 + 29.5 does not.
TIER_SAFE_CONTEXT = 131072

PROFILES: list[dict] = [
    {
        "name": "cncode",
        "api_name": "cn-code",
        "display_name": "Coder-Next 80B-A3B — Code",
        "description": (
            "Code writing and editing at ~2x the 27B's decode and ~3x its "
            "prefill, for 2-4 points of accuracy (FINDINGS section 14). "
            "Checkpoint sampling with temperature lowered for edit loops."
        ),
        "settings": {
            "temperature": 0.7,
            "top_p": 0.95,
            "top_k": 40,
            "force_sampling": True,
            "min_p": 0.0,
            "max_context_window": TIER_SAFE_CONTEXT,
            "max_tokens": 32768,
        },
    },
    {
        "name": "cnagent",
        "api_name": "cn-agent",
        "display_name": "Coder-Next 80B-A3B — Agent",
        "description": (
            "Agentic tool use. Low temperature for reliable tool-call "
            "arguments; oMLX normalises this checkpoint's native function-call "
            "format to the OpenAI shape, verified end to end. Tool results "
            "capped so one large output cannot evict the working context."
        ),
        "settings": {
            "temperature": 0.3,
            "top_p": 0.8,
            "top_k": 40,
            "force_sampling": True,
            "min_p": 0.0,
            "max_context_window": TIER_SAFE_CONTEXT,
            "max_tokens": 8192,
            "max_tool_result_tokens": 16384,
        },
    },
    {
        "name": "cnlong",
        "api_name": "cn-long",
        "display_name": "Coder-Next 80B-A3B — Long context",
        "description": (
            "Whole-repo analysis at the verified 262k ceiling. Peaks near 85 GB, "
            "so the 27B cannot stay resident beside it: expect an eviction when "
            "this profile is used."
        ),
        "settings": {
            "temperature": 0.3,
            "top_p": 0.9,
            "top_k": 40,
            "force_sampling": True,
            "min_p": 0.0,
            "max_context_window": MEASURED_CONTEXT_CEILING,
            "max_tokens": 32768,
        },
    },
]


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
        # The defect FINDINGS section 7 caught on the 27B: without this, opencode's
        # own default temperature silently replaces the profile's.
        if not s.get("force_sampling"):
            problems.append(f"{n}: force_sampling must be set or clients override it")
        # This checkpoint has no thinking mode; asking for one fails quietly.
        if s.get("enable_thinking"):
            problems.append(f"{n}: this checkpoint's chat template has no thinking mode")
        mcw = s.get("max_context_window")
        if mcw and mcw > MEASURED_CONTEXT_CEILING:
            problems.append(
                f"{n}: max_context_window {mcw} exceeds measured ceiling "
                f"{MEASURED_CONTEXT_CEILING}"
            )
        if mcw and mcw > TIER_SAFE_CONTEXT and "long" not in n:
            problems.append(
                f"{n}: context {mcw} evicts the 27B; only the long profile "
                f"should do that"
            )
    return problems


if __name__ == "__main__":
    probs = validate()
    for p in PROFILES:
        s = p["settings"]
        print(f"{p['api_name']:<12} {p['display_name']}")
        print(
            f"  temp={s.get('temperature')} top_p={s.get('top_p')} "
            f"top_k={s.get('top_k')} ctx={s.get('max_context_window')} "
            f"max_tok={s.get('max_tokens')}"
        )
    print()
    print("validation:", "OK" if not probs else "\n  - ".join(["FAILED"] + probs))
