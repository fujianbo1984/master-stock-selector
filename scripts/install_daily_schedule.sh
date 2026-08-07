#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
TEMPLATE="$PROJECT_DIR/deploy/launchd/com.masterstock.daily.plist.example"
ENV_FILE="$PROJECT_DIR/.env"
AGENTS_DIR="${MASTERSTOCK_LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}"
DESTINATION="$AGENTS_DIR/com.masterstock.daily.plist"
DOMAIN="gui/$(id -u)"

fail() {
  echo "daily schedule install refused: $*" >&2
  exit 1
}

[[ -f "$TEMPLATE" ]] || fail "missing template: $TEMPLATE"
[[ -x "$PROJECT_DIR/.venv/bin/masterstock" ]] || fail "missing project CLI"
[[ -f "$ENV_FILE" ]] || fail "missing $ENV_FILE; copy .env.example and set TUSHARE_TOKEN"
grep -Eq '^TUSHARE_TOKEN=.+$' "$ENV_FILE" || fail "TUSHARE_TOKEN is empty in $ENV_FILE"

mkdir -p "$AGENTS_DIR" "$PROJECT_DIR/logs"
temporary_plist="$(mktemp)"
trap 'rm -f "$temporary_plist"' EXIT INT TERM
sed "s#__PROJECT_DIR__#$PROJECT_DIR#g" "$TEMPLATE" >"$temporary_plist"
plutil -lint "$temporary_plist" >/dev/null
install -m 0644 "$temporary_plist" "$DESTINATION"

launchctl bootout "$DOMAIN" "$DESTINATION" >/dev/null 2>&1 || true
launchctl bootstrap "$DOMAIN" "$DESTINATION"
launchctl enable "$DOMAIN/com.masterstock.daily"
echo "daily schedule installed: weekdays 17:10 Asia/Shanghai"
echo "plist: $DESTINATION"
