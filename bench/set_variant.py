#!/usr/bin/env python3
"""Apply a named engine-level variant to the model and reload the engine.

Engine-level settings are part of oMLX's load-time variant signature
(see omlx/engine_pool.py `_variant_signature`), so they only take effect
after a reload. Named variants keep the A/B matrix honest and repeatable.

    python3 bench/set_variant.py list
    python3 bench/set_variant.py apply mtp
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from omlx_client import MODEL, Omlx  # noqa: E402

DRAFT = "Qwen3.8-27B-MTP-8bit"

# Every engine-level key we touch, so each variant is a full reset rather
# than a patch on top of whatever the last experiment left behind.
RESET = {
    "qwen35_ane_prefill_enabled": False,
    "qwen35_ane_prefill_cpu_enabled": False,
    "turboquant_kv_enabled": False,
    "mtp_enabled": False,
    "vlm_mtp_enabled": False,
    "vlm_mtp_draft_model": None,
    "vlm_mtp_draft_block_size": None,
    "dflash_enabled": False,
    "dflash_draft_model": None,
    "specprefill_enabled": False,
    "specprefill_draft_model": None,
    "specprefill_keep_pct": None,
    "specprefill_threshold": None,
}

VARIANTS: dict[str, dict] = {
    # GPU-only prefill, no speculative decode. The measured baseline.
    "plain": {},
    # ANE prefill offload. The auto-tuner recommended against this on M5 Max
    # (GPU-only 833 t/s vs ANE 742-800 t/s); kept so the finding is re-checkable.
    "ane": {
        "qwen35_ane_prefill_enabled": True,
        "qwen35_ane_prefill_sequence_length": 2048,
        "qwen35_ane_prefill_fraction": 0.53,
        "qwen35_ane_prefill_gdn": True,
        "qwen35_ane_prefill_gdn_fraction": 0.5,
    },
    # Native MTP drafter for the VLM engine. Block size 3 per the draft
    # model card. Requires neutral penalties and no guided grammar.
    "mtp": {
        "vlm_mtp_enabled": True,
        "vlm_mtp_draft_model": DRAFT,
        "vlm_mtp_draft_block_size": 3,
    },
    # 4-bit KV quantisation: trades some accuracy for far smaller KV cache,
    # which is what buys usable 200k+ context.
    "tqkv": {
        "turboquant_kv_enabled": True,
        "turboquant_kv_bits": 4.0,
        "turboquant_skip_last": True,
    },
    # SpecPrefill: sparse prefill driven by a draft model's attention. The one
    # accelerator aimed at prefill, which is the dominant remaining cost here
    # (112 s at 65k, ~17 min at 262k) now that MTP has handled decode.
    #
    # Two caveats, both worth knowing before reading a result:
    #  - oMLX describes it as experimental and MoE-oriented, and this checkpoint
    #    is a hybrid-attention dense model, so it may simply not engage.
    #  - the draft model must share the target's tokenizer. The MTP drafter is
    #    the only local model that does (Qwen2.5-Coder-3B has a different
    #    vocabulary), so it doubles as the specprefill draft here.
    # Threshold 8192 means short prompts are untouched, which is why the A/B has
    # to be run at long lengths to show anything.
    "specprefill": {
        "specprefill_enabled": True,
        "specprefill_draft_model": DRAFT,
        "specprefill_keep_pct": 0.2,
        "specprefill_threshold": 8192,
    },
    # SpecPrefill on top of the shipped decode stack, which is the only
    # combination that would actually ship if it wins.
    "mtp_specprefill": {
        "vlm_mtp_enabled": True,
        "vlm_mtp_draft_model": DRAFT,
        "vlm_mtp_draft_block_size": 3,
        "specprefill_enabled": True,
        "specprefill_draft_model": DRAFT,
        "specprefill_keep_pct": 0.2,
        "specprefill_threshold": 8192,
    },
    # The intended production stack: speculative decode + compressed KV.
    "mtp_tqkv": {
        "vlm_mtp_enabled": True,
        "vlm_mtp_draft_model": DRAFT,
        "vlm_mtp_draft_block_size": 3,
        "turboquant_kv_enabled": True,
        "turboquant_kv_bits": 4.0,
        "turboquant_skip_last": True,
    },
}


# DFlash is deliberately absent. It needs a purpose-built block-diffusion draft
# checkpoint in dflash_draft_model, and no local model qualifies: the MTP drafter
# is a different architecture (qwen3_5_mtp) and Qwen2.5-Coder-3B has a different
# tokenizer. The only candidate found is a third-party repo
# (jfan/Qwen3.8-27B-heretic-dflash, 3.2 GB), and downloading unvetted weights is
# not something to do unattended. oMLX reports dflash_compatible=true for this
# checkpoint, so the option is real and worth a supervised session.


def apply(o: Omlx, name: str, model_id: str = MODEL, reload: bool = True) -> dict:
    if name not in VARIANTS:
        raise SystemExit(f"unknown variant {name!r}; have {list(VARIANTS)}")
    patch = {**RESET, **VARIANTS[name]}
    print(f"== applying variant '{name}'")
    active = {k: v for k, v in VARIANTS[name].items()}
    print(f"   {json.dumps(active) if active else '(all accelerators off)'}")
    o.update_settings(patch, model_id=model_id)
    if reload:
        print("   reloading engine…")
        o.reload_model(model_id)
    after = o.settings(model_id)
    got = {k: after.get(k) for k in patch if after.get(k) not in (None, False)}
    print(f"   active engine settings: {json.dumps(got)}")
    return got


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["list", "apply", "show"])
    ap.add_argument("name", nargs="?")
    ap.add_argument("--no-reload", action="store_true")
    ap.add_argument("--model", default=MODEL)
    args = ap.parse_args()

    o = Omlx()
    if args.action == "list":
        for k, v in VARIANTS.items():
            print(f"  {k:<10} {json.dumps(v)}")
        return 0
    if args.action == "show":
        s = o.settings(args.model)
        print(json.dumps({k: s.get(k) for k in RESET}, indent=2))
        return 0
    if not args.name:
        raise SystemExit("apply needs a variant name")
    apply(o, args.name, args.model, reload=not args.no_reload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
