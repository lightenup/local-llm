"""Profiles for `Qwen3.8-Flash-Next-oQ4e-mtp`, the candidate quality tier.

UNMEASURED — these profiles exist so the arm can be benchmarked at all. Nothing
below is validated yet; every number in this docstring is either from the vendor
card, from oMLX's own source, or arithmetic over the checkpoint's safetensors
headers. Do not wire any of this into a client before the arm has run.

What this model is: `Jundot/Qwen3.8-Flash-Next-oQ4e-mtp`, an oQ mixed 4-bit build
of `Qwen/Qwen3.8-Flash-Next` — 125B total with 6B active, 512 experts routing
10 + 1 shared, 48 layers as 12 x (3 x Gated-DeltaNet -> MoE, then 1 x Qwen Sparse
Attention -> MoE), plus a 51.2B hashed n-gram embedding table (the PLE) and a 4B
Lightning MTP head. Same uploader as `oq4e.py`'s checkpoint, which is deliberate:
A2b established that uploader's builds are genuine data-driven mixed-up quants,
so the build method is held constant against the existing fast tier and only the
model changes.

Why it is worth measuring at all, given FINDINGS §22 closed "parameter count is
not the lever": §22 rejected 80B and 122B MoEs of the *previous* generation, and
dismissed this checkpoint on the grounds that `qwen4_exp` is "a different
architecture, not a scaled-up Qwen3.8". That is still true and is now the reason
to test rather than the reason to skip — the vendor's claimed gaps over the
shipped 27B are agentic, not general: DeepSWE 58.7 vs 42.2, JobBench 55.7 vs
33.4, Toolathlon 73.5 vs 67.1, against LiveCodeBench v6 91.9 vs 90.3. If that
shape is real it is exactly the axis §14 found livecodebench blind to.

ARCHITECTURE NOTE — the memory arithmetic is not the 27B's
----------------------------------------------------------
Three of every four layers are Gated DeltaNet, which carries a fixed-size
recurrent state instead of a growing KV cache, so only the 12 QSA layers pay per
token. oMLX prices those itself in
`omlx/memory_monitor.py::estimate_qwen4_exp_kv_bytes_per_token`, as
2*kv_heads*head_dim*dtype + indexer_head_dim*dtype + 3*8 per QSA layer. With this
config (12 QSA layers, 2 KV heads, head_dim 256, indexer_head_dim 128, bf16) that
is 27.3 KB/token against the 27B's measured ~129 KB/token, so 262k of context
costs ~7.3 GB rather than ~34.6 GB. Computed, not measured — bench/kv_shape.py
is the instrument that settles it.

RESIDENCY — the PLE must be offloaded explicitly, and that is a trap
--------------------------------------------------------------------
The checkpoint is 106.3 GB on disk (99.0 GiB), of which 30.0 GB is the PLE table
in six n-gram-only shards. oMLX estimates residency in
`patches/mlx_vlm_qwen4_exp_compat/residency.py` as `checkpoint * 1.05` with the
table resident and `(checkpoint - ple) * 1.05` with it mmapped off SSD:

    PLE resident : 111.6 GB estimated, 103.94 GB as the loader prices it
    PLE on SSD   :  78.0 GB estimated,  72.65 GB priced, 69.37 GB once loaded

`Qwen4ExpResidencyEstimate.force_ssd_offload` only forces the mmap when resident
exceeds the admission ceiling AND the mmap figure does not. It compares against
`final_ceiling` (115.45 GB here), which 111.6 GB fits, so **nothing is forced**:
left alone, oMLX loads the table into memory. `qwen4_ple_ssd_offload` therefore
has to be set deliberately.

MEASURED 2026-09-13, and it corrects an earlier draft of this file: there are
THREE ceilings, and `final_ceiling` is not the one that admits a load.

    final_ceiling   115.45 GB   what force_ssd_offload consults
    metal_cap       107.52 GB   the Metal wired limit; iogpu.wired_limit_mb is 0
                                (default) on this 128 GB machine
    dynamic          91.29 GB   moves with reclaimable host RAM, i.e. with
                                whatever else is running; the static cap is
                                122.00 GB but only what is reclaimable counts

FINDINGS §H's 107.52 GB was right. Worse, the resident arm is not merely tight
but unusable: it loads (99.21 GB) and then oMLX's *prefill* guard rejects even a
trivial prompt, because nothing is left for KV. Offloaded is the only
configuration that serves.

That field is engine-level twice over: `engine_pool.py::_variant_signature` adds
it, so flipping it reloads a 70 GB engine, and oMLX itself lists it in
`model_profiles.py::EXCLUDED_FROM_PROFILES` ("hardware-specific residency
choice; never propagate across models"). It belongs on the base model, which is
the same rule as the README's, arrived at independently by the vendor.

COEXISTENCE — measured, and the answer is no (results/flashnext_residency.json)
The admission guard does not evict to make room, it refuses: 72.65 GB on top of
the 51.71 GB lineup projects to 124.36 GB and is declined. Both models can be
held (69.37 + 28.2 GB), but alternating 6000-token prompts evicted one of them on
3 of 6 calls, and a call that lands on Flash-Next costs a median 63.5 s against
the 27B's 3.9 s. That is 3x the ~20 s alternation that retired Coder-Next (A2c),
so this is a REPLACEMENT tier or nothing.

SAMPLING — the vendor's recommendation looked MTP-hostile and is not
--------------------------------------------------------------------
Qwen recommends non-thinking mode at temperature 0.7 / top_p 0.80 / top_k 20 with
**presence_penalty 1.5**, and thinking mode at 1.0 / 0.95 / 20 with presence
penalty 0. A non-zero presence penalty needs a logits processor, and FINDINGS §7
measured that defeating MTP on the 27B — which is why an earlier draft of this
file rejected the vendor's value outright.

MEASURED 2026-09-13 (results/fn_mtp_check.json), crossing mtp_enabled with the
penalty, 5 repeats per cell at 400 generated tokens:

    mtp_enabled=True   penalty 0.0   74.57 tok/s
    mtp_enabled=True   penalty 1.5   74.07 tok/s
    mtp_enabled=False  penalty 0.0   39.97 tok/s
    mtp_enabled=False  penalty 1.5   39.43 tok/s

MTP is genuinely engaged and worth 1.87x, and the penalty costs 1% — inside the
noise. So §7's exclusivity is a property of DRAFTER-based speculation
(`vlm_mtp_draft_model`), not of `qwen4_exp`'s in-checkpoint Lightning head. The
validator below therefore allows a non-zero presence penalty **for this model
only**, while the profiles still ship 0.0, so this arm's numbers stay comparable
to every other arm in the repo. Adopting the vendor's 1.5 is now a quality
question, not a throughput one, and it is unmeasured.

Thinking is ON by default in this checkpoint's chat template, with
`reasoning_effort` at xhigh, which is a §22-shaped trap: an arm that forgets to
disable it is not comparable to any existing measurement in this repo, and fails
by returning empty content after burning the cap on reasoning. Every profile here
states `enable_thinking` explicitly for that reason, and the thinking-off arms
also pin it through `chat_template_kwargs` because the template's default is the
opposite of the 27B's.

`thinking_budget_enabled` is deliberately absent from `fnreason`. FINDINGS §7
measured it costing the 27B its entire MTP win (17.6 vs 31.9 t/s) even though
oMLX documents it as MTP-safe, and there is no reason to expect that to differ
here. The runaway risk it guards against is instead bounded by `max_tokens`, and
the thinking A/B reports how often that cap is hit.
"""

