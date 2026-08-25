#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.sero.hot-chips-x-monitor"
DOMAIN="gui/$(id -u)"
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"
TEMPLATE="$ROOT/launchd/$LABEL.plist.template"

umask 077
mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/state" "$ROOT/docs"
chmod 700 "$ROOT/state"
chmod 755 "$ROOT/docs"
chmod 700 "$ROOT/run-monitor.sh" "$ROOT/run-and-publish.sh" "$ROOT/publish-pages.sh" "$ROOT/serve-index.sh" "$ROOT/install-launchd.sh"

/usr/bin/python3 - "$TEMPLATE" "$TARGET" "$ROOT" <<'PY'
from pathlib import Path
import sys
template, target, root = map(Path, sys.argv[1:])
target.write_text(template.read_text().replace("__ROOT__", str(root)), encoding="utf-8")
target.chmod(0o600)
PY

/usr/bin/plutil -lint "$TARGET"
/bin/launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
/bin/launchctl bootstrap "$DOMAIN" "$TARGET"
/bin/launchctl kickstart -k "$DOMAIN/$LABEL"
/bin/launchctl print "$DOMAIN/$LABEL"
