#!/usr/bin/env bash
# Overnight validation for the Qwen3.8-27B-8bit profile setup.
#
# What the first session already proved (see docs/FINDINGS.md):
#   - ANE prefill offload loses to GPU-only on M5 Max  -> stays off
#   - vlm_mtp speculative decode is ~1.8x on decode    -> shipped
#   - the 4 profiles are stored, served and switch without an engine reload
#
# What this script settles, highest value first:
#   1. Accuracy floor of the shipped variant
#   2. Whether MTP is output-neutral (the one assumption still untested)
#   3. Whether the code profile should run temp 0.7 (Qwen default) or 0.3
#   4. Prompt-length and batch throughput on the shipped variant
#   5. Whether the reason profile's thinking budget is buying anything, given
#      that it costs the entire MTP win (17.6 vs 31.9 t/s)
#
# SCOPE / RUNTIME
# ---------------
# SAMPLES defaults to 500 per suite, not the full datasets. Full datasets are
# ~101k questions across the passes below, which measured out at 36-44 hours.
# At 500 per suite this is roughly 5-7 hours, plus 1-2 hours for step 5, which
# generates long thinking traces at plain-decode speed in one of its two arms.
# 500 gives about +/-2.2% at 95%
# confidence, which is well inside what is needed to detect an MTP regression
# or a real temperature difference; the full run buys +/-0.9% for 30x the time.
#
# Deliberately NOT run here (they were the bulk of the 36-44 hours):
#   - the thinking-enabled reasoning pass (15-20 h on its own)
#   - the TurboQuant 4-bit KV A/B (context.py already showed the full 262144
#     ceiling is reachable without it, so it has no problem left to solve)
#
# UPLOAD NOTICE
# -------------
# oMLX uploads accuracy results to omlx.ai for any suite of >=100 questions and
# has no opt-out. At SAMPLES=500 that fires. This was explicitly approved. Use
# SAMPLES=99 to keep everything local.
#
# Usage:  bash bench/overnight.sh 2>&1 | tee results/overnight_$(date +%F).log

set -uo pipefail
cd "$(dirname "$0")/.."
mkdir -p results

SAMPLES="${SAMPLES:-500}"
BATCH="${BATCH:-8}"

step() { printf '\n\n======== %s ========\n[%s]\n' "$1" "$(date '+%F %T')"; }

# Step 3 writes base-model sampling, step 2 leaves MTP off, and step 5 toggles
# the reason profile's thinking budget. If this script dies partway through, any
# of those would be left applied and served, so restore the shipped state on any
# exit.
COMPLETED=0
restore() {
  echo
  echo "-- restore trap --"
  python3 - <<'PY' || true
import sys; sys.path.insert(0, "bench")
from omlx_client import Omlx
Omlx().update_settings({"temperature": None, "top_p": None, "top_k": None})
print("base-model sampling cleared; profiles supply sampling again")
PY
  # Only re-apply the variant on an abnormal exit. Step 5 already does it on the
  # happy path, and apply always reloads, so doing it twice would cost another
  # 28 GB load and hand back a cold prefix cache.
  if [ "$COMPLETED" -eq 0 ]; then
    echo "run did not reach the end; re-applying the shipped mtp variant"
    python3 bench/set_variant.py apply mtp || true
    # thinking_budget_ab.py restores the profile itself, but only if it got to
    # run its own cleanup. Rewriting all four from definitions.py is free
    # (universal fields, no reload) and covers a hard kill mid-arm.
    echo "re-applying profiles from definitions.py"
    python3 profiles/apply.py || true
  fi
}
trap restore EXIT

step "0. state before"
python3 bench/set_variant.py show
python3 profiles/apply.py --verify-only

# ---------------------------------------------------------------------------
step "1. accuracy of the shipped variant (mtp, thinking off)"
python3 bench/set_variant.py apply mtp
python3 bench/accuracy.py code    --label ship_code    --samples "$SAMPLES" --batch "$BATCH"
python3 bench/accuracy.py reason  --label ship_reason  --samples "$SAMPLES" --batch "$BATCH"
python3 bench/accuracy.py general --label ship_general --samples "$SAMPLES" --batch "$BATCH"

# ---------------------------------------------------------------------------
step "2. is MTP output-neutral? same suites and sample size, MTP off"
# Speculative decoding is meant to be lossless: the drafter proposes, the target
# model verifies, so accepted tokens should match plain decode. If these scores
# differ beyond noise then the implementation is changing outputs and the 1.8x
# is not free. Same SAMPLES as step 1 so the comparison is like-for-like.
python3 bench/set_variant.py apply plain
python3 bench/accuracy.py code   --label plain_code   --samples "$SAMPLES" --batch "$BATCH"
python3 bench/accuracy.py reason --label plain_reason --samples "$SAMPLES" --batch "$BATCH"

# ---------------------------------------------------------------------------
step "3. code profile sampling A/B (0.7 Qwen default vs 0.3 deterministic)"
# --sampling model_settings makes the suite honour the model's configured
# sampling, so this measures what a profile would actually serve.
python3 bench/set_variant.py apply mtp
for T in 0.7 0.3; do
  python3 - "$T" <<'PY'
import sys
sys.path.insert(0, "bench")
from omlx_client import Omlx
t = float(sys.argv[1])
Omlx().update_settings({"temperature": t, "top_p": 0.8, "top_k": 20})
print(f"-- model sampling set to temperature={t}")
PY
  python3 bench/accuracy.py code --label "code_temp${T/./}" \
      --samples "$SAMPLES" --batch "$BATCH" --sampling model_settings
done
python3 - <<'PY'
import sys; sys.path.insert(0, "bench")
from omlx_client import Omlx
Omlx().update_settings({"temperature": None, "top_p": None, "top_k": None})
print("-- base model sampling reset to None (profiles supply it)")
PY

# ---------------------------------------------------------------------------
step "4. throughput sweep on the shipped variant"
python3 bench/throughput.py mtp_full --lengths 1024 4096 16384 65536 131072 --gen 256
python3 bench/throughput.py mtp_batch --lengths 4096 --gen 256 --batch 2 4 8

# ---------------------------------------------------------------------------
step "5. reason profile thinking-budget A/B (throughput vs runaway risk)"
# FINDINGS section 7 measured the cost (17.6 t/s with the budget, 31.9 without)
# but not the risk it covers. This runs hard prompts through both arms of the
# reason profile and reports reasoning-token counts and truncations, so the
# decision rests on whether an unbudgeted run ever fails to reach an answer.
# The script restores the shipped profile itself; the EXIT trap covers a kill.
# repeats=2 because thinking length varies a lot run to run (411 against 192
# tokens on the same prompt in a trial pair) and the rule turns on the maximum.
python3 bench/thinking_budget_ab.py --repeats 2

# ---------------------------------------------------------------------------
step "6. restore shipped state and re-verify"
python3 bench/set_variant.py apply mtp
python3 profiles/apply.py --verify-only
python3 bench/smoke.py

COMPLETED=1
step "done"
echo "compare results with: python3 bench/report.py"
