#!/bin/bash
# A10, day window (2026-10-20): what is left before the lineup can switch.
#
# Night 2 (FINDINGS §41) left the gemma-26B MoE ahead of oQ4e on every public
# suite and level with gemma-31B, but behind `fast` on taskbench reliability:
# 76/88 attempts against 87/88, p = 0.016 per attempt, p = 0.25 per task. Two
# repeats cannot tell a weaker model from sampling noise, so this run takes
# five, and adds the coder-role arms so `coder` is decided on the same night:
#
#   1  taskbench, 5 repeats: fast slot  g4m-agent vs `fast` (oq-agent)
#                            coder slot g4m-code vs `coder` (8-bit qwen-code)
#                                       vs g4-code (gemma-31B)
#   2  concurrency scaling 1/2/4/8 on g4magent and oqagent (fast takes
#      parallel subagent calls)
#   3  context, last because it may crash oMLX: the MoE's admitted ceiling
#      above 131k (prefill guard, chunked_prefill as it stands), then oQ4e at
#      131k as the reference for the MoE's one multi-hop miss.
#
# Restores the MoE profiles and resumes Hermes at exit.
set -u
cd "$(dirname "$0")/.." || exit 1
MOE="gemma-4-26B-A4B-it-oQ6e-mtp"
G31="gemma-4-31B-it-qat-OptiQ-4bit"
OQ4E="Qwen3.8-27B-oQ4e-mtp"
R=5
note() { printf '\n======== %s  [%s]\n' "$1" "$(date '+%H:%M:%S')"; }
finish() {
  note "restore"
  python3 profiles/apply.py --set gemma4moe 2>&1 | tail -1
  if [ -n "${HERMES_RESUME_CMD:-}" ]; then
    eval "$HERMES_RESUME_CMD" || echo "  !! could not resume Hermes"
  fi
  note "end"
}
trap finish EXIT

note "1a: taskbench fast slot, $R repeats"
python3 -u bench/taskbench.py run --model "$MOE:g4m-agent" --label a10d3_g4moe_agent --repeats $R \
  || note "1a MoE failed"
python3 -u bench/taskbench.py run --model fast --label a10d3_fast_agent --repeats $R \
  || note "1a fast failed"
python3 bench/taskbench.py compare a10d3_fast_agent a10d3_g4moe_agent || true

note "1b: taskbench coder slot, $R repeats"
python3 -u bench/taskbench.py run --model "$MOE:g4m-code" --label a10d3_g4moe_code --repeats $R \
  || note "1b MoE failed"
python3 -u bench/taskbench.py run --model coder --label a10d3_coder --repeats $R \
  || note "1b coder failed"
python3 -u bench/taskbench.py run --model "$G31:g4-code" --label a10d3_g4_code --repeats $R \
  || note "1b gemma-31B failed"
python3 bench/taskbench.py compare a10d3_coder a10d3_g4moe_code || true
python3 bench/taskbench.py compare a10d3_g4_code a10d3_g4moe_code || true

note "2: concurrency scaling, MoE then oQ4e"
python3 -u bench/concurrency.py --experiment scaling --levels 1 2 4 8 --model "$MOE" \
  --profile g4magent && mv results/concurrency.json results/a10d3_conc_g4moe.json \
  || note "2 MoE failed"
python3 -u bench/concurrency.py --experiment scaling --levels 1 2 4 8 --model "$OQ4E" \
  --profile oqagent && mv results/concurrency.json results/a10d3_conc_oq4e.json \
  || note "2 oQ4e failed"
git checkout -- results/concurrency.json 2>/dev/null || true

note "3a: MoE prefill-guard ceiling, 131072..262144"
python3 -u bench/ctx_bisect.py --model "$MOE" --profile g4m-agent --profile-name g4magent \
  --low 131072 --high 262144 --label a10d3_g4moe_bisect || note "3a failed"

note "3b: oQ4e at 131k, reference for the multi-hop miss"
python3 -u bench/longctx_quality.py --model "$OQ4E:oq-long" --lengths 131072 \
  --label a10d3_oq4e_longctx || note "3b failed"
