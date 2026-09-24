#!/bin/sh
# Install or reload the launchd agent that keeps the desk on 127.0.0.1:8800.
set -eu
LABEL=com.yanodintsov.orchestrator-desk
SRC="$(cd "$(dirname "$0")" && pwd)/launchd/$LABEL.plist"
DST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

cp "$SRC" "$DST"
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$DST"
echo "installed: http://127.0.0.1:8800"
