#!/bin/bash
# Qwen3.6-35B-A3B-MLX-8bit, first measurement here, 2026-11-10 overnight.
# Starts when bench/splash_round2.sh has finished (pass its PID as $1).
#
#   Q0  gate beside the lineup and in gemma-26B-A4B's slot, MLX then Splash
#   Q1  throughput, MLX and Splash
#   Q2  Splash sanity: own sampling, thinking off on request
#   Q3  code at 500, Splash            Q4  reason at 500, Splash
#   Q5  retrieval at 64k and 131k      Q6  general and safety at 500, Splash
#   Q7  reason at 500 on MLX, the engine control
#
# Deterministic sampling, thinking off, like every baseline. Suites of 500
# upload to omlx.ai, approved. A step that would end after $CUTOFF (default 06:15) is skipped.
set -u
cd "$(dirname "$0")/.." || exit 1
Q=Qwen3.6-35B-A3B-MLX-8bit
FAST=gemma-4-26B-A4B-it-oQ6e-mtp
ADD=gemma-4-31B-it-qat-OptiQ-4bit
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
qvariant() { python3 -u bench/set_variant.py apply "$1" --model "$Q" --no-reload; }
acc() {  # label group
  python3 -u bench/accuracy.py "$2" --model "$Q" --label "$1" --samples 500 --batch 8 \
    || note "accuracy $1 FAILED"
}

if [ -n "${1:-}" ]; then
  note "waiting for PID $1 (splash_round2.sh)"
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
print("  loaded:", [m["id"] for m in o.models() if m.get("loaded")])
PY
  python3 -u bench/set_variant.py show
  python3 -u bench/doctor.py --quick --layer engine --out results/doctor_after_q36_round.json
  python3 bench/run_plan.py clear
  note "end"
}
trap finish EXIT

python3 bench/run_plan.py start q36-round \
  --step "q36_8bit_splash_code|$Q|humaneval,mbpp,livecodebench|500" \
  --step "q36_8bit_splash_reason|$Q|mmlu_pro,gsm8k,mathqa|500" \
  --step "q36_8bit_splash_general|$Q|mmlu,arc_challenge,hellaswag,winogrande,truthfulqa|500" \
  --step "q36_8bit_splash_safety|$Q|safetybench,bbq|500" \
  --step "q36_8bit_mlx_reason|$Q|mmlu_pro,gsm8k,mathqa|500"

note "Q0a: gate on MLX, beside the lineup, then in the fast slot"
qvariant plain
python3 -u bench/candidate_gate.py "$Q" --load-first "$FAST" "$ADD" --label gate_q36_mlx_beside
python3 -u bench/candidate_gate.py "$Q" --load-first "$FAST" "$ADD" --replaces "$FAST" \
  --label gate_q36_mlx_replaces_fast
note "Q1a: throughput, MLX"
python3 -u bench/throughput.py q36_8bit_mlx --model "$Q" --batch 2 4

note "Q0b: gate on Splash, beside the lineup, then in the fast slot"
qvariant splash
python3 -u bench/candidate_gate.py "$Q" --load-first "$FAST" "$ADD" --label gate_q36_splash_beside
python3 -u bench/candidate_gate.py "$Q" --load-first "$FAST" "$ADD" --replaces "$FAST" \
  --label gate_q36_splash_replaces_fast
note "Q1b: throughput, Splash"
python3 -u bench/throughput.py q36_8bit_splash --model "$Q" --batch 2 4
note "Q2: Splash sanity"
python3 -u bench/engine_checks.py q36_8bit_splash --model "$Q" || note "Q2 checks FAILED"

fits 180 "Q3 code"   && { note "Q3: code, Splash";   acc q36_8bit_splash_code code; }
fits 45  "Q4 reason" && { note "Q4: reason, Splash"; acc q36_8bit_splash_reason reason; }
fits 20  "Q5 longctx" && { note "Q5: long context, Splash"
  python3 -u bench/longctx_quality.py --model "$Q" --lengths 65536 131072 \
    --label longctx_q36_8bit_splash; }
fits 20  "Q6 general" && { note "Q6: general, Splash"; acc q36_8bit_splash_general general; }
fits 10  "Q6 safety"  && { note "Q6: safety, Splash";  acc q36_8bit_splash_safety safety; }
fits 60  "Q7 MLX reason" && { note "Q7: reason, MLX (engine control)"
  qvariant plain && acc q36_8bit_mlx_reason reason; }
