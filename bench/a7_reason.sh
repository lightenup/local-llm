#!/bin/bash
# A7: the reason arms for both candidates, paired against the shipped 8-bit.
#
# Matches results/acc_ship_reason.json EXACTLY -- same three suites, 500 samples,
# batch 8, thinking off, deterministic sampling -- because paired_compare.py
# compares question by question and any difference in those parameters compares
# two different experiments. The reference arms were run thinking=False
# (results/overnight_2026-09-06.log line 48), so these are too, and the profiles
# built in 3a0658a are deliberately NOT exercised here: the comparison has to
# isolate the checkpoint.
#
# enable_thinking is sent explicitly in the request body by
# omlx_client.bench_accuracy, so OptiQ's template default of ON and gemma-4's of
# OFF are both overridden. That is the 28 trap (the 69.7-vs-82.8 MMLU-Pro split
# on omlx.ai) and it is closed by construction here, not by luck.
#
# POWER, measured from the stored per-question results rather than assumed:
# mmlu_pro runs ~10% discordant at n=500, so the floor is ~2.9 points.
#   gemma's claimed +11.0 vs the 8-bit   -> decisively detectable
#   OptiQ's claimed  +1.3 vs the 8-bit   -> NOT detectable, would need n~2400
#   OptiQ's          +3.9 vs oQ4e        -> detectable
# So this settles gemma outright, and for OptiQ answers the question that
# actually decides a tier: does it MATCH the 8-bit while BEATING oQ4e at 68% of
# the 8-bit's size. A null result against the 8-bit is a real answer here, but
# only if it is read as "within 2.9 points", never as "identical".
#
# UPLOADS: 500 >= 100, so every suite uploads to omlx.ai with a machine hash and
# model settings (FINDINGS 11). Accepted, consistent with every arm it is being
# compared against -- running these at 99 to stay local would make them
# incomparable, which is the whole point of the run.
set -u
cd "$(dirname "$0")/.." || exit 1

SAMPLES="${SAMPLES:-500}"
BATCH="${BATCH:-8}"
OPTIQ="Qwen3.8-27B-OptiQ-4bit"
GEMMA="gemma-4-31B-it-qat-OptiQ-4bit"

note() { printf '\n======== %s  [%s]\n' "$1" "$(date '+%H:%M:%S')"; }

note "state before"
python3 - <<'PY'
import sys
sys.path.insert(0, "bench")
from omlx_client import Omlx
o = Omlx()
print("  loaded:", [m["id"] for m in o.models() if m.get("loaded")])
print("  pool  : %.2f GiB" % (o.health()["engine_pool"]["current_model_memory"] / 2**30))
PY

note "1. OptiQ reason (backlog A2b: does KL-driven allocation recover reasoning?)"
python3 bench/accuracy.py reason --model "$OPTIQ" --label optiq_reason \
  --samples "$SAMPLES" --batch "$BATCH" || note "optiq reason failed"

note "2. gemma-4-31B reason (the +11 mmlu_pro claim)"
python3 bench/accuracy.py reason --model "$GEMMA" --label gemma4_reason \
  --samples "$SAMPLES" --batch "$BATCH" || note "gemma reason failed"

note "3. paired comparisons"
# Against the shipped 8-bit first -- that is the tier either would replace --
# then against oQ4e, which is the arm OptiQ has to beat to be worth its extra
# 3 GiB over the existing fast tier.
for pair in "acc_ship_reason acc_optiq_reason" \
            "acc_oq4e_reason acc_optiq_reason" \
            "acc_ship_reason acc_gemma4_reason" \
            "acc_optiq_reason acc_gemma4_reason"; do
  set -- $pair
  echo ""
  echo "--- $1 vs $2"
  python3 bench/paired_compare.py "$1" "$2" \
    --save "$(echo "$2" | sed 's/^acc_//')_vs_$(echo "$1" | sed 's/^acc_//')" \
    || echo "    (comparison failed or an arm is missing)"
done

note "state after"
python3 - <<'PY'
import sys
sys.path.insert(0, "bench")
from omlx_client import Omlx
o = Omlx()
print("  loaded:", [m["id"] for m in o.models() if m.get("loaded")])
PY
