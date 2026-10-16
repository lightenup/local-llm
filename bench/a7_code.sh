#!/bin/bash
# A7c: the code axis for gemma-4-31B. This is the gate on wiring it anywhere.
#
# Why this suite and not another: livecodebench is what separated Coder-Next
# (43.2 vs 59.8), Flash-Next and OptiQ. gemma-4-31B has NO livecodebench figure
# anywhere, including omlx.ai, so unlike every previous candidate there is no
# prior art to read first (backlog D3 step 1 returns nothing).
#
# POWER, measured from the stored per-question results rather than assumed:
# livecodebench runs 17-21% discordant at n=500, so paired McNemar's floor is
# ~3.6-4.1 points. That is enough to catch a Coder-Next-sized regression (-16.6)
# or an oQ4e-sized one (-5.8), and NOT enough to resolve a 2-point difference.
# If it comes back inside +-4, the honest answer is "no code regression
# detectable", not "equal".
#
# D5 CHECK FIRST. mathqa scored gemma at 27.0% purely because its generation
# budget is sized for one-letter answers (FINDINGS 29). That trap does not
# bind here -- the 8-bit's longest stored livecodebench response is 50,022
# chars (~12.5k tokens), so the budget is generous -- and a probe of the five
# hardest items produced a code block every time. The post-run check below
# re-verifies it from the results rather than trusting that.
#
# UPLOADS: 500 >= 100, so this uploads to omlx.ai (FINDINGS 11). Accepted, and
# necessary: acc_ship_code and acc_oq4e_code were collected the same way and a
# 99-sample run could not be paired against them.
set -u
cd "$(dirname "$0")/.." || exit 1

GEMMA="gemma-4-31B-it-qat-OptiQ-4bit"
SAMPLES="${SAMPLES:-500}"
BATCH="${BATCH:-8}"

note() { printf '\n======== %s  [%s]\n' "$1" "$(date '+%H:%M:%S')"; }

note "gemma-4-31B code group at $SAMPLES samples/suite (~8 h, uploads)"
python3 bench/accuracy.py code --model "$GEMMA" --label gemma4_code \
  --samples "$SAMPLES" --batch "$BATCH" || note "code accuracy failed"

note "D5 instrument check: was anything scored zero for being truncated?"
python3 - <<'PY'
import json
from pathlib import Path
p = Path("results/acc_gemma4_code.json")
if not p.exists():
    print("  no result file; nothing to check")
    raise SystemExit
d = json.loads(p.read_text())
ship = {r["benchmark"]: r["question_results"]
        for r in json.loads(Path("results/acc_ship_code.json").read_text())["results"]}
print(f"  {'suite':14s} {'acc':>6} {'empty_pred':>11} {'ship_empty':>11} "
      f"{'median_len':>11} {'ship_med':>9}")
for r in d["results"]:
    q = r["question_results"]
    empty = sum(1 for x in q if not str(x.get("predicted") or "").strip())
    L = sorted(len(x.get("raw_response") or "") for x in q)
    s = ship.get(r["benchmark"], [])
    se = sum(1 for x in s if not str(x.get("predicted") or "").strip())
    sL = sorted(len(x.get("raw_response") or "") for x in s)
    print(f"  {r['benchmark']:14s} {r['accuracy']:6.3f} {empty:11d} {se:11d} "
          f"{L[len(L)//2]:11d} {(sL[len(sL)//2] if sL else 0):9d}")
print("\n  A large empty_pred count against a small ship_empty count is the "
      "mathqa\n  signature (FINDINGS 29). Investigate BEFORE reading the score.")
PY

note "paired comparisons"
for pair in "acc_ship_code acc_gemma4_code" "acc_oq4e_code acc_gemma4_code"; do
  set -- $pair
  echo ""
  echo "--- $1 vs $2"
  python3 bench/paired_compare.py "$1" "$2" \
    --save "$(echo "$2" | sed 's/^acc_//')_vs_$(echo "$1" | sed 's/^acc_//')" \
    || echo "    (comparison failed or an arm is missing)"
done
