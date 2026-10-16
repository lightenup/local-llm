#!/usr/bin/env bash
# Unattended experiment chain for the window while nobody is at the machine.
#
# Runs after the overnight validation and the safety follow-on. Everything here
# is scripted, restores its own state, and is ordered so the cheap high-value
# work lands first and the long speculative work is last, where losing it costs
# least.
#
# PHASES
#   A  hot cache A/B                    ~45 min   the "largest remaining win"
#   B  MTP across batch + concurrency   ~2 h      is the 1.8x real under load?
#   C  chunked_prefill and specprefill  ~1.5 h    the two untried prefill levers
#   D  long-context quality             ~4 h      does 262k actually work?
#   E  4-bit quantisation comparison    ~9 h      the only axis never touched
#
# WHY PHASE A CAN EXIST AT ALL
# docs/FINDINGS.md section 8 called the hot cache unreachable without a
# settings.json edit and a server restart. It is reachable: hot_cache_max_size
# is an admin GlobalSettingsRequest field applied at runtime through
# _apply_cache_settings_runtime. Same for scheduler.chunked_prefill.
#
# SAFETY CONTRACT
# This machine serves a live editor integration from four profiles on
# Qwen3.8-27B-8bit. Every phase must hand that back untouched:
#   - each phase script restores what it changed, in its own finally block
#   - the EXIT trap here re-applies the shipped variant, the shipped global
#     settings and all four profiles regardless of how the run ends
#   - a DEADLINE stops new phases so the restore always happens before the
#     user is back, even if a phase overruns
#   - the run ends with a state report, so the log's last page is proof of what
#     the server is actually serving
#
# Usage:
#   WAIT_PID=<pid> bash bench/unattended.sh
#   DEADLINE="2026-09-10 06:00" PHASES=ABCDE bash bench/unattended.sh

set -uo pipefail
cd "$(dirname "$0")/.."
mkdir -p results

WAIT_PID="${WAIT_PID:-}"
PHASES="${PHASES:-ABCDE}"
SAMPLES="${SAMPLES:-500}"
DEADLINE="${DEADLINE:-2026-09-10 06:00}"

DEADLINE_EPOCH=$(date -j -f "%Y-%m-%d %H:%M" "$DEADLINE" +%s 2>/dev/null || echo 0)
if [ "$DEADLINE_EPOCH" -eq 0 ]; then
  echo "!! could not parse DEADLINE='$DEADLINE'; aborting rather than running unbounded"
  exit 1
fi

stamp() { date '+%F %T'; }
step()  { printf '\n\n%s\n======== %s ========\n[%s]\n' "$(printf '=%.0s' {1..70})" "$1" "$(stamp)"; }
note()  { printf '  -- %s\n' "$1"; }

have_time() {
  # $1 = minutes the next phase is expected to need
  local need=$(( ${1:-30} * 60 ))
  local now; now=$(date +%s)
  if [ $(( now + need )) -gt "$DEADLINE_EPOCH" ]; then
    note "skipping: not enough time before deadline $DEADLINE"
    return 1
  fi
  return 0
}

wants() { [[ "$PHASES" == *"$1"* ]]; }

restore_shipped() {
  echo
  echo "======== restoring shipped state ========"
  echo "[$(stamp)]"
  python3 - <<'PY' || true
import sys
sys.path.insert(0, "bench")
from omlx_client import Omlx
o = Omlx()
# Global levers back to the values the setup shipped with.
try:
    o.update_global_settings({"hot_cache_max_size": "0", "chunked_prefill": False})
    c = o.global_settings()
    print("  hot_cache_max_size =", (c.get("cache") or {}).get("hot_cache_max_size"))
    print("  chunked_prefill    =", (c.get("scheduler") or {}).get("chunked_prefill"))
except Exception as e:
    print("!! global settings restore failed:", e)
# Base-model sampling must stay unset so profiles own sampling.
try:
    o.update_settings({"temperature": None, "top_p": None, "top_k": None})
    print("  base-model sampling cleared")
except Exception as e:
    print("!! sampling clear failed:", e)
PY
  python3 bench/set_variant.py apply mtp || true
  python3 profiles/apply.py || true
}

