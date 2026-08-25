#!/bin/zsh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
"$ROOT/run-monitor.sh"
"$ROOT/publish-pages.sh"
