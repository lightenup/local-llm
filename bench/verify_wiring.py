#!/usr/bin/env python3
"""Confirm the opencode agent wiring actually resolves and behaves.

B1b found that `opencode.json` listed only the four 27B profiles, so profiles
oMLX served were unreachable to every client. Wiring is not done when the config
is edited; it is done when a call through the configured path returns something
sane. This checks that, for every agent, against the provider opencode actually
uses -- which is oMLX directly (`omlx/...`), NOT the LiteLLM proxy.

Three things are checked per agent, because each has bitten this repo:

  resolves   the served model id exists in /v1/models. A typo here is silent
             until the agent is used.
  answers    a real completion comes back non-empty.
  clean      the reply does not open with internal monologue. Calling a
             qwen3_5 checkpoint with no profile leaks "We need answer user's
             request: ..." into content (§28), and a mis-set reasoning_parser
             does the same thing through a profile.

Also verifies that each agent's declared context limit does not exceed what the
model can actually serve, because opencode will happily pack a context oMLX then
refuses (FINDINGS §32).
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from omlx_client import Omlx  # noqa: E402

CFG = Path(os.environ.get("OPENCODE_CONFIG",
                          "~/.config/opencode/opencode.json")).expanduser()
LEAK = re.compile(r"^\s*(we need|we must|the user wants|okay,? the user|"
                  r"let me (analyz|think)|first,? (i|we) (need|should))", re.I)
# Verified ceilings. Anything not listed is not checked; anything listed is.
SERVABLE = {"gemma-4-31B-it-qat-OptiQ-4bit": 122880,  # §40
            "gemma-4-26B-A4B-it-oQ6e-mtp": 131072}  # §41


def main() -> int:
    if not CFG.exists():
        print(f"!! no config at {CFG}")
        return 2
    d = json.loads(CFG.read_text())
    o = Omlx()
    served = {m["id"] for m in o.get("/v1/models", admin=False)["data"]}
    models = (d.get("provider", {}).get("omlx", {}) or {}).get("models", {})

    agents = {n: a.get("model") for n, a in (d.get("agent") or {}).items()}
    agents["(default)"] = d.get("model")
    agents["(small_model)"] = d.get("small_model")

    print(f"{'agent':16s} {'served id':46s} {'resolves':>9} {'answers':>8} {'clean':>6}")
    print("-" * 92)
    problems = 0
    checked: dict[str, tuple[bool, bool]] = {}
    for name, ref in sorted(agents.items()):
        if not ref:
            continue
        mid = ref.split("/", 1)[1] if "/" in ref else ref
        ok_resolve = mid in served
        if mid in checked:
            ok_answer, ok_clean = checked[mid]
        elif not ok_resolve:
            ok_answer = ok_clean = False
        else:
            try:
                # Reasoning profiles spend a small budget thinking and return
                # EMPTY content with the text in `reasoning_content`. A 24-token
                # probe scored both `plan` and the untouched `speckit` as "does
                # not answer", which was the checker being wrong, not the
                # wiring. Budget generously and accept either field.
                r = o.chat([{"role": "user",
                             "content": "Reply with exactly: ready"}],
                           model=mid, max_tokens=512)
                msg = r["choices"][0]["message"]
                txt = (msg.get("content") or "").strip()
                think = (msg.get("reasoning_content") or "").strip()
                ok_answer = bool(txt or think)
                # A leak is monologue in CONTENT. The same words in
                # reasoning_content are the field doing its job, so only
                # content is tested (§28's failure was content specifically).
                ok_clean = not bool(LEAK.match(txt))
            except Exception as e:  # noqa: BLE001
                print(f"  {name}: {type(e).__name__}: {str(e)[:100]}")
                ok_answer = ok_clean = False
            checked[mid] = (ok_answer, ok_clean)
        flag = lambda b: "yes" if b else "NO"  # noqa: E731
        print(f"{name:16s} {mid[:46]:46s} {flag(ok_resolve):>9} "
              f"{flag(ok_answer):>8} {flag(ok_clean):>6}")
        problems += (not ok_resolve) + (not ok_answer) + (not ok_clean)

    print("\ndeclared context limits vs what the model can serve:")
    for mid, meta in sorted(models.items()):
        base = mid.split(":", 1)[0]
        cap = SERVABLE.get(base)
        declared = ((meta.get("limit") or {}).get("context"))
        if cap is None:
            print(f"  {mid[:52]:52s} declared {declared}  (no verified ceiling on record)")
            continue
        bad = declared and declared > cap
        problems += bool(bad)
        print(f"  {mid[:52]:52s} declared {declared} vs verified {cap}"
              f"{'   !! EXCEEDS' if bad else '   ok'}")

    print(f"\n{'PROBLEMS: ' + str(problems) if problems else 'wiring is consistent'}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
