#!/usr/bin/env python3
"""Probe the real maximum context this machine can serve, under the memory guard.

oMLX floors the applied value to 2048-token units. The reported boundary is
what `max_context_window` should be set to for a long-context profile.

    python3 bench/context.py --target 262144 --label ctx_mtp
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from omlx_client import MODEL, Omlx  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--target",
        type=int,
        default=262144,
        choices=[16384, 32768, 65536, 131072, 262144, 524288],
    )
    ap.add_argument("--label", required=True)
    ap.add_argument("--model", default=MODEL)
    args = ap.parse_args()

    o = Omlx()
    variant = {
        k: v
        for k, v in o.settings(args.model).items()
        if k.startswith(("turboquant", "vlm_mtp", "qwen35_ane")) and v not in (None, False)
    }
    print(f"== context boundary probe target={args.target} on {args.model}")
    print(f"   variant={json.dumps(variant)}")

    res = o.bench_context(target_tokens=args.target, model_id=args.model)
    payload = {"label": args.label, "target": args.target, "variant": variant, "raw": res}
    Omlx.save(f"ctx_{args.label}", payload)

    if res.get("status") != "completed":
        print(f"!! {res.get('status')}: {res.get('error_message')}")
        return 1

    r = res.get("result") or {}
    print("\n-- result --")
    print(json.dumps(r, indent=2)[:2500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
