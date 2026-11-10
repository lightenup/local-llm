#!/bin/bash
# Splash round 2 on the 8-bit (FINDINGS §44 left these open), 2026-11-10.
#
#   A  coexistence with the lineup, Splash then MLX                ~20 min
#   B  prefix cache: cold, repeat, churn, engine reload; both      ~30 min
#   C  long-context retrieval: Splash 64k/128k, Splash sized for
#      256k, MLX 64k/128k on this build                            ~45 min
#   D  throughput A/B: bf16 KV, ANE prefill off                    ~15 min
#   E  accuracy at 500 per suite, deterministic, thinking off:
#      Splash int8 on reason, general, safety, code; Splash bf16
#      on reason; MLX plain on reason as the same-build control    ~6-8 h
#
# Every suite of 500 uploads to omlx.ai (>= 100 questions), approved.
# The accuracy suites unload every other model while they run.
# The EXIT trap puts the shipped plain variant back however the run ends.
set -u
cd "$(dirname "$0")/.." || exit 1
note() { printf '\n======== %s  [%s]\n' "$1" "$(date '+%Y-%m-%d %H:%M:%S')"; }
variant() { python3 -u bench/set_variant.py apply "$1" || { note "variant $1 FAILED"; return 1; }; }
finish() {
  note "restore shipped state"
  python3 -u bench/set_variant.py apply plain
  python3 -u profiles/apply.py --verify-only
  python3 -u bench/doctor.py --quick --layer engine \
    --out results/doctor_after_splash_round2.json
  python3 bench/run_plan.py clear
  note "end"
}
trap finish EXIT

python3 bench/run_plan.py start splash-round2 \
  --step "splash_8bit_reason|Qwen3.8-27B-8bit|mmlu_pro,gsm8k,mathqa|500" \
  --step "splash_8bit_general|Qwen3.8-27B-8bit|mmlu,arc_challenge,hellaswag,winogrande,truthfulqa|500" \
  --step "splash_8bit_safety|Qwen3.8-27B-8bit|safetybench,bbq|500" \
  --step "splash_8bit_bf16_reason|Qwen3.8-27B-8bit|mmlu_pro,gsm8k,mathqa|500" \
  --step "mlx_8bit_plain_074_reason|Qwen3.8-27B-8bit|mmlu_pro,gsm8k,mathqa|500" \
  --step "splash_8bit_code|Qwen3.8-27B-8bit|humaneval,mbpp,livecodebench|500"

note "A1: coexistence, Splash"
variant splash && python3 -u bench/coexist.py --label coexist_splash_8bit
note "A2: coexistence, MLX plain"
variant plain && python3 -u bench/coexist.py --label coexist_mlx_8bit_plain_074

note "B1: prefix cache, Splash"
variant splash && python3 -u bench/cache_ab.py --arms baseline --post-reload \
  --label cache_splash_8bit
note "B2: prefix cache, MLX plain"
variant plain && python3 -u bench/cache_ab.py --arms baseline --post-reload \
  --label cache_mlx_8bit_plain_074

note "C1: long context, Splash at its default 128K plan"
variant splash && python3 -u bench/longctx_quality.py --lengths 65536 131072 \
  --label longctx_splash_8bit
note "C2: long context, Splash sized for 256K"
variant splash_262k && python3 -u bench/longctx_quality.py --only-length 262144 \
  --label longctx_splash_8bit_262k
note "C3: long context, MLX plain on this build"
variant plain && python3 -u bench/longctx_quality.py --lengths 65536 131072 \
  --label longctx_mlx_8bit_plain_074

note "D1: throughput, Splash bf16 KV"
variant splash_bf16 && python3 -u bench/throughput.py splash_8bit_bf16 --batch 2 4
note "D2: throughput, Splash ANE prefill off"
variant splash_noane && python3 -u bench/throughput.py splash_8bit_noane --batch 2 4

acc() {  # label group
  python3 -u bench/accuracy.py "$2" --label "$1" --samples 500 --batch 8 \
    || note "accuracy $1 FAILED"
}
note "E1: accuracy, Splash int8"
if variant splash; then
  acc splash_8bit_reason reason
  acc splash_8bit_general general
  acc splash_8bit_safety safety
fi
note "E2: accuracy, Splash bf16 KV, reason"
variant splash_bf16 && acc splash_8bit_bf16_reason reason
note "E3: accuracy, MLX plain on this build, reason (control)"
variant plain && acc mlx_8bit_plain_074_reason reason
note "E4: accuracy, Splash int8, code (longest last)"
variant splash && acc splash_8bit_code code
