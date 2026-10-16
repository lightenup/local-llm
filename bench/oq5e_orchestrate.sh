#!/usr/bin/env bash
# Unattended: wait for the GBP-DE oQ5e download to finish, then run the
# replacement-mode benchmark chain. Each python step restores the lineup itself.
# Everything logs to results/oq5e_bench.log.
set -uo pipefail
cd "$(dirname "$0")/.."

REPO="GBP-DE/Qwen3.8-Flash-Next-oQ5e-mtp"
MODEL="Qwen3.8-Flash-Next-oQ5e-mtp"
LOG="results/oq5e_bench.log"

say() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }

say "orchestrator start; waiting on download of $REPO"

# 1) block until the hf task leaves a running state
python3 - "$REPO" <<'PY' 2>&1 | tee -a "$LOG"
import sys, time
sys.path.insert(0, "bench")
from omlx_client import Omlx
repo = sys.argv[1]
o = Omlx()
deadline = time.time() + 4*3600
last = ""
while time.time() < deadline:
    t = next((t for t in o.hf_tasks() if t.get("repo_id") == repo), None)
    if t is None:
        print("  no task yet..."); time.sleep(15); continue
    st = (t.get("status") or "").lower()
    msg = f"{st} {round((t.get('downloaded_size') or 0)/1e9,1)}/{round((t.get('total_size') or 0)/1e9,1)}GB"
    if msg != last:
        print("  ", msg, flush=True); last = msg
    if st in ("completed","complete","done"):
        print("  DOWNLOAD COMPLETE"); sys.exit(0)
    if st in ("error","failed","cancelled"):
        print("  DOWNLOAD FAILED:", t.get("error")); sys.exit(3)
    time.sleep(30)
print("  download wait timed out"); sys.exit(4)
PY
dl_rc=${PIPESTATUS[0]}
if [ "$dl_rc" -ne 0 ]; then say "download did not complete (rc=$dl_rc); aborting benchmark"; exit "$dl_rc"; fi

# 2) confirm the model is visible and classify the local build
say "download done; classifying local build"
python3 bench/quant_id.py --all --out results/quant_id_local_oq5e.json 2>&1 | tee -a "$LOG"

# 3) coexistence gate (expected: refuses to serve beside the pinned lineup)
say "candidate_gate: coexistence check"
python3 bench/candidate_gate.py "$MODEL" --label candidate_gate_oq5e 2>&1 | tee -a "$LOG"

# 4) replacement-mode decode: MTP on/off x presence_penalty (the §26 measurement)
say "flashnext_mtp_check: decode + MTP on oQ5e"
python3 bench/flashnext_mtp_check.py --model "$MODEL" --repeats 5 --max-tokens 400 \
    --label fn_oq5e_mtp_check 2>&1 | tee -a "$LOG"

# 5) thinking on/off behaviour + completion sanity
say "flashnext_thinking_ab: thinking behaviour on oQ5e"
python3 bench/flashnext_thinking_ab.py --model "$MODEL" --label fn_oq5e_thinking \
    --repeats 2 --max-tokens 2048 2>&1 | tee -a "$LOG"

say "orchestrator DONE; lineup restored by each step"
python3 - <<'PY' 2>&1 | tee -a "$LOG"
import sys; sys.path.insert(0,"bench")
from omlx_client import Omlx
o=Omlx()
print("  loaded now:", [m["id"] for m in o.models() if m.get("loaded")])
PY
