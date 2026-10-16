#!/bin/bash
# Render a LaunchAgent from its template and (re)load it.
#
#   proxy/launchd/install.sh llm-queue            # render, install, bootstrap
#   proxy/launchd/install.sh --render llm-queue   # print the plist, change nothing
#   proxy/launchd/install.sh --remove llm-queue   # bootout and delete
#
# launchd does not expand variables, so the paths are filled in here. Values
# come from .env.local at the repo root if present, else these defaults:
#
#   LAUNCHD_PREFIX  label prefix; the label is $LAUNCHD_PREFIX.<name>   local
#   LLM_LOG_DIR     stdout/stderr and the queue's event log    ~/Library/Logs/llm_queue
#
# bench/doctor.py reads the same LAUNCHD_PREFIX, so change both together.
set -eu

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
# shellcheck disable=SC1091
[ -f "$repo/.env.local" ] && . "$repo/.env.local"
prefix="${LAUNCHD_PREFIX:-local}"
log_dir="${LLM_LOG_DIR:-$HOME/Library/Logs/llm_queue}"

mode=install
case "${1:-}" in
  --render) mode=render; shift ;;
  --remove) mode=remove; shift ;;
esac
name="${1:?usage: install.sh [--render|--remove] llm-queue|inference-awake}"
template="$here/$name.plist.template"
[ -f "$template" ] || { echo "no template: $template" >&2; exit 1; }

label="$prefix.$name"
target="$HOME/Library/LaunchAgents/$label.plist"
domain="gui/$(id -u)"

render() {
  sed -e "s#@PREFIX@#$prefix#g" -e "s#@REPO@#$repo#g" -e "s#@LOG_DIR@#$log_dir#g" "$template"
}

case "$mode" in
  render) render ;;
  remove)
    launchctl bootout "$domain/$label" 2>/dev/null || true
    rm -f "$target"
    echo "removed $label" ;;
  install)
    mkdir -p "$log_dir" "$(dirname "$target")"
    render > "$target"
    plutil -lint "$target" >/dev/null
    launchctl bootout "$domain/$label" 2>/dev/null || true
    launchctl bootstrap "$domain" "$target"
    echo "loaded $label from $target" ;;
esac
