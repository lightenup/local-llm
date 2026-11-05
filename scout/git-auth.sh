# Sourced by the Hermes wrappers. `scout_git` is git with read access to this repo
# when it is private: the token comes from GITHUB_PAT_HERMES (the agent's
# env_passthrough, homelab-proxmox backlog 099) and reaches git through a credential
# helper, so it is never in the clone URL, .git/config or a log line. Without the
# variable it is plain git, which is enough while the repo is public.
export GIT_TERMINAL_PROMPT=0   # no TTY under cron: fail instead of waiting for a password

scout_git() {
  if [ -n "${GITHUB_PAT_HERMES:-}" ]; then
    git -c credential.helper= \
        -c credential.helper='!f() { test "$1" = get && printf "username=x-access-token\npassword=%s\n" "$GITHUB_PAT_HERMES"; }; f' \
        "$@"
  else
    git "$@"
  fi
}
