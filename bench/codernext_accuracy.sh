#!/usr/bin/env bash
# Accuracy pass for Qwen3-Coder-Next-6bit, closing the wave-1 gate in
# docs/backlog.md A1.
#
# FINDINGS section 12 measured speed, memory, long-context retrieval and
# tool-call format on this checkpoint and Coder-Next won every one of them. None
# of that is quality on hard tasks, which is the constraint the whole exercise
# exists to relieve, so the gate stays open until this runs.
#
# Settings are pinned to match results/acc_ship_code.json and
# results/acc_ship_reason.json exactly -- same six suites, 500 samples, batch 8,
# thinking off, deterministic sampling. Comparability is the entire point: a
# different sample count or batch size makes the pair uncomparable and wastes the
# hours. Judge the outcome with bench/paired_compare.py on per-question data, not
# on suite scores, which cannot resolve under ~2 points at 500 samples.
#
# The 27B took 7.8 h for these six, 6.2 h of it livecodebench. Coder-Next decodes
# ~2x and prefills ~3x faster, so budget 4-5 h.
#
#   bash bench/codernext_accuracy.sh 2>&1 | tee results/codernext_acc.log
#
# UPLOAD: suites of >=100 questions are uploaded to omlx.ai with a machine hash
# and model settings, and there is no opt-out (FINDINGS section 11). This run is
# 500 samples, so it uploads. SAMPLES=99 stays local but breaks comparability
# with the shipped 27B baselines, which were taken at 500.
set -u

cd "$(dirname "$0")/.."

MODEL="${MODEL:-Qwen3-Coder-Next-6bit}"
SHIPPED="${SHIPPED:-Qwen3.8-27B-8bit}"
SAMPLES="${SAMPLES:-500}"
BATCH="${BATCH:-8}"

note() { printf '\n== %s  [%s]\n' "$1" "$(date '+%H:%M:%S')"; }

# The shipped model and its four profiles are what the machine is expected to be
# serving when this finishes, whether it finishes cleanly or dies partway. This
# script never writes a profile or a global setting, so restoring is only a
# question of which weights are resident.
restore() {
  local rc=$?
  note "restoring the shipped setup"
  python3 - "$MODEL" "$SHIPPED" <<'PY' || true
import sys
sys.path.insert(0, "bench")
from omlx_client import Omlx
target, shipped = sys.argv[1], sys.argv[2]
o = Omlx()
try:
    o.unload(target)
except Exception as e:
    print(f"  unload {target}: {type(e).__name__}: {e}")
try:
    o.load(shipped)
except Exception as e:
    print(f"  load {shipped}: {type(e).__name__}: {e}")
print("  loaded:", [m["id"] for m in o.models() if m.get("loaded")])
PY
  python3 profiles/apply.py --verify-only || true
  note "done (exit $rc)"
  exit $rc
}
trap restore EXIT INT TERM

note "freeing memory: unloading $SHIPPED, loading $MODEL"
python3 - "$MODEL" "$SHIPPED" <<'PY'
import sys
sys.path.insert(0, "bench")
from omlx_client import Omlx
target, shipped = sys.argv[1], sys.argv[2]
o = Omlx()
try:
    o.unload(shipped)
except Exception as e:
    print(f"  unload {shipped}: {type(e).__name__}: {e}")
o.load(target)
print("  loaded:", [m["id"] for m in o.models() if m.get("loaded")])
PY

# Reason before code. Both are wanted, but reason is ~56 min against code's
# ~6.9 h on the 27B, so this order puts a usable answer on disk early: if the
# run dies overnight there is still a result rather than nothing.
note "reason suites (mmlu_pro, gsm8k, mathqa)"
python3 bench/accuracy.py reason \
  --model "$MODEL" --label codernext_reason \
  --samples "$SAMPLES" --batch "$BATCH" \
  || note "reason group failed, continuing to code"

note "code suites (humaneval, mbpp, livecodebench)"
python3 bench/accuracy.py code \
  --model "$MODEL" --label codernext_code \
  --samples "$SAMPLES" --batch "$BATCH" \
  || note "code group failed"

note "comparison against the shipped 27B"
python3 - <<'PY' || true
import json
from pathlib import Path

# accuracy.py prepends "acc_" to every label when it saves, so the label is not
# the filename. Passing the bare label here found nothing and printed two
# FileNotFoundErrors after a 4-hour run.
pairs = [("acc_codernext_reason", "acc_ship_reason"),
         ("acc_codernext_code", "acc_ship_code")]
base = Path("results")
print(f"  {'suite':<16} {'27B':>7} {'CoderNext':>10} {'delta':>7} {'n':>5}")
print("  " + "-" * 50)
for new, old in pairs:
    try:
        a = {r["benchmark"]: r for r in json.loads((base / f"{old}.json").read_text())["results"]}
        b = {r["benchmark"]: r for r in json.loads((base / f"{new}.json").read_text())["results"]}
    except (OSError, json.JSONDecodeError, KeyError) as e:
        print(f"  ! {new}: {type(e).__name__}: {e}")
        continue
    for suite in a:
        if suite not in b:
            continue
        oa, na = a[suite]["accuracy"], b[suite]["accuracy"]
        print(f"  {suite:<16} {oa:>7.3f} {na:>10.3f} {na - oa:>+7.3f} {b[suite]['total']:>5}")
print("\n  At n=500 the 95% band on one score is ~+/-4.4 points, and ~+/-6.2 on a")
print("  difference of two. Nothing below that is readable here.")
print("  Settle anything closer with: python3 bench/paired_compare.py")
PY
