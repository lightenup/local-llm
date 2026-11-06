"""LiteLLM role aliases -> model and profile, read from proxy/litellm-config.yaml.

Clients call roles (coder, reasoner, fast, long, embed, rerank), not models,
so the dashboard leads with this mapping. Parsed line by line, standard
library only: an entry is `- model_name: X` followed by `model: provider/Y`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

ROLES = ("coder", "reasoner", "fast", "long", "embed", "rerank")
_NAME = re.compile(r"^\s*- model_name:\s*(\S+)")
_MODEL = re.compile(r"^\s+model:\s*(\S+)")
_CTX = re.compile(r"^\s+max_input_tokens:\s*(\d+)")


@dataclass(frozen=True)
class Role:
    role: str
    model: str
    profile: str | None
    max_input_tokens: int | None


def load_roles(config: Path) -> list[Role]:
    """Roles in ROLES order; an absent file or role is simply left out."""
    if not config.is_file():
        return []
    found: dict[str, dict] = {}
    current = None
    for line in config.read_text().splitlines():
        m = _NAME.match(line)
        if m:
            current = m.group(1) if m.group(1) in ROLES else None
            if current:
                found[current] = {}
            continue
        if current is None:
            continue
        m = _MODEL.match(line)
        if m and "model" not in found[current]:
            found[current]["model"] = m.group(1).split("/", 1)[-1]
            continue
        m = _CTX.match(line)
        if m:
            found[current]["ctx"] = int(m.group(1))
    out = []
    for r in ROLES:
        if r in found and "model" in found[r]:
            model, _, profile = found[r]["model"].partition(":")
            out.append(Role(r, model, profile or None, found[r].get("ctx")))
    return out
