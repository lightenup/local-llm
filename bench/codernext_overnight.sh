#!/usr/bin/env bash
# Overnight follow-on to bench/codernext_accuracy.sh. Waits for that run to
# finish, then settles the quality question it opened.
#
# Where it picks up: McNemar on the 500-sample pass already settled the reasoning
# question. mmlu_pro flipped 118 questions, 74 to the 27B against 44 to
# Coder-Next, p=0.00733 -- a real deficit, where comparing the two suite scores
# could not have told you (a difference of independent scores at n=500 carries a
# ~+/-6.2 point band, and the gap was 6.0). gsm8k was a dead tie and mathqa's
# +3.0 was noise.
#
# So Coder-Next is a coder and agent driver, not a replacement: the 27B stays as
# the reasoning tier. Re-running mmlu_pro at its full 12,032 questions would cost
# ~4 h to re-confirm a settled result, so it is not here.
#
# What is on the critical path instead is whether both models can be resident at
# once, because that is what a tier needs. At 8-bit they are 64.7 + 29.5 = 94 GB
# of 128, leaving ~34 GB of KV for both. A 4-bit 27B would make it comfortable --
# and the 27B is now only being asked to reason, which is exactly what
# quant_compare measures. That script has been written, fixed and never run
# (FINDINGS section 10.3).
#
# Phase order: free work first, then the cheap breadth, then the long run.
#
#   1. McNemar on every arm already on disk. No GPU, seconds.
#   2. general and safety on Coder-Next, ~13 min, because a genuine broad
#      reasoning deficit should show up across mmlu, arc_challenge, hellaswag,
#      winogrande and truthfulqa too, not only in mmlu_pro. Seven more paired
#      comparisons for very little GPU.
#   3. quant_compare on the 27B: 4-bit against the shipped 8-bit, throughput and
#      accuracy. ~6-7 h, and it is the one that decides whether the tier fits in
#      memory.
#
# Not included: the oQ 4-bit arm (Jundot/Qwen3.8-27B-oQ4e-mtp) that would make
# A2b the three-way it should be. It needs its drafter wiring checked by hand
# first, and an unverified checkpoint is the wrong thing to hand an unattended
# run. Tonight produces the RTN 4-bit arm; the oQ arm compares against both
# later.
#
#   WAIT_PID=<pid of codernext_accuracy.sh> nohup bash bench/codernext_overnight.sh \
#     > results/codernext_overnight.log 2>&1 &
#
# Budget: ~7 h of GPU, so a 22:10 start lands near 05:15. DEADLINE stops new
# phases rather than new questions, so overrun is bounded by one suite.
#
# UPLOAD: full-dataset runs are far above the 100-question threshold and upload
# to omlx.ai with a machine hash and model settings (FINDINGS section 11). The
# 500-sample pass already did; this is the same decision, at a larger n.
#
# Not included, deliberately:
#   specprefill on Coder-Next -- oMLX needs a draft model sharing the target's
#   tokenizer for it, and no Coder-Next drafter exists (FINDINGS section 12), so
#   there is nothing to point specprefill_draft_model at.
#   D2 best-of-N -- worth doing, but the script does not exist yet and an
#   untested script is the wrong thing to leave running unattended.
set -u

cd "$(dirname "$0")/.."

CN="${CN:-Qwen3-Coder-Next-6bit}"
SHIPPED="${SHIPPED:-Qwen3.8-27B-8bit}"
WAIT_PID="${WAIT_PID:-}"
DEADLINE="${DEADLINE:-$(date -v+1d '+%Y-%m-%d' 2>/dev/null || date '+%Y-%m-%d') 07:00}"

DEADLINE_EPOCH=$(date -j -f "%Y-%m-%d %H:%M" "$DEADLINE" +%s 2>/dev/null || echo 0)
if [ "$DEADLINE_EPOCH" -eq 0 ]; then
  echo "!! could not parse DEADLINE='$DEADLINE'; aborting rather than running unbounded"
  exit 2
fi

