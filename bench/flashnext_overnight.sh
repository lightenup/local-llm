#!/usr/bin/env bash
#
# Qwen3.8-Flash-Next-oQ4e-mtp, one unattended arm.
#
# THE QUESTION
# Is a 125B/6B-active qwen4_exp model better than the shipped Qwen3.8-27B-8bit
# on the axis its vendor claims — agentic and tool-shaped work — while being
# cheap enough per token to be worth 80 GB of the machine? FINDINGS §22 closed
# "parameter count is not the lever" against previous-generation MoEs; this is a
# different architecture, so §22 is the reason to test rather than to skip.
#
# WHAT IT BUILDS ON
#   results/acc_ship_code.json        the 8-bit at 500 samples/suite, the pair
#   results/acc_oq4e_code.json        the fast tier, 54.0 on livecodebench
#   results/tp_mtp.json               the 8-bit's throughput baseline
#   docs/FINDINGS.md §7               a non-zero presence penalty kills MTP
#   docs/FINDINGS.md §14              livecodebench is blind to agentic ability
#   docs/backlog.md A3b               two models that cannot both hold KV are
#                                     worse than one, whatever they score
#
# PHASE ORDER IS DELIBERATE
# Residency runs first, before anything has started evicting things, because it
# can disqualify the model outright: 80.1 GB offloaded plus the shipped 28.2 GB
# is 108.3 GB against a 115.4 GB admission ceiling, leaving ~7 GB for both KV
# caches. If it cannot coexist, the accuracy hours are wasted either way.
# The thinking A/B runs second and gates the accuracy phase, because this
# checkpoint's chat template defaults thinking ON and accuracy.py has no
# chat_template_kwargs path at all — if the switch is not connected, an eight
# hour "thinking off" code run measures something nobody asked for.
#
# EVERY PHASE RUNS WITH THE PLE OFFLOADED (qwen4_ple_ssd_offload=True).
# oMLX only *forces* offload when resident exceeds the ceiling and mmap does
# not; at 111.6 GB resident against 115.4 GB it does not fire, so the default is
# to hold the 51.2B n-gram table in RAM and starve everything else. Offload is
# therefore set deliberately here, and it is part of _variant_signature, so
# every flip reloads an 80 GB engine. Flipping it mid-run would silently
# reload and make two phases incomparable.
#
# UPLOAD: yes, and on purpose. --samples 500 matches the existing arms, and
# paired_compare.py pairs per question id, so anything under 500 would shrink
# the shared set and throw away the McNemar comparison this arm exists to make.
# 500 >= UPLOAD_THRESHOLD (100), so the suites upload to omlx.ai. Accepted for
# the same reason bench/oq4e_overnight.sh accepts it: the pairing is worth more
# than the privacy of public benchmark questions.
#
#   nohup bash bench/flashnext_overnight.sh > results/flashnext_overnight.log 2>&1 &
#
# Env knobs, all optional:
#   SKIP_RESIDENCY=1   phase 0 is already measured (results/flashnext_residency.json)
#   SKIP_ACCURACY=1    stop before the ~8 h code suite
#   TP_LENGTHS="1024 4096"   drop 16384 if the bench task errors at that length
#   AB_REPEATS=5       repeats per thinking-A/B arm; 1 is too few to trust
#   FN / SHIPPED / SAMPLES / BATCH / WAIT_PID / DEADLINE
#
# As of 2026-09-13 the useful invocation is the agentic axis alone, because
# phases 0-0c have run and the code suite is unauthorised:
#
#   SKIP_RESIDENCY=1 SKIP_ACCURACY=1 nohup bash bench/flashnext_overnight.sh \
#     > results/flashnext_overnight.log 2>&1 &
#
# It needs the proxy restarted first, or phase 1 self-skips with instructions:
#   docker compose -f proxy/docker-compose.yml restart litellm
#
# Budget: residency ~45 min, throughput ~25 min, thinking A/B ~20 min,
# taskbench ~45 min, code accuracy ~8 h. Roughly 10 h end to end.

