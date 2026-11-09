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
repo_url="${SCOUT_REPO_URL:-https://github.com/lightenup/local-llm.git}"
export SCOUT_STATE="${SCOUT_STATE:-$home/state/model_scout.json}"
report="${SCOUT_REPORT:-$home/state/model_scout_report.json}"
# This job and scout-assess update the same shallow clone. When two shallow
# fetches overlap, git aborts one ("shallow file has changed since we read it",
# 2026-11-09 07:30) instead of waiting, so both take this lock around the
# update. It lives outside the clone so it also covers the first clone. Taken
# in a subshell, it is released before the scan; `exec 9>` would hand it on.
git_lock="$home/state/scout-git.lock"
git_lock_wait="${SCOUT_GIT_LOCK_WAIT:-240}"
# Takes the lock on fd 9, which the caller has opened on $git_lock.
take_git_lock() {
  command -v flock >/dev/null \
    || { echo "flock is not installed, so the scout git lock cannot be taken" >&2; return 127; }
  flock -w "$git_lock_wait" 9 \
    || { echo "timed out after ${git_lock_wait}s waiting for the scout git lock" >&2; return 75; }
}
. "$(dirname "${BASH_SOURCE[0]}")/git-auth.sh" 2>/dev/null \
  || . "$repo/scout/git-auth.sh" 2>/dev/null \
  || scout_git() { git "$@"; }
mkdir -p "$home/state" "$(dirname "$repo")"

stale=""
if [ -d "$repo/.git" ]; then
  # A stale clone still scans correctly, but on an old policy and setup.json.
  # Say so in the delivered message: on stderr nobody sees it, and a revoked or
  # expired token would look like a quiet week.
  # Fetch main and force the clone onto it rather than `pull`: the clone is
  # read-only, and pull obeys the clone's branch config, which on 2026-11-06
  # named two merge heads and failed with "Cannot fast-forward to multiple
  # branches". This repairs any such state on the next tick.
  if ! err=$( (
        take_git_lock || exit
        scout_git -C "$repo" fetch -q --depth 1 origin main \
          && git -C "$repo" checkout -q -f -B main FETCH_HEAD
      ) 9>"$git_lock" 2>&1); then
    stale="warning: git pull failed, scanned with the clone of $(git -C "$repo" log -1 --format=%cs): ${err%%$'\n'*}"
  fi
else
  (
    take_git_lock || exit
    # A run that waited here behind another first clone finds it done.
    [ -d "$repo/.git" ] || scout_git clone -q --depth 1 "$repo_url" "$repo"
  ) 9>"$git_lock"
fi
status=0
python3 "$repo/bench/model_scout.py" scan --quiet --report-out "$report" || status=$?
[ -z "$stale" ] || printf '\n%s\n' "$stale"
exit "$status"
