#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
STATE_DIR="$ROOT/state"
LOG_FILE="$STATE_DIR/monitor.log"

umask 077
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
mkdir -p "$STATE_DIR" "$ROOT/docs"
chmod 700 "$STATE_DIR"
chmod 755 "$ROOT/docs"
touch "$LOG_FILE"
chmod 600 "$LOG_FILE"

exec /usr/bin/python3 "$ROOT/hot_chips_monitor.py" >>"$LOG_FILE" 2>&1