set -u
cd "$(dirname "$0")/.."

FN="${FN:-Qwen3.8-Flash-Next-oQ4e-mtp}"
SHIPPED="${SHIPPED:-Qwen3.8-27B-8bit}"
SAMPLES="${SAMPLES:-500}"
BATCH="${BATCH:-8}"
WAIT_PID="${WAIT_PID:-}"
# Both default to running. SKIP_RESIDENCY=1 once the gate has already been
# measured (it costs ~40 min and two cold 70-100 GB loads); SKIP_ACCURACY=1 to
# take the ~1.5 h of cheap phases and stop before the ~8 h code suite.
SKIP_RESIDENCY="${SKIP_RESIDENCY:-0}"
SKIP_ACCURACY="${SKIP_ACCURACY:-0}"
# Deliberately word-split below. 16384 is included by default but oMLX's bench
# task returned a bare `error` with no message at that length on 2026-09-13, so
# it is worth being able to drop it without editing the script.
TP_LENGTHS="${TP_LENGTHS:-1024 4096 16384}"
# n=4 tasks x 1 repeat was enough to see the shape and far too little to trust
# the presence_penalty figure, which came out the opposite way to FINDINGS §7.
AB_REPEATS="${AB_REPEATS:-1}"
# Phase 1 repeats. 2 is what task_coder/task_fast/task_oqcode were collected at,
# so anything else makes `taskbench compare`'s RELIABILITY half (passed n of n)
# read against a different denominator; the CAPABILITY half (passed >=1) still
# compares, and gets stricter for the arm with more repeats.
TASK_REPEATS="${TASK_REPEATS:-2}"
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

# Which models were loaded before this run displaced them. Written by setup,
# read by restore, because this arm does not merely add a model — the admission
# guard refuses a 72.65 GB load while the lineup is resident, so the lineup has
# to be unloaded and therefore has to be put back by name.
BASELINE_FILE="$(mktemp -t flashnext_baseline)"

# ---------------------------------------------------------------- restore
# Captures $? first so the trap cannot mask the real exit status. Each remote
# call is guarded on its own: a failed unload must not prevent the reload that
# puts production back, and neither may fail the trap itself.
restore() {
  local rc=$?
  note "restoring the shipped setup"
  python3 - "$FN" "$SHIPPED" "$BASELINE_FILE" <<'PY' || true
import json
import sys
from pathlib import Path
sys.path.insert(0, "bench")
from omlx_client import Omlx
fn, shipped, baseline_file = sys.argv[1], sys.argv[2], sys.argv[3]
o = Omlx()
try:
    o.unload(fn)
except Exception as e:
    print(f"  unload {fn}: {type(e).__name__}: {e}")
# The candidate keeps its own settings on disk; leave the PLE flag as the run
# left it (it only matters when this model is loaded) but put MTP back to the
# state the profiles assume, so a later manual load is not silently unaccelerated.
try:
    o.update_settings({"mtp_enabled": True}, model_id=fn)
except Exception as e:
    print(f"  settings {fn}: {type(e).__name__}: {e}")
try:
    baseline = json.loads(Path(baseline_file).read_text())
except Exception:
    baseline = []
# The shipped model first and unconditionally: if the baseline record is missing
# or unreadable, production still has to come back.
for target in [shipped] + [b for b in baseline if b != shipped]:
    try:
        o.load(target)
    except Exception as e:
        print(f"  load {target}: {type(e).__name__}: {str(e)[:160]}")
print("  loaded:", [x["id"] for x in o.models() if x.get("loaded")])
print("  baseline was:", baseline or "(not recorded)")
PY
  python3 profiles/apply.py --verify-only || true
  rm -f "$BASELINE_FILE"
  note "flashnext arm done (exit $rc)"
  exit $rc
}
trap restore EXIT INT TERM

if [ -n "$WAIT_PID" ]; then
  note "waiting for pid $WAIT_PID"
  while kill -0 "$WAIT_PID" 2>/dev/null; do sleep 60; done
  note "predecessor finished"
  sleep 30
