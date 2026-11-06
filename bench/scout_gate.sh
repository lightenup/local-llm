#!/bin/bash
# Download scout candidates and run the D3 gate on each, unattended.
#
#   bash bench/scout_gate.sh mlx-community/flint-mixed-3-4bit mlx-community/flint-oQ8e
#
# The scout's notification gives two commands per candidate (download, then
# candidate_gate.py); this is those, chained, so a launchd one-shot can run
# them while nobody watches. Every repo is queued at once, then each is gated
# as soon as its own download finishes, in argument order: put the one that
# fits alongside the lineup first, so its verdict lands before the big one
# starts evicting.
#
# Gate only, no accuracy suites (D3: those are commissioned after reading the
# gate). candidate_gate.py restores the lineup after each model. Each model
# gets its own results/gate_<id>.json, the label the scout suggests.
set -u
cd "$(dirname "$0")/.." || exit 1
note() { printf '\n======== %s  [%s]\n' "$1" "$(date '+%Y-%m-%d %H:%M:%S')"; }
[ $# -gt 0 ] || { echo "usage: $0 REPO..." >&2; exit 2; }

note "queue downloads"
for repo in "$@"; do
  python3 bench/model_scout.py download "$repo" || echo "  !! could not queue $repo"
done

for repo in "$@"; do
  id="${repo##*/}"
  label="gate_$(printf '%s' "$id" | tr 'A-Z.-' 'a-z__')"
  note "$id: wait for download"
  if ! python3 -u - "$repo" <<'PY'
import sys; sys.path.insert(0, "bench")
from omlx_client import Omlx
task = Omlx().wait_for_download(sys.argv[1], poll=60)
ok = (task.get("status") or "").lower() in ("completed", "complete", "done")
sys.exit(0 if ok else 1)
PY
  then
    echo "  !! download did not complete; skipping the gate for $id"
    continue
  fi
  note "$id: gate -> results/$label.json"
  python3 -u bench/candidate_gate.py "$id" --label "$label" || echo "  !! gate exited non-zero"
done
note "end"
