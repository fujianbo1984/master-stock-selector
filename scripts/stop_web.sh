#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
HOST="${MASTERSTOCK_WEB_HOST:-127.0.0.1}"
PORT="${MASTERSTOCK_WEB_PORT:-8000}"
PID_FILE="${MASTERSTOCK_WEB_PID_FILE:-$PROJECT_DIR/.masterstock-web.pid}"
STOP_WAIT_SECONDS="${MASTERSTOCK_WEB_STOP_WAIT_SECONDS:-10}"

stop_pid() {
  local pid="$1"

  if ! kill -0 "$pid" 2>/dev/null; then
    if lsof -t -p "$pid" >/dev/null 2>&1; then
      echo "cannot signal process $pid; permission denied" >&2
      return 1
    fi
    return 0
  fi

  if ! kill "$pid" 2>/dev/null; then
    echo "cannot stop process $pid; permission denied" >&2
    return 1
  fi
  for ((attempt = 1; attempt <= STOP_WAIT_SECONDS; attempt++)); do
    if ! kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
    sleep 1
  done

  echo "process $pid did not stop within ${STOP_WAIT_SECONDS}s; sending SIGKILL" >&2
  kill -KILL "$pid" 2>/dev/null || true
}

if [[ -f "$PID_FILE" ]]; then
  pid="$(<"$PID_FILE")"
  if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    stop_pid "$pid"
    rm -f "$PID_FILE"
    echo "web stopped (pid $pid)"
    exit 0
  fi
  rm -f "$PID_FILE"
fi

listener_pids="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN -nP 2>/dev/null || true)"
if [[ -z "$listener_pids" ]]; then
  echo "web is not running (no PID file or listener on $HOST:$PORT)"
  exit 0
fi

while IFS= read -r pid; do
  [[ "$pid" =~ ^[0-9]+$ ]] || continue
  stop_pid "$pid"
done <<< "$listener_pids"

rm -f "$PID_FILE"
echo "web stopped by listener on $HOST:$PORT"