from __future__ import annotations

# The checkpoint advertises 262,144 natively (extensible to 1M via YaRN, which is
# not enabled here). Unlike the 27B this has NOT been probed with bench/context.py
# yet, so treat it as the config's claim, not a measured ceiling.
CLAIMED_CONTEXT_CEILING = 262144

# Until bench/context.py runs against this checkpoint, no profile asks for more
# than half the claimed ceiling: at 27.3 KB/token the arithmetic says 262k fits,
# but the arithmetic also said the PLE would be offloaded automatically.
UNVERIFIED_CONTEXT_CAP = 131072

# Set on the BASE MODEL, never in a profile (see the docstring). Listed here so
# validate() can reject a profile that tries to carry one.
ENGINE_LEVEL_FIELDS = (
    "qwen4_ple_ssd_offload",
    "mtp_enabled",
    "mtp_num_draft_tokens",
    "vlm_mtp_enabled",
    "vlm_mtp_draft_model",
    "vlm_mtp_draft_block_size",
    "dflash_enabled",
    "dflash_draft_model",
    "specprefill_enabled",
    "turboquant_kv_enabled",
    "qwen35_ane_prefill_enabled",
    "trust_remote_code",
)

# Each needs a logits processor, which on the 27B disables speculation outright
# (FINDINGS §7). `presence_penalty` is deliberately NOT in this set: it is the one
# that was actually measured here, and results/fn_mtp_check.json puts the cost of
# the vendor's 1.5 at 1% of decode with MTP on (74.57 -> 74.07 tok/s, inside a
# per-cell stdev of 2.4-4.0) while MTP itself is worth 1.87x. So §7's exclusivity
# is a property of drafter-based speculation, not of the in-checkpoint Lightning
# head, and the vendor's value is permitted for this model.
#
# The other two stay rejected because they were NOT measured. Same mechanism is
# not the same evidence, and §22's lesson is that assuming transfer is how this
# repo has been wrong before.
MTP_INCOMPATIBLE = {
    "repetition_penalty": 1.0,
    "frequency_penalty": 0.0,
}