fi

# ---------------------------------------------------------------- setup
# The one phase allowed to abort the run: if the model will not come up with the
# PLE offloaded and MTP on, there is nothing to measure.
#
# MEASURED (results/flashnext_residency_refused.json): oMLX's admission guard
# does not evict to make room, it refuses — 72.65 GB on top of a 51.71 GB lineup
# projects to 124.36 GB against a 107.52 GB metal_cap. So the lineup is unloaded
# here, by name, and recorded in BASELINE_FILE for restore to put back.
note "setup: make room, then mtp_enabled + qwen4_ple_ssd_offload, then prove it loads"
python3 - "$FN" "$BASELINE_FILE" <<'PY' || exit 1
import json
import sys
import time
from pathlib import Path
sys.path.insert(0, "bench")
from omlx_client import Omlx
m, baseline_file = sys.argv[1], sys.argv[2]
o = Omlx()
info = o.model(m)          # raises if the checkpoint is not on disk

baseline = [x["id"] for x in o.models() if x.get("loaded")]
Path(baseline_file).write_text(json.dumps(baseline))
print(f"  baseline loaded     {baseline}")
for victim in [b for b in baseline if b != m]:
    try:
        o.unload(victim)
        print(f"  unloaded            {victim}")
    except Exception as e:
        print(f"  could not unload {victim}: {type(e).__name__}: {str(e)[:120]}")

# An unload does not release immediately. Attempting the load straight after one
# was refused against a *dynamic* ceiling of 91.29 GB with the pool still
# reporting 48.61 GB in use — a third ceiling, below both metal_cap (107.52 GB)
# and final_ceiling (115.45 GB), and the only one that moves with what other
# apps are holding. So wait for the pool to actually drain before asking.
cur = o.health()["engine_pool"]["current_model_memory"]
for _ in range(90):
    if cur < 5e9:
        break
    time.sleep(2)
    cur = o.health()["engine_pool"]["current_model_memory"]
print(f"  pool after unloads  {cur / 1e9:.2f} GB")

if not o.settings(m):      # a never-loaded model has no settings block yet
    o.load(m)
o.update_settings({"mtp_enabled": True, "qwen4_ple_ssd_offload": True}, model_id=m)
# Reclaimable host RAM fluctuates with whatever else is running, so a refusal is
# not necessarily final. Retry a few times before giving up on the whole arm.
for attempt in range(1, 5):
    try:
        o.load(m)
        break
    except Exception as e:
        print(f"  load attempt {attempt} failed: {str(e)[:200]}")
        if attempt == 4:
            raise
        time.sleep(20)
s, info = o.settings(m), o.model(m)
print(f"  loaded              {info.get('loaded')}")
print(f"  size                {str(info.get('actual_size_formatted'))}")
print(f"  engine_type         {info.get('engine_type')}")
print(f"  mtp_compatible      {info.get('mtp_compatible')}")
print(f"  mtp_enabled         {s.get('mtp_enabled')}")
print(f"  ple_ssd_offload     {s.get('qwen4_ple_ssd_offload')}")
print(f"  context             {info.get('model_context_length')}")
assert info.get("loaded"), f"{m} did not load"
assert s.get("qwen4_ple_ssd_offload") is True, "PLE offload did not stick"
PY

# ---------------------------------------------------------------- phase 0
# Residency and eviction cost. Runs before anything else touches memory, and
# prices both arms (resident and mmap) plus the alternation cost against the
# shipped 8-bit. This is the gate: A3b retired Coder-Next on exactly this
# measurement, not on a score.
if [ "$SKIP_RESIDENCY" -eq 1 ]; then
  note "phase 0 skipped: SKIP_RESIDENCY=1, see results/flashnext_residency.json"
elif have_time 45 "phase 0 residency gate"; then
  note "phase 0: residency and eviction cost"
  python3 bench/flashnext_residency.py --model "$FN" --arms mmap,resident --rounds 3 \
    || note "residency probe failed, continuing (read the log before trusting later phases)"
