#!/bin/bash
# Backlog A12: Qwen3.6-35B-A3B for the 8-bit's roles, 2026-11-11 overnight.
#
#   T1  gate in the 8-bit's slot (gemma-26B-A4B and gemma-31B resident), MLX
#       then Splash
#   T2  profile checks on MLX: client temperature, thinking per profile, budget
#   T3  retrieval through q36-long at 64k/131k, and the bare id at 262k, MLX
#   T4  taskbench q36-code and q36-agent, 5 repeats, MLX
#   T5  taskbench controls on this build: the 8-bit's coder, then its agent
#   T6  Splash: profile checks and taskbench q36-agent
#
# Taskbench uses each profile's own sampling, through LiteLLM. Nothing here
# uploads. A step that would end after $CUTOFF (default 06:15) is skipped.
set -u
cd "$(dirname "$0")/.." || exit 1
Q=Qwen3.6-35B-A3B-MLX-8bit
B=Qwen3.8-27B-8bit
FAST=gemma-4-26B-A4B-it-oQ6e-mtp
ADD=gemma-4-31B-it-qat-OptiQ-4bit
CUTOFF=${CUTOFF:-06:15}
DEADLINE=$(date -j -v+1d -f '%H:%M:%S' "$CUTOFF:00" +%s)
[ "$(date +%H)" -lt 12 ] && DEADLINE=$(date -j -f '%H:%M:%S' "$CUTOFF:00" +%s)
note() { printf '\n======== %s  [%s]\n' "$1" "$(date '+%Y-%m-%d %H:%M:%S')"; }
fits() {  # minutes label
  if [ $(( $(date +%s) + $1 * 60 )) -gt "$DEADLINE" ]; then
    note "SKIP $2: ~$1 min would pass $CUTOFF"; return 1
  fi
}
qvariant() { python3 -u bench/set_variant.py apply "$1" --model "$Q" --no-reload; }
task() {  # model label
  python3 -u bench/taskbench.py run --model "$1" --label "$2" --repeats 5 \
    || note "taskbench $2 FAILED"
}

finish() {
  note "restore"
  qvariant plain
  python3 - "$Q" "$B" <<'PY'
import sys; sys.path.insert(0, "bench")
from omlx_client import Omlx
o = Omlx()
try:
    o.unload(sys.argv[1])
except Exception:
    pass
o.load(sys.argv[2])
print("  loaded:", [m["id"] for m in o.models() if m.get("loaded")])
PY
  python3 -u bench/set_variant.py apply plain
  python3 -u profiles/apply.py --verify-only
  python3 -u profiles/apply.py --set qwen36 --verify-only
  python3 -u bench/doctor.py --quick --layer engine --out results/doctor_after_a12.json
  note "end"
}
trap finish EXIT

note "T1a: gate in the 8-bit's slot, MLX"
qvariant plain && python3 -u bench/candidate_gate.py "$Q" --load-first "$B" "$FAST" "$ADD" \
  --replaces "$B" --label gate_q36_mlx_replaces_8bit
note "T1b: gate in the 8-bit's slot, Splash"
qvariant splash && python3 -u bench/candidate_gate.py "$Q" --load-first "$B" "$FAST" "$ADD" \
  --replaces "$B" --label gate_q36_splash_replaces_8bit
qvariant plain
python3 - "$Q" <<'PY'
import sys; sys.path.insert(0, "bench")
from omlx_client import Omlx
try:
    Omlx().unload(sys.argv[1])
except Exception:
    pass
PY

fits 10 "T2 checks" && { note "T2: profile checks, MLX"
  python3 -u bench/engine_checks.py q36_mlx_profiles --model "$Q" || note "T2 checks FAILED"; }

fits 20 "T3 longctx" && { note "T3: retrieval through q36-long, then the bare id at 262k, MLX"
  python3 -u bench/longctx_quality.py --model "$Q:q36-long" --lengths 65536 131072 \
    --label longctx_q36_mlx_profile
  python3 -u bench/longctx_quality.py --model "$Q" --only-length 262144 \
    --label longctx_q36_mlx_262k; }

fits 30 "T4 q36 code"  && { note "T4a: taskbench q36-code, MLX";  task "$Q:q36-code"  a12_q36_code; }
fits 30 "T4 q36 agent" && { note "T4b: taskbench q36-agent, MLX"; task "$Q:q36-agent" a12_q36_agent; }

fits 75 "T5 coder control" && { note "T5a: taskbench coder (8-bit qwen-code), this build"
  task coder a12_coder; }
fits 75 "T5 agent control" && { note "T5b: taskbench 8-bit qwen-agent, this build"
  task "$B:qwen-agent" a12_8bit_agent; }

fits 40 "T6 Splash" && { note "T6: Splash, profile checks and taskbench q36-agent"
  qvariant splash && {
    python3 -u bench/engine_checks.py q36_splash_profiles --model "$Q" || note "T6 checks FAILED"
    task "$Q:q36-agent" a12_q36_agent_splash; }; }
