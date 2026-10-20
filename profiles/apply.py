#!/usr/bin/env python3
"""Create/refresh the exposed profiles on the oMLX model, then verify them.

Each profile is exposed as its own model ID, so any OpenAI-compatible client
selects behaviour purely by model name - no config switching, no reload.

    python3 profiles/apply.py --dry-run
    python3 profiles/apply.py
    python3 profiles/apply.py --verify-only
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "bench"))
sys.path.insert(0, str(HERE))

from omlx_client import MODEL, Omlx, OmlxError  # noqa: E402

# Two models, two profile sets, and they are not interchangeable: the 27B's set
# is constrained by its MTP drafter, Coder-Next's by having no thinking mode.
# Selecting the wrong one would write plausible-looking profiles that quietly do
# the wrong thing, so the default model is paired with its own set here.
PROFILE_SETS = {
    "qwen27b": ("definitions", MODEL),
    "codernext": ("codernext", "Qwen3-Coder-Next-6bit"),
    # Replaces codernext as the fast tier, 2026-09-12 (backlog A2c). The
    # codernext entry is kept so the retired profiles can still be deleted, and
    # so the decision is reversible without archaeology.
    "oq4e": ("oq4e", "Qwen3.8-27B-oQ4e-mtp"),
    # Candidate quality tier, unmeasured. Its set is constrained differently
    # again: the chat template defaults thinking ON, so "unset" is not "off".
    "flashnext": ("flashnext", "Qwen3.8-Flash-Next-oQ4e-mtp"),
    # Candidates as of 2026-09-17, both UNMEASURED here and wired to no agent.
    # optiq is the same base model as the shipped 8-bit, so its set mirrors
    # definitions.py and differs only where the build does. gemma4 is the first
    # non-Qwen family in the lineup and shares none of the Qwen constants --
    # top_k 64 rather than 20, and a template that defaults thinking OFF.
    "optiq": ("optiq", "Qwen3.8-27B-OptiQ-4bit"),
    "gemma4": ("gemma4", "gemma-4-31B-it-qat-OptiQ-4bit"),
    # Fast-slot challenger to oq4e, 2026-10-19 (backlog A10 step 2). Trial only.
    "gemma4moe": ("gemma4moe", "gemma-4-26B-A4B-it-oQ6e-mtp"),
}


def apply_all(o: Omlx, model_id: str, profiles: list[dict],
              dry_run: bool = False) -> None:
    try:
        existing = {p["name"]: p for p in o.list_profiles(model_id)}
    except OmlxError as e:
        # A candidate model set can be applied before its checkpoint finishes
        # downloading. That is an ordinary situation, not a bug, and a traceback
        # here reads like a broken script.
        if "404" in str(e):
            raise SystemExit(
                f"!! oMLX does not know {model_id} yet.\n"
                f"   If it is still downloading, wait: "
                f"python3 -c \"import sys; sys.path.insert(0,'bench'); "
                f"from omlx_client import Omlx; print(Omlx().hf_tasks())\""
            ) from None
        raise
    for spec in profiles:
        name = spec["name"]
        action = "update" if name in existing else "create"
        print(f"  {action:<6} {name:<8} -> api_name={spec['api_name']}")
        if dry_run:
            continue
        if action == "create":
            o.create_profile(
                name=name,
                display_name=spec["display_name"],
                settings=spec["settings"],
                api_name=spec["api_name"],
                description=spec.get("description"),
                expose_as_model=True,
                model_id=model_id,
            )
        else:
            o.update_profile(
                name,
                model_id=model_id,
                display_name=spec["display_name"],
                api_name=spec["api_name"],
                description=spec.get("description"),
                settings=spec["settings"],
                expose_as_model=True,
            )


def verify(o: Omlx, model_id: str, profiles: list[dict]) -> int:
    """Confirm stored settings match the definitions and IDs are API-visible."""
    failures = 0
    stored = {p["name"]: p for p in o.list_profiles(model_id)}
    served = {m["id"] for m in o.get("/v1/models", admin=False)["data"]}

    print(f"\n  {'profile':<9} {'served model id':<32} {'served':>7}  settings match")
    print("  " + "-" * 76)
    for spec in profiles:
        name = spec["name"]
        got = stored.get(name)
        if got is None:
            print(f"  {name:<9} {'-':<32} {'-':>7}  MISSING")
            failures += 1
            continue

        # oMLX serves an exposed profile as "<base model id>:<api_name>" and
        # reports that composite in the profile's model_id field.
        api_id = got.get("model_id") or f"{model_id}:{got.get('api_name') or name}"
        is_served = api_id in served
        gs = got.get("settings") or {}
        diff = {
            k: (v, gs.get(k)) for k, v in spec["settings"].items() if gs.get(k) != v
        }
        engine_fields = got.get("has_engine_fields")
        ok = not diff and not engine_fields
        note = "ok" if ok else ""
        if diff:
            note = json.dumps(diff)
        if engine_fields:
            note += " HAS ENGINE FIELDS (forces reload on switch)"
        print(
            f"  {name:<9} {api_id:<32} {'yes' if is_served else 'NO':>7}  {note}"
        )
        if not ok or not is_served:
            failures += 1
    return failures


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", choices=sorted(PROFILE_SETS), default="qwen27b",
                    help="which profile set to apply; each is paired with its model")
    ap.add_argument("--model", help="override the model the chosen set applies to")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify-only", action="store_true")
    args = ap.parse_args()

    module_name, default_model = PROFILE_SETS[args.set]
    module = importlib.import_module(module_name)
    PROFILES, validate = module.PROFILES, module.validate
    if not args.model:
        args.model = default_model

    problems = validate()
    if problems:
        print("!! definitions invalid:")
        for p in problems:
            print(f"   - {p}")
        return 1

    o = Omlx()
    if not args.verify_only:
        print(f"== applying {len(PROFILES)} profiles to {args.model}")
        apply_all(o, args.model, PROFILES, dry_run=args.dry_run)
        if args.dry_run:
            print("\n(dry run, nothing written)")
            return 0

    failures = verify(o, args.model, PROFILES)
    print()
    if failures:
        print(f"!! {failures} profile(s) failed verification")
        return 1
    print(f"all {len(PROFILES)} profiles stored and served")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
