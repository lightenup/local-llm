#!/usr/bin/env bash
# A2b, oQ arm — the third leg of the quantization comparison.
#
# What this settles. The RTN arm is already on disk and said something specific:
# 27B 4-bit RTN matches the shipped 8-bit on code (p = 0.130) and loses on
# reasoning (p = 0.0028). So bit count alone forces a two-model lineup. The open
# question is whether BUILD METHOD closes that gap — whether a data-driven
# mixed-up allocation at the same size recovers the reasoning loss. If it does,
# one 17 GB model does both jobs and the lineup collapses to one checkpoint.
#
# The gate is already passed, by hand, on 2026-09-11 (results/a2b_gate_oq4e.json):
# `Qwen3.8-27B-oQ4e-mtp` reports mtp_compatible=true with no blocking reason —
# unlike the mlx-community MTP drafters, which declare MTP layers whose tensors
# are absent — and its in-checkpoint MTP measured 64.0 tok/s against 30.2 with
# it off, a 2.12x speedup. That is better than the shipped 8-bit's 1.97x, and it
# needs no separate drafter. So the checkpoint is real and the accuracy hours
# are justified; that was the one thing that could have wasted the night.
#
# PHASE ORDER IS DELIBERATE: reason before code, which is cheaper-first but more
# importantly question-first. The reasoning suites are ~4 s/question and the
# whole group runs in under two hours; livecodebench alone is ~50 s/question and
# ~7 h. Reason is also the ONLY arm that can change the lineup — code is already
# known not to discriminate at 4 bits. If this run dies at 3 a.m., the half that
# survives should be the half that answers the question.
#
# UPLOAD, accepted deliberately. 500 samples/suite crosses the 100-question
# threshold and uploads to omlx.ai with a machine hash and model settings
# (FINDINGS §11). It cannot be avoided here: paired_compare works question by
# question, so this arm must use the SAME sample size as the RTN and 8-bit arms
# already on disk, and at 99 the comparison that makes A2b interpretable would
# not exist. See docs/backlog.md §A2b.
#
#   nohup bash bench/oq4e_overnight.sh > results/oq4e_overnight.log 2>&1 &
#
# Budget: ~20 min memory/throughput + ~1.5-2 h reason + ~7-8 h code. The 2.12x decode should pull this in
# under the ~10 h the backlog budgeted, but do not count on it: these suites are
# prefill-heavy and §8 showed the MTP win is single-stream only, while accuracy
# runs at batch 8.
set -u

cd "$(dirname "$0")/.."

OQ="${OQ:-Qwen3.8-27B-oQ4e-mtp}"
SHIPPED="${SHIPPED:-Qwen3.8-27B-8bit}"
SAMPLES="${SAMPLES:-500}"
WAIT_PID="${WAIT_PID:-}"
DEADLINE="${DEADLINE:-$(date -v+1d '+%Y-%m-%d' 2>/dev/null || date '+%Y-%m-%d') 09:00}"

DEADLINE_EPOCH=$(date -j -f "%Y-%m-%d %H:%M" "$DEADLINE" +%s 2>/dev/null || echo 0)
if [ "$DEADLINE_EPOCH" -eq 0 ]; then
  echo "!! could not parse DEADLINE='$DEADLINE'; aborting rather than running unbounded"
  exit 2
fi

note() { printf '\n== %s  [%s]\n' "$1" "$(date '+%H:%M:%S')"; }

have_time() {
  local need=$(( $1 * 60 )) now
  now=$(date +%s)
  if [ $(( now + need )) -gt "$DEADLINE_EPOCH" ]; then
    note "skipping $2: needs ~$1 min, deadline is $DEADLINE"
    return 1
  fi
  return 0
}

# The shipped setup is what this must hand back: the 27B resident and pinned,
# its four profiles intact. Coder-Next is unpinned by design and pages back in
# on demand, so it is not force-loaded here.
restore() {
  local rc=$?
  note "restoring the shipped setup"
  python3 - "$OQ" "$SHIPPED" <<'PY' || true
import sys
sys.path.insert(0, "bench")
from omlx_client import Omlx
oq, shipped = sys.argv[1], sys.argv[2]
o = Omlx()
try:
    o.unload(oq)
except Exception as e:
    print(f"  unload {oq}: {type(e).__name__}: {e}")
try:
    o.load(shipped)
except Exception as e:
    print(f"  load {shipped}: {type(e).__name__}: {e}")
print("  loaded:", [x["id"] for x in o.models() if x.get("loaded")])
PY
  python3 profiles/apply.py --verify-only || true
  note "oQ arm done (exit $rc)"
  exit $rc
}
trap restore EXIT INT TERM

if [ -n "$WAIT_PID" ]; then
  note "waiting for pid $WAIT_PID"
  while kill -0 "$WAIT_PID" 2>/dev/null; do sleep 60; done
  note "predecessor finished"
  sleep 30
fi

note "loading $OQ (mtp_enabled was set during the hand gate)"
python3 - "$OQ" <<'PY' || exit 1
import sys
sys.path.insert(0, "bench")
from omlx_client import Omlx
o = Omlx(); m = sys.argv[1]
o.update_settings({"mtp_enabled": True}, model_id=m)
o.load(m)
info = o.model(m)
print(f"  loaded={info['loaded']} size={info.get('actual_size_formatted')} "
      f"mtp_compatible={info.get('mtp_compatible')} "
      f"mtp_enabled={o.settings(m).get('mtp_enabled')}")
