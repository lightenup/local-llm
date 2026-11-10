#!/usr/bin/env python3
"""Which engine served a result, recorded beside every measurement.

oMLX can hand a model to Splash, a separate Metal engine with DFlash2
speculative decoding, instead of running it in-process on MLX. Both engines
answer under the same model id, the same profiles and the same API, so a
result file that does not say which one served it cannot be compared with
anything. `engine_snapshot()` goes into every result payload under "engine".

Files written before that field existed were all served in-process, which is
what `engine_tag()` reports for them.
"""

from __future__ import annotations

import plistlib
from pathlib import Path
from typing import Any

OMLX_APP_PLIST = Path("/Applications/oMLX.app/Contents/Info.plist")
GIB = 2**30


def omlx_version() -> str | None:
    """The installed oMLX build. The server API does not report it."""
    try:
        with OMLX_APP_PLIST.open("rb") as f:
            return plistlib.load(f).get("CFBundleShortVersionString")
    except (OSError, plistlib.InvalidFileException):
        return None


def engine_snapshot(o: Any, model_id: str) -> dict:
    """The engine serving `model_id` (a base id or a `base:profile` id).

    Take it after the measurement, not before: Splash reports a model as
    served, and the memory budget it planned, only once it has started.
    "serving" false on a splash result means oMLX was set to Splash and did
    not run it, so the numbers are not Splash's.
    """
    base = model_id.split(":", 1)[0]
    snap: dict = {"engine": "mlx", "omlx_version": omlx_version()}
    try:
        settings = o.settings(base)
        glob = o.global_settings()
    except Exception as e:  # noqa: BLE001
        snap["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        return snap
    external = settings.get("external_engine")
    if not external:
        return snap
    snap["engine"] = external
    if external != "splash":
        return snap
    status = (glob.get("external_engines") or {}).get("splash") or {}
    integrations = glob.get("integrations") or {}
    served = next(
        (m for m in status.get("models") or [] if m.get("id") == base), None
    )
    budget = (served or {}).get("memory_budget_bytes")
    memory = (served or {}).get("memory_bytes")
    snap["splash"] = {
        "version": status.get("version"),
        "draft_model": settings.get("dflash_draft_model"),
        "kv_format": integrations.get("splash_kv_format"),
        "ane_prefill": integrations.get("splash_ane_prefill"),
        "max_memory_gb": integrations.get("splash_max_memory_gb"),
        "ssd_cache_gb": integrations.get("splash_ssd_cache_gb"),
        "serving": served is not None,
        "memory_budget_gb": round(budget / GIB, 2) if budget else None,
        "process_memory_gb": round(memory / GIB, 2) if memory else None,
    }
    return snap


def engine_tag(run: dict) -> str:
    """A short engine label for a result file, for report tables.

    "mlx" for in-process runs, including every file written before the
    "engine" field existed; "splash 1.6.0 int8" and the like otherwise, with
    "no-ane" or "NOT SERVING" appended where they apply.
    """
    engine = run.get("engine")
    if not isinstance(engine, dict):
        return (run.get("variant") or {}).get("external_engine") or "mlx"
    name = engine.get("engine") or "mlx"
    if name != "splash":
        return name
    sp = engine.get("splash") or {}
    parts = ["splash", sp.get("version"), sp.get("kv_format")]
    if sp.get("ane_prefill") is False:
        parts.append("no-ane")
    if sp.get("serving") is False:
        parts.append("NOT SERVING")
    return " ".join(str(p) for p in parts if p)
