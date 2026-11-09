#!/usr/bin/env bash
# Role assessment as a Hermes Agent cron job with a gate script (docs/SCOUT.md,
# "Assessment job"; homelab-proxmox backlog 088).
#
# Hermes runs this every tick. Empty stdout is a silent tick with no model call;
# anything printed becomes the agent's input. `assess-gate` prints one pending
# promising family, with scout/assess.md as instructions, only when the Mac
# answers a 1-token probe through the router. So a sleeping Mac costs nothing,
# and the first tick after it wakes is the retry.
#
# Hermes strips the agent's own router key from job scripts (homelab 083 R-004),
# so the probe uses a key of its own, passed in through the agent's
# env_passthrough (homelab backlog 088 FR-008). Without it the probe gets 401
# and every tick stays silent.
#
# The installed copy sets what must not live in this repo, then execs
# this file in the clone:
#   cat > "$HERMES_HOME/scripts/scout-assess.sh" <<'WRAP'
#   #!/usr/bin/env bash
#   export SCOUT_ROUTER_URL=http://<llm-router-prod>:4000/v1
#   export SCOUT_ROUTER_KEY_ENV=LLM_ROUTER_CLIENT_SCOUT_PROBE_KEY
#   exec bash "$HERMES_HOME/workspace/local-llm/scout/hermes-assess.sh"
#   WRAP
#   hermes cron create "*/15 * * * *" --script scout-assess.sh --model hermes-deep \
#     --deliver ntfy --name scout-assess "Follow the instructions in the Script Output."
set -euo pipefail

home="${HERMES_HOME:-$HOME/.hermes}"
repo="${SCOUT_REPO:-$home/workspace/local-llm}"
export SCOUT_ASSESS_STATE="${SCOUT_ASSESS_STATE:-$home/state/scout_assess.json}"
# The same lock as hermes-cron.sh, for the reason given there.
git_lock="$home/state/scout-git.lock"
git_lock_wait="${SCOUT_GIT_LOCK_WAIT:-240}"

# model-scout.sh keeps the clone current and reports a failed pull in its daily
# message. Here any stdout starts a model call, so a failure only goes to stderr.
. "$repo/scout/git-auth.sh" 2>/dev/null || scout_git() { git "$@"; }
mkdir -p "$home/state"
# Fetch and force onto main, not `pull`, for the reason in hermes-cron.sh. The
# subshell releases the lock before the exec below.
(
  flock -w "$git_lock_wait" 9 \
    || { echo "timed out after ${git_lock_wait}s waiting for the scout git lock" >&2; exit 75; }
  scout_git -C "$repo" fetch -q --depth 1 origin main \
    && git -C "$repo" checkout -q -f -B main FETCH_HEAD
) 9>"$git_lock" >&2 || true
exec python3 "$repo/bench/model_scout.py" assess-gate \
  --report "${SCOUT_REPORT:-$home/state/model_scout_report.json}" \
  --jobs "$home/cron/jobs.json" --job-name "${SCOUT_ASSESS_JOB:-scout-assess}"