fi

# Re-assert the flag: the probe deliberately exercises both arms and restores
# the baseline, which is not necessarily the offloaded state the rest wants.
note "phase 0a: re-pin the offloaded configuration for the measured phases"
python3 - "$FN" <<'PY' || exit 1
import sys
import time
sys.path.insert(0, "bench")
from omlx_client import Omlx
m = sys.argv[1]
o = Omlx()
# The residency probe restores the baseline lineup on its way out, so room has
# to be made again here — the guard still will not do it for us, and the release
# is not immediate (see the setup phase's note on the dynamic ceiling).
for victim in [x["id"] for x in o.models() if x.get("loaded") and x["id"] != m]:
    try:
        o.unload(victim)
    except Exception as e:
        print(f"  could not unload {victim}: {type(e).__name__}: {str(e)[:120]}")
cur = o.health()["engine_pool"]["current_model_memory"]
for _ in range(90):
    if cur < 5e9:
        break
    time.sleep(2)
    cur = o.health()["engine_pool"]["current_model_memory"]
o.update_settings({"mtp_enabled": True, "qwen4_ple_ssd_offload": True}, model_id=m)
for attempt in range(1, 5):
    try:
        o.load(m)
        break
    except Exception as e:
        print(f"  load attempt {attempt} failed: {str(e)[:200]}")
        if attempt == 4:
            raise
        time.sleep(20)
s = o.settings(m)
print(f"  mtp_enabled={s.get('mtp_enabled')} "
      f"qwen4_ple_ssd_offload={s.get('qwen4_ple_ssd_offload')}")
assert s.get("qwen4_ple_ssd_offload") is True, "PLE offload did not stick"
PY

# ---------------------------------------------------------------- phase 0b
# Throughput. Capped at 16384 rather than 65536: the long-context axis was
# dropped for this arm, and a 65k prefill on an SSD-backed PLE is a measurement
# of the SSD as much as of the model.
if have_time 30 "phase 0b throughput"; then
  note "phase 0b: throughput against tp_mtp"
  # shellcheck disable=SC2086  # TP_LENGTHS is a deliberate word-split list
  python3 bench/throughput.py flashnext --lengths $TP_LENGTHS --model "$FN" \
    || note "throughput failed, continuing"
  if have_time 20 "phase 0b batch throughput"; then
    python3 bench/throughput.py flashnext_batch --lengths 4096 --batch 2 4 8 \
      --model "$FN" 2>/dev/null || note "batch throughput failed, continuing"
  fi
fi

# ---------------------------------------------------------------- phase 0c
# Thinking A/B, and the gate on the accuracy phase. Exit 3 means preflight found
# the thinking switch disconnected, which invalidates any thinking-off suite on
# this model — so the code phase is skipped rather than run into the ground.
THINKING_OK=1
if have_time 25 "phase 0c thinking A/B"; then
  note "phase 0c: thinking off vs on, and what presence_penalty 1.5 costs"
  python3 bench/flashnext_thinking_ab.py --model "$FN" --label fn_thinking \
    --repeats "$AB_REPEATS"
  case $? in
    0) : ;;
    3) THINKING_OK=0
       note "!! preflight says thinking-off is not honoured; skipping the code suite" ;;
    *) note "thinking A/B failed for another reason, continuing" ;;
  esac
fi

# ---------------------------------------------------------------- phase 1
# The question. Agentic and tool-shaped work is where the vendor claims the gap
# (DeepSWE 58.7 vs 42.2, JobBench 55.7 vs 33.4), and §14 found livecodebench
# blind to it. Goes through the LiteLLM proxy, so it needs the proxy up and the
# fn-* entries in proxy/litellm-config.yaml.
if have_time 60 "phase 1 taskbench"; then
  note "phase 1: taskbench, the agentic axis"
  # Checking for the model NAME, not just liveness: the fn-* entries were added
  # to litellm-config.yaml but LiteLLM reads that file at startup, so a proxy
  # that has not been restarted since is healthy and still cannot route here.
  # Better to say so than to spend the phase collecting 400s.
  # Resolved through taskbench's own PROXY and _key(), so the check cannot pass
  # while the real request would fail on a different base URL or key.
  if python3 - "$FN:fn-agent" <<'PY'
