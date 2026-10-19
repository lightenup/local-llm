#!/usr/bin/env bash
# Model scout as a Hermes Agent no-agent cron job (homelab-proxmox backlog 083, FR-017).
#
# Hermes delivers this script's stdout, so the scout runs with --quiet: a
# markdown report when something is new, one line saying so otherwise. A
# non-zero exit is reported by Hermes as a failed run. The JSON report also goes
# to $SCOUT_REPORT for the assessment job (scout/hermes-assess.sh).
#
# Install inside the agent (Hermes only runs scripts from its scripts dir). The
# installed copy only execs this file in the clone, so changes here arrive with
# the next `git pull` instead of needing a re-copy:
#   printf '#!/usr/bin/env bash\nexec bash "$HERMES_HOME/workspace/local-llm/scout/hermes-cron.sh"\n' \
#     > "$HERMES_HOME/scripts/model-scout.sh"
#   hermes cron create "30 7 * * *" --no-agent --script model-scout.sh \
#     --deliver ntfy --name model-scout
#   hermes cron run <job_id>          # once, to see it deliver
#
# Everything it keeps lives under $HERMES_HOME (/opt/data in the official image),
# so the agent's backup covers the clone and the seen-repo state.
set -euo pipefail

home="${HERMES_HOME:-$HOME/.hermes}"
repo="${SCOUT_REPO:-$home/workspace/local-llm}"
export SCOUT_STATE="${SCOUT_STATE:-$home/state/model_scout.json}"
report="${SCOUT_REPORT:-$home/state/model_scout_report.json}"

if [ -d "$repo/.git" ]; then
  # A stale clone still scans correctly; say so rather than fail the run.
  git -C "$repo" pull -q --ff-only || echo "warning: git pull failed, using the existing clone" >&2
else
  mkdir -p "$(dirname "$repo")"
  git clone -q --depth 1 https://github.com/lightenup/local-llm.git "$repo"
fi

exec python3 "$repo/bench/model_scout.py" scan --quiet --report-out "$report"
