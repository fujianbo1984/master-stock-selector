#!/usr/bin/env bash
set -euo pipefail

AGENTS_DIR="${MASTERSTOCK_LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}"
DESTINATION="$AGENTS_DIR/com.masterstock.daily.plist"
DOMAIN="gui/$(id -u)"

if [[ -f "$DESTINATION" ]]; then
  launchctl bootout "$DOMAIN" "$DESTINATION" >/dev/null 2>&1 || true
  rm -f "$DESTINATION"
fi
echo "daily schedule removed: $DESTINATION"
