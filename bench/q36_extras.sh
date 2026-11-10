#!/bin/bash
# Additions after bench/q36_round.sh, 2026-11-10 overnight (pass its PID as $1).
#
#   X0  Qwen3.6 Splash gate in fast's slot with the 8-bit resident (the round's
#       Splash gates ran after a throughput run had unloaded the 8-bit)
#   X1  the 8-bit: Splash int8 reason at 500 again, the run-to-run noise floor
#   X2  the 8-bit: concurrency 1/2/4, cold then warm prefix cache, Splash and MLX
#   X3  Qwen3.6 on Splash sized for 262,144: retrieval at 262k
#   X4  Qwen3.6 on MLX: general and safety at 500, then code at 500
#
# Deterministic, thinking off; suites of 500 upload to omlx.ai, approved.
# A step that would end after $CUTOFF (default 06:15) is skipped.
set -u
cd "$(dirname "$0")/.." || exit 1
Q=Qwen3.6-35B-A3B-MLX-8bit
# The machine is free until 06:30; the cut-off leaves the restore 15 minutes.
CUTOFF=${CUTOFF:-06:15}
DEADLINE=$(date -j -v+1d -f '%H:%M:%S' "$CUTOFF:00" +%s)
[ "$(date +%H)" -lt 12 ] && DEADLINE=$(date -j -f '%H:%M:%S' "$CUTOFF:00" +%s)
note() { printf '\n======== %s  [%s]\n' "$1" "$(date '+%Y-%m-%d %H:%M:%S')"; }
fits() {  # minutes label
  if [ $(( $(date +%s) + $1 * 60 )) -gt "$DEADLINE" ]; then
    note "SKIP $2: ~$1 min would pass $CUTOFF"; return 1
  fi
}
variant() { python3 -u bench/set_variant.py apply "$@" || { note "variant $* FAILED"; return 1; }; }
acc() {  # label group [model]
  python3 -u bench/accuracy.py "$2" --model "${3:-Qwen3.8-27B-8bit}" --label "$1" \
    --samples 500 --batch 8 || note "accuracy $1 FAILED"
}
conc() {  # label-stem
  local salt="$1-$(date +%s)-"
  python3 -u bench/concurrency.py --experiment scaling --levels 1 2 4 \
    --salt "$salt" --label "concurrency_${1}_cold"
  python3 -u bench/concurrency.py --experiment scaling --levels 1 2 4 \
    --salt "$salt" --label "concurrency_${1}_warm"
}

if [ -n "${1:-}" ]; then
  note "waiting for PID $1 (q36_round.sh)"
  while kill -0 "$1" 2>/dev/null; do sleep 60; done
fi

finish() {
  note "restore"
  python3 -u bench/set_variant.py apply plain --model "$Q" --no-reload
  python3 - "$Q" <<'PY'
import sys; sys.path.insert(0, "bench")
from omlx_client import Omlx
o = Omlx()
try:
    o.unload(sys.argv[1])
except Exception:
    pass
o.load("Qwen3.8-27B-8bit")
PY
  python3 -u bench/set_variant.py apply plain
  python3 -u bench/set_variant.py show
  python3 -u profiles/apply.py --verify-only
  python3 -u bench/doctor.py --quick --layer engine --out results/doctor_after_q36_extras.json
  python3 bench/run_plan.py clear
  note "end"
}
trap finish EXIT

python3 bench/run_plan.py start q36-extras \
  --step "splash_8bit_reason_repeat|Qwen3.8-27B-8bit|mmlu_pro,gsm8k,mathqa|500" \
  --step "q36_8bit_mlx_general|$Q|mmlu,arc_challenge,hellaswag,winogrande,truthfulqa|500" \
  --step "q36_8bit_mlx_safety|$Q|safetybench,bbq|500" \
  --step "q36_8bit_mlx_code|$Q|humaneval,mbpp,livecodebench|500"

FAST=gemma-4-26B-A4B-it-oQ6e-mtp
ADD=gemma-4-31B-it-qat-OptiQ-4bit
fits 10 "X0 Q36 Splash gate" && { note "X0: Qwen3.6 Splash gate in the fast slot, full lineup"
  variant splash --model "$Q" --no-reload && python3 -u bench/candidate_gate.py "$Q" \
    --load-first Qwen3.8-27B-8bit "$FAST" "$ADD" --replaces "$FAST" \
    --label gate_q36_splash_replaces_fast_full
  variant plain --model "$Q" --no-reload
  python3 - "$Q" <<'PY'
import sys; sys.path.insert(0, "bench")
from omlx_client import Omlx
try:
    Omlx().unload(sys.argv[1])
except Exception:
    pass
PY
}

if fits 40 "X1/X2 8-bit Splash"; then
  note "X1: 8-bit, Splash int8 reason, repeat"
  variant splash && acc splash_8bit_reason_repeat reason
  note "X2a: 8-bit concurrency, Splash, cold then warm"
  variant splash && conc splash_8bit
fi
fits 15 "X2b MLX concurrency" && { note "X2b: 8-bit concurrency, MLX plain, cold then warm"
  variant plain && conc mlx_8bit_plain_074; }

fits 30 "X3 Q36 262k" && { note "X3: Qwen3.6, Splash sized for 262k, retrieval at 262k"
  variant splash_262k --model "$Q" --no-reload && python3 -u bench/longctx_quality.py --model "$Q" \
    --only-length 262144 --label longctx_q36_8bit_splash_262k; }

fits 40 "X4a Q36 MLX general+safety" && { note "X4a: Qwen3.6 on MLX, general and safety"
  variant plain --model "$Q" --no-reload && {
    acc q36_8bit_mlx_general general "$Q"; acc q36_8bit_mlx_safety safety "$Q"; }; }
fits 150 "X4b Q36 MLX code" && { note "X4b: Qwen3.6 on MLX, code (longest last)"
  variant plain --model "$Q" --no-reload && acc q36_8bit_mlx_code code "$Q"; }
