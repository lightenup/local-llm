#!/usr/bin/env bash
# Model scout as a Hermes Agent no-agent cron job (homelab-proxmox backlog 083, FR-017).
#
# Hermes delivers this script's stdout and stays silent when it is empty, so the
# scout runs with --quiet: a markdown report when something is new, nothing
# otherwise. A non-zero exit is reported by Hermes as a failed run.
#
# Install inside the agent (Hermes only runs scripts from its scripts dir):
#   cp scout/hermes-cron.sh "$HERMES_HOME/scripts/model-scout.sh"
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

if [ -d "$repo/.git" ]; then
  # A stale clone still scans correctly; say so rather than fail the run.
  git -C "$repo" pull -q --ff-only || echo "warning: git pull failed, using the existing clone" >&2
else
  mkdir -p "$(dirname "$repo")"
  git clone -q --depth 1 https://github.com/lightenup/local-llm.git "$repo"
fi

exec python3 "$repo/bench/model_scout.py" scan --quiet