state_report() {
  echo
  echo "======== state report ========"
  echo "[$(stamp)]"
  python3 bench/set_variant.py show || true
  python3 - <<'PY' || true
import sys
sys.path.insert(0, "bench")
from omlx_client import Omlx
o = Omlx()
g = o.global_settings()
c, s = g.get("cache") or {}, g.get("scheduler") or {}
print("global cache/scheduler:")
for k in ("hot_cache_max_size", "hot_cache_only", "ssd_cache_max_size"):
    print(f"  cache.{k:<22} {c.get(k)}")
for k in ("chunked_prefill", "max_concurrent_requests", "prefill_priority"):
    print(f"  scheduler.{k:<18} {s.get(k)}")
print("models:")
for m in o.models():
    print(f"  {m['id']:<28} loaded={m['loaded']}")
print("profiles:")
for p in o.list_profiles():
    st = p.get("settings") or {}
    print(
        f"  {p['name']:<9} {p.get('model_id'):<32} temp={st.get('temperature')} "
        f"ctx={st.get('max_context_window')} force={st.get('force_sampling')}"
    )
PY
  echo
  echo "results present:"
  ls -1 results/*.json 2>/dev/null | sed 's/^/  /' || true
}

COMPLETED=0
WATCHDOG_PID=""

finish() {
  [ -n "$WATCHDOG_PID" ] && kill "$WATCHDOG_PID" 2>/dev/null
  restore_shipped
  echo
  note "verifying the served setup"
  python3 profiles/apply.py --verify-only || true
  python3 bench/smoke.py || true
  state_report
  if [ "$COMPLETED" -eq 1 ]; then
    echo "[$(stamp)] unattended chain finished normally"
  else
    echo "[$(stamp)] unattended chain ended early (see above)"
  fi
  echo "compare everything with: python3 bench/report.py"
}
trap finish EXIT
# A signal must route through the EXIT trap, otherwise a watchdog kill would
# leave the hot cache or a non-shipped variant applied.
trap 'echo "[$(stamp)] signal received"; exit 143' TERM INT

# have_time only stops new phases from STARTING. A phase that overruns its own
# internal budget could still be running when the user is back, so this is the
# hard stop: 30 minutes past the deadline, kill the phase and let the EXIT trap
# restore the shipped state.
watchdog() {
  local hard=$(( DEADLINE_EPOCH + 1800 ))
  while [ "$(date +%s)" -lt "$hard" ]; do sleep 120; done
  echo
  echo "[$(stamp)] WATCHDOG: hard deadline passed, terminating the current phase"
  pkill -P "$1" 2>/dev/null
  kill -TERM "$1" 2>/dev/null
}
watchdog $$ &
WATCHDOG_PID=$!

# ---------------------------------------------------------------------------
if [ -n "$WAIT_PID" ]; then
  echo "[$(stamp)] waiting for pid $WAIT_PID (overnight + safety chain) to finish"
  while kill -0 "$WAIT_PID" 2>/dev/null; do sleep 60; done
  echo "[$(stamp)] predecessor exited"
  sleep 30
fi

echo "[$(stamp)] starting unattended chain: phases=$PHASES deadline=$DEADLINE"
python3 bench/set_variant.py show

# ---------------------------------------------------------------------------
if wants A && have_time 60; then
  step "A. hot cache A/B (the FINDINGS section 8 blocker, now reachable by API)"
  python3 bench/cache_ab.py --hot-size 20GB --prefix-tokens 16384 \
      --repeats 6 --churn 4 || note "phase A failed, continuing"
fi

# ---------------------------------------------------------------------------
if wants B && have_time 150; then
  step "B1. is the MTP 1.8x real across batch sizes?"
  # The overnight step-2 timings implied MTP is worth only about 11% at batch 8,
  # against 1.8x measured at batch 1. If that holds, the shipped variant is
  # tuned for a serving mode nobody uses.
  # oMLX rejects batch_size 1 (BenchmarkRequest allows only 2, 4 and 8); the
  # single-stream row the harness always emits alongside is the batch-1 point.
  python3 bench/set_variant.py apply mtp || true
  python3 bench/throughput.py mtp_batchsweep --lengths 4096 --gen 256 \
      --batch 2 4 8 || note "mtp batch sweep failed"
  python3 bench/set_variant.py apply plain || true
  python3 bench/throughput.py plain_batchsweep --lengths 4096 --gen 256 \
      --batch 2 4 8 || note "plain batch sweep failed"
  python3 bench/set_variant.py apply mtp || true

  step "B2. real concurrency across profiles"
  python3 bench/concurrency.py || note "phase B2 failed, continuing"
fi

# ---------------------------------------------------------------------------
if wants C && have_time 90; then
  step "C1. scheduler.chunked_prefill A/B at long prompts"
  # Flagged in FINDINGS section 8 as "worth an A/B at long context" and never
  # done. It is a global setting applied at runtime, so a model reload is enough.
  for CP in true false; do
    python3 - "$CP" <<'PY' || true
import sys
sys.path.insert(0, "bench")
from omlx_client import Omlx
val = sys.argv[1] == "true"
o = Omlx()
o.update_global_settings({"chunked_prefill": val})
got = (o.global_settings().get("scheduler") or {}).get("chunked_prefill")
print(f"  chunked_prefill set to {val}, server reports {got}")
o.reload_model()
print("  model reloaded")
PY
    python3 bench/throughput.py "chunkpf_${CP}" --lengths 16384 65536 131072 \
        --gen 128 || note "chunked_prefill=$CP sweep failed"
  done

  step "C2. specprefill A/B at long prompts"
  # Never examined in any session. It targets prefill, which is the dominant
  # remaining cost. Caveat: oMLX calls it experimental and MoE-oriented, and
  # this is a dense hybrid-attention checkpoint, so it may not engage at all.
  python3 bench/set_variant.py apply mtp_specprefill || true
  python3 bench/throughput.py mtp_specprefill --lengths 16384 65536 131072 \
      --gen 128 || note "specprefill sweep failed"
  python3 bench/set_variant.py apply mtp || true
fi

# ---------------------------------------------------------------------------
if wants D && have_time 240; then
  step "D. long-context quality: does 262k do anything beyond being admitted?"
  python3 bench/longctx_quality.py || note "phase D failed, continuing"
fi

# ---------------------------------------------------------------------------
if wants E && have_time 540; then
  step "E. 4-bit quantisation comparison"
  # The only axis never measured. A 4-bit build could decode faster and free
  # ~12 GB, which is what the hot cache from phase A would want.
  python3 bench/quant_compare.py --samples "$SAMPLES" || note "phase E failed"
fi

COMPLETED=1
step "all requested phases done"
