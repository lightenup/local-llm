#!/bin/bash
# A10 step 3: can gemma-4-26B-A4B-oQ6e-mtp take the fast slot from oQ4e?
#
# Step 2 settled speed (FINDINGS §41): it passes the D3 gate, samples on its
# MTP path, prefills ~4x and decodes 1.2-1.6x oQ4e at 4k-16k. Speed is what
# the MoEs in A9 had too, and both failed on code. So this run is the decision.
#
# Arms are paired against what is already on disk, collected the same way
# (bare model id, deterministic sampling, n=500, batch 8):
#   acc_oq4e_code / acc_oq4e_reason   the incumbent in the fast slot
#   acc_ship_code / acc_ship_reason   the 8-bit, for scale
#   acc_gemma4_code / acc_gemma4_reason  the dense gemma, same family
# Deterministic, not the profiles' sampling: every baseline is deterministic,
# and a sampled arm could not be paired against them. §39's greedy bug is a
# profile-path bug; it does not touch these numbers.
#
# mathqa is left out: it scored gemma-31B 27.0% purely on its answer budget
# (FINDINGS §29), and this is the same family and template.
#
# UPLOADS: 500 >= 100, so oMLX uploads to omlx.ai (FINDINGS §11), as it did
# for every baseline it is paired against.
set -u
cd "$(dirname "$0")/.." || exit 1

MOE="gemma-4-26B-A4B-it-oQ6e-mtp"
SAMPLES="${SAMPLES:-500}"
BATCH="${BATCH:-8}"

note() { printf '\n======== %s  [%s]\n' "$1" "$(date '+%H:%M:%S')"; }

note "code group, $SAMPLES samples/suite"
python3 bench/accuracy.py code --model "$MOE" --label g4moe_code \
  --samples "$SAMPLES" --batch "$BATCH" || note "code accuracy failed"

note "reason: mmlu_pro and gsm8k, $SAMPLES samples/suite"
python3 bench/accuracy.py reason --suites mmlu_pro gsm8k --model "$MOE" \
  --label g4moe_reason --samples "$SAMPLES" --batch "$BATCH" || note "reason accuracy failed"

note "D5 instrument check: was anything scored zero for being truncated or empty?"
python3 - <<'PY'
import json
from pathlib import Path
for label, ref in (("g4moe_code", "oq4e_code"), ("g4moe_reason", "oq4e_reason")):
    p = Path(f"results/acc_{label}.json")
    if not p.exists():
        print(f"  {label}: no result file")
        continue
    ref_q = {r["benchmark"]: r["question_results"]
             for r in json.loads(Path(f"results/acc_{ref}.json").read_text())["results"]}
    for r in json.loads(p.read_text())["results"]:
        q = r["question_results"]
        empty = sum(1 for x in q if not str(x.get("predicted") or "").strip())
        re = sum(1 for x in ref_q.get(r["benchmark"], [])
                 if not str(x.get("predicted") or "").strip())
        print(f"  {r['benchmark']:14s} acc {r['accuracy']:.3f}  empty {empty:3d}  "
              f"(oq4e empty {re})")
print("  Many more empties than oq4e is the mathqa signature (§29): investigate first.")
PY

note "paired comparisons"
for pair in "acc_oq4e_code acc_g4moe_code" "acc_ship_code acc_g4moe_code" \
            "acc_gemma4_code acc_g4moe_code" "acc_oq4e_reason acc_g4moe_reason" \
            "acc_ship_reason acc_g4moe_reason" "acc_gemma4_reason acc_g4moe_reason"; do
  set -- $pair
  echo ""
  echo "--- $1 vs $2"
  python3 bench/paired_compare.py "$1" "$2" \
    --save "${2#acc_}_vs_${1#acc_}" || echo "    (comparison failed or an arm is missing)"
done
note "done"
