#!/usr/bin/env bash
# Run the safety suites (safetybench, bbq) after the overnight validation ends.
#
# WHY THIS IS A SEPARATE SCRIPT
# -----------------------------
# bash reads a script incrementally as it executes, so editing overnight.sh
# while a run is in flight risks corrupting the steps it has not yet parsed.
# This chains onto the running job instead by waiting on its pid. The permanent
# step belongs in overnight.sh and should be added once no run is active.
#
# WHY THESE SUITES
# ----------------
# safetybench and bbq are already declared in bench/accuracy.py's GROUPS and
# ship in the oMLX bundle (omlx/eval/{safetybench,bbq}.py), but overnight.sh
# never ran them, so the shipped variant has no safety measurement at all.
# Both are multiple-choice, so at 500 samples this is roughly 15 minutes -
# unlike the generation-bound code suites, where livecodebench alone costs
# ~50 s per question.
#
# ORDERING
# --------
# Waiting for the whole run to exit means this lands after overnight.sh step 6
# has re-applied the shipped mtp variant, so the scores are attributable to the
# shipped configuration. accuracy.py records the engine variant in its payload
# either way, which also covers the case where the run died early and the EXIT
# trap restored the variant.
#
# UPLOAD NOTICE
# -------------
# Same as the overnight run: oMLX uploads any suite of >=100 questions to
# omlx.ai with no opt-out. Use SAMPLES=99 to stay local.
#
# Usage:  WAIT_PID=32288 bash bench/followup_safety.sh
#         bash bench/followup_safety.sh          # run immediately, no waiting

set -uo pipefail
cd "$(dirname "$0")/.."

WAIT_PID="${WAIT_PID:-}"
SAMPLES="${SAMPLES:-500}"
BATCH="${BATCH:-8}"

stamp() { date '+%F %T'; }

if [ -n "$WAIT_PID" ]; then
  echo "[$(stamp)] waiting for overnight run (pid $WAIT_PID) to exit"
  while kill -0 "$WAIT_PID" 2>/dev/null; do
    sleep 60
  done
  echo "[$(stamp)] overnight run exited"
  # The queue drains before overnight.sh returns, but give the server a moment
  # to finish unloading/reloading before adding work.
  sleep 30
fi

echo "[$(stamp)] engine variant these scores belong to:"
python3 bench/set_variant.py show

python3 bench/accuracy.py safety --label ship_safety \
    --samples "$SAMPLES" --batch "$BATCH"
rc=$?

echo "[$(stamp)] safety suites finished (exit $rc)"
echo "compare everything with: python3 bench/report.py"
exit "$rc"