note() { printf '\n== %s  [%s]\n' "$1" "$(date '+%H:%M:%S')"; }

# need_minutes <mins> <what> -- refuse to start something that cannot finish.
have_time() {
  local need=$(( $1 * 60 )) now
  now=$(date +%s)
  if [ $(( now + need )) -gt "$DEADLINE_EPOCH" ]; then
    note "skipping $2: needs ~$1 min, deadline is $DEADLINE"
    return 1
  fi
  return 0
}

swap_to() {
  note "loading $1"
  python3 - "$1" "$2" <<'PY' || true
import sys
sys.path.insert(0, "bench")
from omlx_client import Omlx
want, drop = sys.argv[1], sys.argv[2]
o = Omlx()
try:
    o.unload(drop)
except Exception as e:
    print(f"  unload {drop}: {type(e).__name__}: {e}")
o.load(want)
print("  loaded:", [m["id"] for m in o.models() if m.get("loaded")])
PY
}

restore() {
  local rc=$?
  note "restoring the shipped setup"
  python3 - "$CN" "$SHIPPED" <<'PY' || true
import sys
sys.path.insert(0, "bench")
from omlx_client import Omlx
cn, shipped = sys.argv[1], sys.argv[2]
o = Omlx()
for m in (cn,):
    try:
        o.unload(m)
    except Exception as e:
        print(f"  unload {m}: {type(e).__name__}: {e}")
try:
    o.load(shipped)
except Exception as e:
    print(f"  load {shipped}: {type(e).__name__}: {e}")
print("  loaded:", [x["id"] for x in o.models() if x.get("loaded")])
PY
  python3 profiles/apply.py --verify-only || true
  note "overnight chain done (exit $rc)"
  exit $rc
}
trap restore EXIT INT TERM

if [ -n "$WAIT_PID" ]; then
  note "waiting for pid $WAIT_PID (the 500-sample accuracy pass)"
  while kill -0 "$WAIT_PID" 2>/dev/null; do sleep 60; done
  note "predecessor finished"
  sleep 30
fi

# ---------------------------------------------------------------- phase 1
# Free, no GPU, and it may make the expensive phases unnecessary.
note "phase 1: McNemar on what is already measured"
for pair in "acc_ship_reason acc_codernext_reason" "acc_ship_code acc_codernext_code"; do
  set -- $pair
  echo "--- $1 vs $2"
  python3 bench/paired_compare.py "$1" "$2" || note "paired_compare $1/$2 failed"
done

# ---------------------------------------------------------------- phase 2
# Coder-Next is already loaded by the predecessor. Cheap breadth first, then the
# expensive full mmlu_pro.
if have_time 20 "general+safety on Coder-Next"; then
  note "phase 2a: general and safety suites on Coder-Next (~13 min)"
  python3 bench/accuracy.py general --model "$CN" --label codernext_general --samples 500 --batch 8 \
    || note "general failed, continuing"
  python3 bench/accuracy.py safety --model "$CN" --label codernext_safety --samples 500 --batch 8 \
    || note "safety failed, continuing"
fi

note "phase 2c: McNemar on the breadth arms"
for pair in "acc_ship_general acc_codernext_general" \
            "acc_ship_safety acc_codernext_safety"; do
  set -- $pair
  echo "--- $1 vs $2"
  python3 bench/paired_compare.py "$1" "$2" 2>/dev/null || echo "    (arm missing, skipped)"
done

# ---------------------------------------------------------------- phase 3
# Hands the GPU back to the 27B family. quant_compare manages its own model
# state and never writes the production model or its four profiles; both
# checkpoints are already on disk, so the download phase is skipped.
if have_time 420 "quant_compare (27B 4-bit vs 8-bit)"; then
  swap_to "$SHIPPED" "$CN"
  note "phase 3: quant_compare, 27B 4-bit against the shipped 8-bit (~6-7 h)"
  python3 bench/quant_compare.py --skip-download --samples 500 \
    || note "quant_compare failed"
fi
