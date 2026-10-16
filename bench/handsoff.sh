#!/bin/bash
# Hands-off closeout for the gemma-4-31B evaluation.
#
# Everything that still needed measuring after FINDINGS §32, arranged so it can
# run without supervision and leave the machine exactly as it found it.
#
# WHAT IS LEFT, and why each is here:
#
#   1. BISECT THE CONTEXT CEILING. §32 verified 65,215 tokens and saw hard
#      refusals at 131k. The true limit is somewhere in (65536, 131072] and
#      nothing has bisected it. It matters because it is the input to the
#      `build` decision: a 96k ceiling makes whole-repo work plausible and a
#      70k one does not. Binary search on the guard's accept/refuse, which is
#      deterministic, so 4 probes settle it to ~4k.
#   2. RETRIEVAL AT THE FOUND CEILING. A length the guard admits is not a
#      length the model attends to -- that distinction is the whole reason
#      bench/longctx_quality.py exists (§8). Admitting 96k and retrieving
#      nothing at depth would be worse than a 65k cap.
#   3. VERIFY THE WIRING. `plan` now points at g4-reason. Confirm the served id
#      resolves, answers, and does not leak reasoning into content.
#   4. RESTORE AND REPORT.
#
# NOT here, deliberately: the `build` decision (a workload judgement, not a
# measurement), and D6's episode harness (~2 days, and it needs design input).
#
# The lineup is restored on any exit, including a kill. Nothing in this script
# edits configuration; the only mutation is loading and unloading models.
set -u
cd "$(dirname "$0")/.." || exit 1

GEMMA="gemma-4-31B-it-qat-OptiQ-4bit"
LINEUP_DEFAULT="Qwen3.8-27B-8bit Qwen3.8-27B-oQ4e-mtp Qwen2.5-Coder-3B-8bit"
BASELINE_FILE="$(mktemp -t handsoff_baseline)"
DEADLINE="${DEADLINE:-$(date -v+1d '+%Y-%m-%d' 2>/dev/null || date '+%Y-%m-%d') 09:00}"
DEADLINE_EPOCH=$(date -j -f "%Y-%m-%d %H:%M" "$DEADLINE" +%s 2>/dev/null || echo 0)
if [ "$DEADLINE_EPOCH" -eq 0 ]; then
  echo "!! could not parse DEADLINE='$DEADLINE'; refusing to run unbounded"
  exit 2
fi

note() { printf '\n======== %s  [%s]\n' "$1" "$(date '+%H:%M:%S')"; }
have_time() {
  local need=$(( $1 * 60 )) now; now=$(date +%s)
  if [ $(( now + need )) -gt "$DEADLINE_EPOCH" ]; then
    note "skipping $2: needs ~$1 min, deadline is $DEADLINE"; return 1
  fi; return 0
}

restore() {
  local rc=$?
  note "restoring the lineup"
  python3 - "$GEMMA" "$BASELINE_FILE" "$LINEUP_DEFAULT" <<'PY' || true
import json, sys
from pathlib import Path
sys.path.insert(0, "bench")
from omlx_client import Omlx
gemma, bf, fallback = sys.argv[1], sys.argv[2], sys.argv[3].split()
o = Omlx()
try:
    baseline = json.loads(Path(bf).read_text()) or fallback
except Exception:
    baseline = fallback
for m in [x["id"] for x in o.models() if x.get("loaded") and x["id"] not in baseline]:
    try:
        o.unload(m)
    except Exception as e:
        print(f"  unload {m}: {str(e)[:90]}")
for m in baseline:
    try:
        o.load(m)
    except Exception as e:
        print(f"  load {m}: {str(e)[:110]}")
print("  loaded:", [x["id"] for x in o.models() if x.get("loaded")])
PY
  python3 profiles/apply.py --verify-only || true
  rm -f "$BASELINE_FILE"
  note "hands-off run finished (exit $rc)"
  exit $rc
}
trap restore EXIT INT TERM

note "0. state before"
python3 - "$GEMMA" "$BASELINE_FILE" <<'PY' || exit 1
import json, sys, time
from pathlib import Path
sys.path.insert(0, "bench")
from omlx_client import Omlx
gemma, bf = sys.argv[1], sys.argv[2]
o = Omlx()
base = [x["id"] for x in o.models() if x.get("loaded") and x["id"] != gemma]
Path(bf).write_text(json.dumps(base))
print("  baseline lineup:", base)
print("  pool %.2f GiB" % (o.health()["engine_pool"]["current_model_memory"] / 2**30))
PY

# --------------------------------------------------------------- phase 1+2
if have_time 90 "context bisection and retrieval"; then
  note "1. bisect the context ceiling in (65536, 131072], gemma alone"
  python3 bench/ctx_bisect.py --model "$GEMMA" --profile g4-code \
    --low 65536 --high 131072 --label ctx_bisect_gemma4 \
    || note "bisection failed, continuing"
fi

# --------------------------------------------------------------- phase 3
if have_time 15 "wiring verification"; then
  note "3. verify the plan wiring end to end"
  python3 bench/verify_wiring.py || note "wiring verification reported problems"
fi

note "4. summary"
python3 - <<'PY' || true
import json
from pathlib import Path
p = Path("results/ctx_bisect_gemma4.json")
if p.exists():
    d = json.loads(p.read_text())
    print(f"  context ceiling: admitted up to {d.get('max_admitted')} tokens, "
          f"refused at {d.get('min_refused')}")
    r = d.get("retrieval") or {}
    if r:
        print(f"  retrieval at {r.get('tokens')}: {r.get('correct')}/{r.get('asked')}")
    print(f"  recommended cap: {d.get('recommended_cap')}")
else:
    print("  no bisection result on disk")
PY
