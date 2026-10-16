#!/bin/bash
# Check that the tracked tree is safe to publish. Run before every push.
#
#   scripts/check_public.sh
#
# 1. gitleaks over the tracked files (secrets).
# 2. Every regex in the ```denylist block of the untracked LOCAL.md, plus the
#    generic ones below, against every tracked file. The real values live in
#    LOCAL.md so this script can stay public without naming them.
set -u
cd "$(dirname "$0")/.." || exit 1

fail=0
patterns=('/Users/[A-Za-z.]+/' '\b192\.168\.[0-9]+\.[0-9]+\b')
if [ -f LOCAL.md ]; then
  while IFS= read -r p; do [ -n "$p" ] && patterns+=("$p"); done < <(
    sed -n '/^```denylist$/,/^```$/p' LOCAL.md | sed '1d;$d')
else
  echo "note: no LOCAL.md, checking generic patterns only"
fi

for p in "${patterns[@]}"; do
  if hits=$(git grep -nIE -e "$p" -- . ':!LOCAL.md'); then
    echo "!! /$p/ matches tracked files:"; echo "$hits" | cut -c1-160 | head -20
    fail=1
  fi
done

if command -v gitleaks >/dev/null; then
  # Only tracked files, as they are in the working tree: proxy/.env and
  # results/backup/ hold real keys and are expected to be on disk.
  tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
  git ls-files -z | rsync -a --from0 --files-from=- . "$tmp"
  gitleaks dir "$tmp" --no-banner --redact >/dev/null 2>&1 || {
    echo "!! gitleaks found secrets; run: gitleaks dir . --redact -v"; fail=1; }
else
  echo "note: gitleaks not installed, secret scan skipped"
fi

[ $fail -eq 0 ] && echo "ok: tracked tree is clean"
exit $fail
