#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

[ "$(git branch --show-current)" = "main" ] || { print -u2 "Refusing publish outside main"; exit 1; }
git remote get-url origin >/dev/null
git fetch origin main --prune --quiet
[ "$(git rev-parse main)" = "$(git rev-parse origin/main)" ] || {
  print -u2 "Refusing publish: local main and origin/main diverged"
  exit 1
}

if [ -n "$(git status --porcelain -- . ':(exclude)docs')" ]; then
  print -u2 "Refusing publish while non-docs project files are dirty"
  exit 1
fi

git add docs
git diff --cached --quiet && exit 0
git commit -m "Update Hot Chips registry $(date -u '+%Y-%m-%d %H:%M UTC')" --quiet
git push origin main --quiet
git fetch origin main --prune --quiet
[ "$(git rev-parse main)" = "$(git rev-parse origin/main)" ]
