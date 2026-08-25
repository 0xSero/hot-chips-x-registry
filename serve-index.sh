#!/bin/zsh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
exec /usr/bin/python3 -m http.server 8787 --bind 127.0.0.1 --directory "$ROOT/docs"