assert info["loaded"] and info.get("mtp_compatible"), "oQ4e did not come up with MTP"
PY

# ---------------------------------------------------------------- phase 0
# Cheap, and it decides more than accuracy does. A3b established that the 27B
# and Coder-Next cannot both hold KV under the guard's ceiling, so every
# alternation evicts and reloads at ~20 s. oQ4e is 16 GB against Coder-Next's
# 64.7, so the lineup question is now partly a MEMORY question and tomorrow's
# decision needs both numbers side by side.
#
# Order matters here: the residency check runs while both models are up, BEFORE
# the accuracy suites start evicting things.
note "phase 0: throughput and two-model residency with oQ4e (~20 min)"
python3 bench/throughput.py oq4e --lengths 1024 4096 16384 --model "$OQ" \
  2>/dev/null || note "throughput failed, continuing"

python3 - "$OQ" "$SHIPPED" <<'PY' || note "residency check failed, continuing"
import json, subprocess, sys, time
sys.path.insert(0, "bench")
from omlx_client import Omlx, OmlxError

oq, shipped = sys.argv[1], sys.argv[2]
o = Omlx()


def mem():
    out = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
    import re
    ps = int(re.search(r"page size of (\d+)", out).group(1))
    a = int(re.search(r"Pages active:\s+(\d+)", out).group(1))
    w = int(re.search(r"Pages wired down:\s+(\d+)", out).group(1))
    return round((a + w) * ps / 1e9, 2)


rec = {"question": "does oQ4e coexist with the 27B without eviction?"}
o.load(shipped)
o.load(oq)
rec["both_loaded"] = [m["id"] for m in o.models() if m.get("loaded")]
rec["mem_after_load_gb"] = mem()

# The exact alternation that thrashes with Coder-Next: a substantial prompt to
# each model in turn, checking after every call whether the other survived.
rec["rounds"] = []
for i in range(3):
    row = {}
    for label, model in (("27B", shipped), ("oQ4e", oq)):
        t = time.time()
        try:
            o.chat([{"role": "user", "content": "word " * 6000}],
                   model=model, max_tokens=1, timeout=900)
            row[label] = {"s": round(time.time() - t, 1)}
        except OmlxError as e:
            row[label] = {"s": round(time.time() - t, 1), "error": str(e)[:160]}
        row[label]["loaded"] = [m["id"] for m in o.models() if m.get("loaded")]
        row[label]["mem_gb"] = mem()
    rec["rounds"].append(row)
    print(f"  round {i+1}: "
          f"27B {row['27B']['s']}s loaded={len(row['27B']['loaded'])}  "
          f"oQ4e {row['oQ4e']['s']}s loaded={len(row['oQ4e']['loaded'])}")

# The verdict: with Coder-Next this alternation dropped to one resident model
# every single time and paid ~20 s to reload. If oQ4e holds two throughout, the
# thrash is gone and the lineup question is settled on memory as well as accuracy.
evicted = sum(1 for r in rec["rounds"] for v in r.values() if len(v["loaded"]) < 2)
rec["evictions_observed"] = evicted
rec["verdict"] = ("no eviction - oQ4e coexists with the 27B"
                  if evicted == 0 else
                  f"{evicted} evictions across 6 calls - still thrashing")
print(f"  VERDICT: {rec['verdict']}")
Omlx.save("oq4e_residency", rec)
PY

# ---------------------------------------------------------------- phase 1
# The question. ~1.5-2 h. mmlu_pro is the suite that carried the RTN verdict
# (p = 0.0028), so this is where a build-method win would show up.
if have_time 150 "reason suites on $OQ"; then
  note "phase 1: reason group on $OQ, $SAMPLES samples (the question)"
  python3 bench/accuracy.py reason --model "$OQ" --label oq4e_reason \
    --samples "$SAMPLES" --batch 8 || note "reason failed, continuing"

  note "phase 1b: McNemar, oQ against both arms already on disk"
  for pair in "acc_ship_reason acc_oq4e_reason" "acc_quant4_reason acc_oq4e_reason"; do
    set -- $pair
    echo "--- $1 vs $2"
    python3 bench/paired_compare.py "$1" "$2" 2>/dev/null || echo "    (arm missing, skipped)"
  done
fi

# ---------------------------------------------------------------- phase 2
# Confirmatory. RTN 4-bit already matched 8-bit here (p = 0.130), so the
# expected result is "no difference" and the value is in ruling out a
# build-method regression rather than in finding a win.
if have_time 480 "code suites on $OQ"; then
  note "phase 2: code group on $OQ, $SAMPLES samples (~7-8 h, confirmatory)"
  python3 bench/accuracy.py code --model "$OQ" --label oq4e_code \
    --samples "$SAMPLES" --batch 8 || note "code failed, continuing"

  note "phase 2b: McNemar on the code arms"
  for pair in "acc_ship_code acc_oq4e_code" "acc_quant4_code acc_oq4e_code"; do
    set -- $pair
    echo "--- $1 vs $2"
    python3 bench/paired_compare.py "$1" "$2" 2>/dev/null || echo "    (arm missing, skipped)"
  done
fi

note "all phases attempted"
