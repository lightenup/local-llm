#!/bin/bash
# A10 step 3, second night (2026-10-19): what the first run could not settle.
#
# The re-grade (bench/regrade.py) showed the grader changes in oMLX 0.7.1rc1
# altered 1 verdict in ~6,000 stored answers, so every suite is a fair
# comparison EXCEPT livecodebench's 216 functional problems, whose prompt
# changed. Those were only ever asked of the gemma-26B MoE in the new form.
# Cheapest first, so the morning has the decisive numbers even if a late phase
# fails:
#
#   A  context: can the MoE serve what the fast slot is declared for (131,072)?
#   B  taskbench agent categories, MoE g4m-agent vs `fast` (oq-agent), same night
#   C  oQ4e livecodebench on today's harness: the incumbent on the 216
#   D  gemma-31B livecodebench on today's harness, if there is time
#
# Restores the MoE profiles from their definitions and resumes Hermes at exit.
set -u
cd "$(dirname "$0")/.." || exit 1
MOE="gemma-4-26B-A4B-it-oQ6e-mtp"
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

note "A: MoE context, g4m-agent window raised to 135168 for the run"
python3 - <<'PY'
import sys; sys.path.insert(0, "bench")
from omlx_client import Omlx
o = Omlx(); M = "gemma-4-26B-A4B-it-oQ6e-mtp"
s = [dict(p["settings"]) for p in o.list_profiles(M) if p["name"] == "g4magent"][0]
s["max_context_window"] = 135168
o.update_profile("g4magent", model_id=M, settings=s)
print("  g4magent window", s["max_context_window"])
PY
python3 -u bench/longctx_quality.py --model "$MOE:g4m-agent" --lengths 65536 131072 \
  --label a10_g4moe_longctx || note "A failed"

note "B: taskbench, MoE vs fast, 2 repeats each"
python3 bench/taskbench.py run --model "$MOE:g4m-agent" --label a10_g4moe_agent --repeats 2 \
  || note "B (MoE) failed"
python3 bench/taskbench.py run --model fast --label a10_fast_agent --repeats 2 \
  || note "B (fast) failed"
python3 bench/taskbench.py compare a10_fast_agent a10_g4moe_agent || true

note "C: oQ4e livecodebench on the current harness"
python3 bench/accuracy.py code --suites livecodebench --model Qwen3.8-27B-oQ4e-mtp \
  --label oq4e_lcb_rc1 --samples 500 --batch 8 || note "C failed"
python3 bench/paired_compare.py acc_oq4e_lcb_rc1 acc_g4moe_code \
  --save g4moe_code_vs_oq4e_lcb_rc1 || true

note "D: gemma-31B livecodebench on the current harness"
python3 bench/accuracy.py code --suites livecodebench --model gemma-4-31B-it-qat-OptiQ-4bit \
  --label gemma4_lcb_rc1 --samples 500 --batch 8 || note "D failed"
python3 bench/paired_compare.py acc_gemma4_lcb_rc1 acc_g4moe_code \
  --save g4moe_code_vs_gemma4_lcb_rc1 || true
