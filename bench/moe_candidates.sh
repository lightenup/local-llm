#!/usr/bin/env bash
# Download + gate two qwen3_5_moe candidates, unattended.
#   1. peculiar-ragdoll/Tiel-Coder-35B-A3B-MLX-oQ4e   (code/fast tier challenger)
#   2. NovaeonStudio/Qwen3.8-35B-A3B-Distill-oQ8-fp16-mtp (quality tier challenger)
# candidate_gate.py restores the lineup itself after each run.
set -uo pipefail
cd "$(dirname "$0")/.."
LOG="results/moe_candidates.log"
say() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }

declare -a REPOS=(
  "peculiar-ragdoll/Tiel-Coder-35B-A3B-MLX-oQ4e"
  "NovaeonStudio/Qwen3.8-35B-A3B-Distill-oQ8-fp16-mtp"
)
declare -a IDS=(
  "Tiel-Coder-35B-A3B-MLX-oQ4e"
  "Qwen3.8-35B-A3B-Distill-oQ8-fp16-mtp"
)
declare -a LABELS=(
  "gate_tielcoder"
  "gate_novaeon_distill"
)

# queue both downloads up front
say "queueing downloads"
python3 - "${REPOS[@]}" <<'PY' 2>&1 | tee -a "$LOG"
import sys; sys.path.insert(0,"bench")
from omlx_client import Omlx
o=Omlx()
for repo in sys.argv[1:]:
    try:
        rec=o.hf_download(repo); print("  queued", repo, rec.get("task",{}).get("status"))
    except Exception as e:
        print("  queue error", repo, str(e)[:200])
PY

wait_dl() {  # $1 = repo
  python3 - "$1" <<'PY'
import sys, time
sys.path.insert(0,"bench")
from omlx_client import Omlx
repo=sys.argv[1]; o=Omlx()
deadline=time.time()+4*3600; last=""
while time.time()<deadline:
    t=next((t for t in o.hf_tasks() if t.get("repo_id")==repo),None)
    if t is None: time.sleep(15); continue
    st=(t.get("status") or "").lower()
    msg=f"{st} {round((t.get('downloaded_size') or 0)/1e9,1)}/{round((t.get('total_size') or 0)/1e9,1)}GB"
    if msg!=last: print("   ",repo.split('/')[-1],msg,flush=True); last=msg
    if st in ("completed","complete","done"): sys.exit(0)
    if st in ("error","failed","cancelled"): print("  FAILED",t.get("error")); sys.exit(3)
    time.sleep(30)
sys.exit(4)
PY
}

for i in 0 1; do
  repo="${REPOS[$i]}"; id="${IDS[$i]}"; label="${LABELS[$i]}"
  say "waiting on $repo"
  if wait_dl "$repo" 2>&1 | tee -a "$LOG"; then
    say "downloaded $repo; classifying + gating as $id"
    python3 bench/quant_id.py "$repo" 2>&1 | tee -a "$LOG"
    python3 bench/candidate_gate.py "$id" --label "$label" 2>&1 | tee -a "$LOG"
  else
    say "download failed for $repo; skipping gate"
  fi
done

say "MoE candidate gating DONE"
python3 - <<'PY' 2>&1 | tee -a "$LOG"
import sys; sys.path.insert(0,"bench")
from omlx_client import Omlx
o=Omlx(); print("  loaded now:", [m["id"] for m in o.models() if m.get("loaded")])
PY
