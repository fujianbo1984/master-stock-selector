#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
MASTERSTOCK_BIN="${MASTERSTOCK_BIN:-$PROJECT_DIR/.venv/bin/masterstock}"
HOST="${MASTERSTOCK_WEB_HOST:-127.0.0.1}"
PORT="${MASTERSTOCK_WEB_PORT:-8000}"
MARKET_DATABASE="${MASTERSTOCK_MARKET_DATABASE:-$PROJECT_DIR/data/market.sqlite3}"
WATCHLIST_DATABASE="${MASTERSTOCK_WATCHLIST_DATABASE:-$PROJECT_DIR/data/master_watchlist.sqlite3}"
PID_FILE="${MASTERSTOCK_WEB_PID_FILE:-$PROJECT_DIR/.masterstock-web.pid}"
LOG_FILE="${MASTERSTOCK_WEB_LOG_FILE:-$PROJECT_DIR/logs/web.log}"
WAIT_SECONDS="${MASTERSTOCK_WEB_WAIT_SECONDS:-10}"

fail() {
  echo "start web failed: $*" >&2
  exit 1
}

is_running() {
  local pid="$1"
  kill -0 "$pid" 2>/dev/null
}

if [[ ! -x "$MASTERSTOCK_BIN" ]]; then
  fail "missing executable: $MASTERSTOCK_BIN"
fi

if [[ -f "$PID_FILE" ]]; then
  pid="$(<"$PID_FILE")"
  if [[ "$pid" =~ ^[0-9]+$ ]] && is_running "$pid"; then
    echo "web is already running (pid $pid): http://$HOST:$PORT"
    exit 0
  fi
  rm -f "$PID_FILE"
fi

if command -v curl >/dev/null 2>&1 && curl --noproxy '*' -fsS --max-time 1 "http://$HOST:$PORT/healthz" >/dev/null 2>&1; then
  fail "port $HOST:$PORT is already serving a web process; refusing to claim it"
fi

mkdir -p "$(dirname "$PID_FILE")" "$(dirname "$LOG_FILE")" "$(dirname "$WATCHLIST_DATABASE")"

nohup "$MASTERSTOCK_BIN" web \
  --host "$HOST" \
  --port "$PORT" \
  --market-database "$MARKET_DATABASE" \
  --watchlist-database "$WATCHLIST_DATABASE" \
  >>"$LOG_FILE" 2>&1 &
pid=$!
echo "$pid" >"$PID_FILE"

for ((attempt = 1; attempt <= WAIT_SECONDS; attempt++)); do
  if ! is_running "$pid"; then
    rm -f "$PID_FILE"
    echo "web process exited during startup; log: $LOG_FILE" >&2
    tail -n 40 "$LOG_FILE" >&2 || true
    exit 1
  fi
  if command -v curl >/dev/null 2>&1 && curl --noproxy '*' -fsS --max-time 1 "http://$HOST:$PORT/healthz" >/dev/null 2>&1; then
    echo "web started (pid $pid): http://$HOST:$PORT"
    exit 0
  fi
  sleep 1
done

echo "web process started but health check did not pass within ${WAIT_SECONDS}s (pid $pid)" >&2
echo "log: $LOG_FILE" >&2
exit 1
