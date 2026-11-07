#!/bin/bash
# Flint, livecodebench at 500 on the current harness (FINDINGS §42), 2026-11-06.
#
#   A  flint-mixed-3-4bit beside the lineup              ~3.3-4.5 h
#   B  flint-oQ8e in the 8-bit's slot (8-bit unloaded)    ~3.6-5 h
#
# Both run as shipped: --thinking (flint has no switch) and model_settings
# sampling (1.0 / 0.95 / 20 from generation_config; greedy is not validated).
# oMLX caps livecodebench at 16,384 tokens; truncations are in the result JSON
# as finish_reason "length". Both upload to omlx.ai (500 >= 100), on purpose.
#
# During B, coder/reasoner/long have no model behind them. Pause Hermes first.
# A request to those roles reloads the 8-bit mid-run; the state check after B
# records whether that happened.
#
# The EXIT trap unloads both flints and reloads the 8-bit however the run ends.
set -u
cd "$(dirname "$0")/.." || exit 1
note() { printf '\n======== %s  [%s]\n' "$1" "$(date '+%Y-%m-%d %H:%M:%S')"; }
lineup() {
  python3 - <<'PY'
import sys; sys.path.insert(0, "bench")
from omlx_client import Omlx
print("  loaded:", [m["id"] for m in Omlx().models() if m.get("loaded")])
PY
}
finish() {
  note "restore"
  python3 - <<'PY'
import sys, time; sys.path.insert(0, "bench")
from omlx_client import Omlx
o = Omlx()
for m in ("flint-oQ8e", "flint-mixed-3-4bit"):
    try:
        o.unload(m)
    except Exception:
        pass
time.sleep(10)
o.load("Qwen3.8-27B-8bit")
PY
  lineup
  note "end"
}
trap finish EXIT

note "A: flint-mixed-3-4bit, livecodebench 500"
lineup
python3 -u bench/accuracy.py code --suites livecodebench --samples 500 --batch 8 \
  --thinking --sampling model_settings --model flint-mixed-3-4bit \
  --label flint_mixed_lcb500 || note "A failed"
python3 - <<'PY'
import sys; sys.path.insert(0, "bench")
from omlx_client import Omlx
Omlx().unload("flint-mixed-3-4bit")
PY

note "B: flint-oQ8e in the 8-bit's slot, livecodebench 500"
python3 - <<'PY'
import sys, time; sys.path.insert(0, "bench")
from omlx_client import Omlx
o = Omlx()
o.unload("Qwen3.8-27B-8bit")
for _ in range(90):
    if "Qwen3.8-27B-8bit" not in [m["id"] for m in o.models() if m.get("loaded")]:
        break
    time.sleep(2)
PY
lineup
python3 -u bench/accuracy.py code --suites livecodebench --samples 500 --batch 8 \
  --thinking --sampling model_settings --model flint-oQ8e \
  --label flint_oq8e_lcb500 || note "B failed"
note "B: state after the run (an 8-bit here means a client reloaded it mid-run)"
lineup
