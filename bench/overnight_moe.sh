#!/usr/bin/env bash
# Corrected overnight: CODE axis for both MoE candidates (their actual tier job),
# hard deadline 06:30. Speed (P0 decode/MTP) and the agentic toolcall touchpoint
# are already measured (results/decode_*.json, results/task_tc_*.json).
#
# livecodebench is a STATIC-CODE FLOOR here, not the agentic verdict: FINDINGS
# 14/27 show it is blind to the agentic-coding gap these coders are built for.
# Read a null result as "no static-code regression", not "no better".
#
#   P1 Tiel-Coder code @500   vs ship_code, gemma4_code, oq4e_code   ~4.5-5.5h
#   P2 Novaeon    code @250   vs ship_code (directional)             ~2-2.5h
set -u
cd "$(dirname "$0")/.." || exit 1

TIEL="Tiel-Coder-35B-A3B-MLX-oQ4e"
NOVA="Qwen3.8-35B-A3B-Distill-oQ8-fp16-mtp"
LINEUP=(Qwen2.5-Coder-3B-8bit Qwen3.8-27B-8bit Qwen3.8-27B-oQ4e-mtp)
LOG="results/overnight_moe.log"

DEADLINE_TS=$(python3 -c "import datetime as dt;now=dt.datetime.now();t=now.replace(hour=6,minute=30,second=0,microsecond=0);\
t=t+dt.timedelta(days=1) if now>=t else t;print(int(t.timestamp()))")
say(){ echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }
have_time(){ local n; n=$(date +%s); [ $((DEADLINE_TS-n)) -ge "$1" ]; }
remain(){ echo $(( (DEADLINE_TS-$(date +%s))/60 ))m; }

free(){ python3 - "$1" <<'PY' 2>&1 | tee -a "$LOG"
import sys,time; sys.path.insert(0,"bench")
from omlx_client import Omlx
o=Omlx()
try: o.unload(sys.argv[1]); print("  unloaded",sys.argv[1])
except Exception as e: print("  unload skip",str(e)[:70])
time.sleep(3)
PY
}
restore(){
  say "restore: unload candidates, re-pin lineup, re-apply profiles"
  python3 - "$TIEL" "$NOVA" "${LINEUP[@]}" <<'PY' 2>&1 | tee -a "$LOG"
import sys,time; sys.path.insert(0,"bench")
from omlx_client import Omlx
tiel,nova,*lineup=sys.argv[1:]; o=Omlx()
try: o.post("/admin/api/bench/accuracy/cancel", {})
except Exception: pass
for m in (tiel,nova):
    try: o.unload(m)
    except Exception: pass
time.sleep(3)
for m in lineup:
    try: o.load(m)
    except Exception as e: print("  reload",m,str(e)[:80])
print("  loaded:",[m['id'] for m in o.models() if m.get('loaded')])
PY
  python3 profiles/apply.py 2>&1 | tail -3 | tee -a "$LOG" || true
}
trap restore EXIT

pair(){ local new=$1; shift
  for b in "$@"; do
    [ -f "results/${b}.json" ] || { say "  (no ${b}.json)"; continue; }
    say "  paired: ${new} vs ${b}"
    python3 bench/paired_compare.py "$new" "$b" 2>&1 | tee -a "$LOG" || true
  done
}

say "overnight code start; deadline 06:30 ($(remain) left)"

# P1: Tiel-Coder code @500 -- the coder's static-code floor
if have_time 18000; then
  say "P1 Tiel code @500  [$(remain) left]"
  free "$NOVA"
  python3 bench/accuracy.py code --model "$TIEL" --label tiel_code \
    --samples 500 --batch 8 2>&1 | tee -a "$LOG" || say "P1 failed"
  pair acc_tiel_code acc_ship_code acc_gemma4_code acc_oq4e_code
else say "P1 skipped (deadline)"; fi

# P2: Novaeon code @250 -- the 8-bit's axis, directional
if have_time 9000; then
  say "P2 Novaeon code @250  [$(remain) left]"
  free "$TIEL"
  python3 bench/accuracy.py code --model "$NOVA" --label novaeon_code \
    --samples 250 --batch 8 2>&1 | tee -a "$LOG" || say "P2 failed"
  pair acc_novaeon_code acc_ship_code acc_gemma4_code
else say "P2 skipped (deadline)"; fi

say "overnight code DONE ($(remain) to deadline)"