PROFILES: list[dict] = [
    {
        "name": "fncode",
        "api_name": "fn-code",
        "display_name": "Qwen3.8-Flash-Next oQ4e — Code",
        "description": (
            "Code writing and editing, thinking off. Vendor non-thinking "
            "sampling, but presence_penalty held at 0.0 for comparability with "
            "the other arms rather than necessity. Code accuracy UNMEASURED."
        ),
        "settings": {
            "temperature": 0.7,
            "top_p": 0.8,
            "top_k": 20,
            "force_sampling": True,
            "min_p": 0.0,
            "presence_penalty": 0.0,
            "enable_thinking": False,
            # The template defaults thinking ON, so say so twice: the settings
            # field and the kwargs the template actually reads.
            "chat_template_kwargs": {"enable_thinking": False},
            "max_context_window": UNVERIFIED_CONTEXT_CAP,
            "max_tokens": 32768,
        },
    },
    {
        "name": "fnagent",
        "api_name": "fn-agent",
        "display_name": "Qwen3.8-Flash-Next oQ4e — Agent",
        "description": (
            "Agentic tool use, thinking off, low temperature for reliable "
            "tool-call arguments. This is the axis the vendor claims the "
            "largest gap over the 27B on, so it is the arm that matters most."
        ),
        "settings": {
            "temperature": 0.3,
            "top_p": 0.8,
            "top_k": 20,
            "force_sampling": True,
            "min_p": 0.0,
            "presence_penalty": 0.0,
            "enable_thinking": False,
            "chat_template_kwargs": {"enable_thinking": False},
            "max_context_window": UNVERIFIED_CONTEXT_CAP,
            "max_tokens": 8192,
            "max_tool_result_tokens": 16384,
        },
    },
    {
        "name": "fnreason",
        "api_name": "fn-reason",
        "display_name": "Qwen3.8-Flash-Next oQ4e — Reason",
        "description": (
            "Thinking on at the vendor's thinking-mode sampling, with "
            "reasoning_effort left at the template's own default. No thinking "
            "budget: FINDINGS §7 measured that costing the whole MTP win, so "
            "max_tokens bounds the runaway instead."
        ),
        "settings": {
            "temperature": 1.0,
            "top_p": 0.95,
            "top_k": 20,
            "force_sampling": True,
            "min_p": 0.0,
            "presence_penalty": 0.0,
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
        # replaces the profile's and the table above becomes fiction.
        if not s.get("force_sampling"):
            problems.append(f"{n}: force_sampling must be set or clients override it")

        # This template defaults thinking ON, unlike every other model here, so
        # an unstated value is a silent behaviour change rather than a default.
        if "enable_thinking" not in s:
            problems.append(
                f"{n}: enable_thinking must be explicit — this template defaults "
                f"it ON, so omitting it is not the same as 'off'"
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

        for field, neutral in MTP_INCOMPATIBLE.items():
            if field in s and s[field] != neutral:
                problems.append(
                    f"{n}: {field}={s[field]} needs a logits processor, whose "
                    f"effect on this checkpoint's MTP is UNMEASURED — only "
                    f"presence_penalty was tested (results/fn_mtp_check.json)"
                )
        for field in ENGINE_LEVEL_FIELDS:
            if field in s:
                problems.append(
                    f"{n}: {field} is engine-level; on a profile it forces a "
                    f"reload of an 80 GB engine on every switch"
                )

        # A thinking budget is the one MTP-"safe" field that is not throughput-safe.
        if s.get("thinking_budget_enabled"):
            problems.append(
                f"{n}: thinking_budget_enabled cost the 27B its entire MTP win "
                f"(FINDINGS §7); bound runaway with max_tokens instead"
            )

        mcw = s.get("max_context_window")
        if mcw and mcw > CLAIMED_CONTEXT_CEILING:
            problems.append(
                f"{n}: max_context_window {mcw} exceeds the checkpoint's "
                f"{CLAIMED_CONTEXT_CEILING}"
            )
        if mcw and mcw > UNVERIFIED_CONTEXT_CAP:
            problems.append(
                f"{n}: max_context_window {mcw} is above the unverified cap "
                f"{UNVERIFIED_CONTEXT_CAP}; run bench/context.py first"
            )
    return problems


if __name__ == "__main__":
    probs = validate()
    for p in PROFILES:
        s = p["settings"]
        print(f"{p['api_name']:<11} {p['display_name']}")
        print(
            f"  temp={s.get('temperature')} top_p={s.get('top_p')} "
            f"top_k={s.get('top_k')} thinking={s.get('enable_thinking')} "
            f"ctx={s.get('max_context_window')} max_tok={s.get('max_tokens')}"
        )
    print()
    print("validation:", "OK" if not probs else "\n  - ".join(["FAILED"] + probs))