import json, sys, urllib.request
sys.path.insert(0, "bench")
from taskbench import PROXY, _key
want = sys.argv[1]
req = urllib.request.Request(
    f"{PROXY}/v1/models", headers={"Authorization": f"Bearer {_key()}"})
try:
    with urllib.request.urlopen(req, timeout=15) as r:
        served = [m.get("id") for m in (json.load(r).get("data") or [])]
except Exception as e:
    print(f"   proxy unreachable at {PROXY}: {type(e).__name__}: {e}")
    raise SystemExit(1)
if want in served:
    raise SystemExit(0)
print(f"   {want} is not in the proxy's model list ({len(served)} served)")
raise SystemExit(1)
PY
  then
    python3 bench/taskbench.py run --model "$FN:fn-agent" --label flashnext \
      --repeats "$TASK_REPEATS" || note "taskbench failed, continuing"
    # task_coder is the shipped 8-bit through the `coder` alias; task_fast is the
    # oQ4e fast tier through `fast`, which is the same agent-shaped role fn-agent
    # is being tried for. task_oqcode is the fast tier on the code profile.
    #
    # `taskbench compare` takes LABELS and prefixes task_ itself, so these are
    # bare labels while the -f guard tests the on-disk filename. Passing
    # "task_coder" here made it look for results/task_task_coder.json, and the
    # 2>/dev/null turned that FileNotFoundError into a silent "compare failed"
    # on 2026-09-16 — the run measured fine and reported nothing.
    for arm in coder fast oqcode; do
      if [ -f "results/task_$arm.json" ]; then
        echo "--- task_$arm vs task_flashnext"
        python3 bench/taskbench.py compare "$arm" flashnext \
          || echo "    (compare failed, skipped)"
      fi
    done
  else
    note "proxy is not serving $FN:fn-agent; skipping taskbench"
    echo "   taskbench has no direct-to-oMLX path. Either the proxy is down or it"
    echo "   has not been restarted since the fn-* entries were added:"
    echo "     docker compose -f proxy/docker-compose.yml restart litellm"
  fi
fi

# ---------------------------------------------------------------- phase 2
# Confirmatory, not exploratory: the code group is the axis where the vendor
# claims almost nothing (91.9 vs 90.3 on livecodebench v6), so its job is to
# prove the candidate does not REGRESS on the work the 8-bit currently holds.
# ~8 h at 500 samples, livecodebench alone being ~50 s/question.
if [ "$SKIP_ACCURACY" -eq 1 ]; then
  note "phase 2 skipped: SKIP_ACCURACY=1"
elif [ "$THINKING_OK" -eq 1 ] && have_time 480 "phase 2 code accuracy"; then
  note "phase 2: code group at $SAMPLES samples/suite (uploads to omlx.ai)"
  python3 bench/accuracy.py code --model "$FN" --label flashnext_code \
    --samples "$SAMPLES" --batch "$BATCH" || note "code accuracy failed, continuing"

  note "phase 2b: McNemar against every code arm on disk"
  for pair in "acc_ship_code acc_flashnext_code" "acc_oq4e_code acc_flashnext_code"; do
    set -- $pair
    echo "--- $1 vs $2"
    python3 bench/paired_compare.py "$1" "$2" --save "$(echo "$2" | sed 's/^acc_//')_vs_$(echo "$1" | sed 's/^acc_//')" \
      2>/dev/null || echo "    (arm missing, skipped)"
  done
elif [ "$THINKING_OK" -ne 1 ]; then
  note "phase 2 skipped: fix the thinking switch first, then re-run with --arms off"
fi

note "all phases attempted"
