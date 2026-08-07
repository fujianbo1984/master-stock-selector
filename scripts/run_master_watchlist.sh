#!/usr/bin/env bash
set -euo pipefail

export TZ="Asia/Shanghai"

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$PROJECT_DIR"
MASTERSTOCK_BIN="${MASTERSTOCK_BIN:-$PROJECT_DIR/.venv/bin/masterstock}"
MARKET_DATABASE="${MASTERSTOCK_MARKET_DATABASE:-$PROJECT_DIR/data/market.sqlite3}"
WATCHLIST_DATABASE="${MASTERSTOCK_WATCHLIST_DATABASE:-$PROJECT_DIR/data/master_watchlist.sqlite3}"
LOCK_DIR="${MASTERSTOCK_WATCHLIST_LOCK_DIR:-$PROJECT_DIR/logs/.master-watchlist.lock}"
RUN_DATE="${MASTERSTOCK_WATCHLIST_DATE:-$(date +%F)}"
ORIGIN="observed"
FROM_DATE=""

fail() {
  echo "master watchlist refused: $*" >&2
  exit 1
}

if [[ $# -eq 2 && "$1" == "--reconstruct-from" ]]; then
  FROM_DATE="$2"
  ORIGIN="reconstructed"
  RUN_DATE="${MASTERSTOCK_WATCHLIST_DATE:-}"
elif [[ $# -ne 0 ]]; then
  fail "usage: scripts/run_master_watchlist.sh [--reconstruct-from YYYY-MM-DD]"
fi

if [[ "$ORIGIN" == "observed" && "$(date +%H%M)" < "1530" ]]; then
  fail "Shanghai time is $(date +%H:%M); observed facts may run only after 15:30"
fi

[[ -x "$MASTERSTOCK_BIN" ]] || fail "missing project CLI: $MASTERSTOCK_BIN"
[[ -f "$MARKET_DATABASE" ]] || fail "missing market database: $MARKET_DATABASE"
mkdir -p "$(dirname "$WATCHLIST_DATABASE")" "$(dirname "$LOCK_DIR")"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  fail "another watchlist run may be active: $LOCK_DIR"
fi

cleanup_lock() {
  rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup_lock EXIT INT TERM

if [[ "$ORIGIN" == "observed" ]]; then
  args=(
    daily
    --date "$RUN_DATE"
    --market-database "$MARKET_DATABASE"
    --watchlist-database "$WATCHLIST_DATABASE"
    --apply
  )
else
  args=(
    watchlist
    --origin reconstructed
    --market-database "$MARKET_DATABASE"
    --watchlist-database "$WATCHLIST_DATABASE"
    --apply
  )
  if [[ -n "$RUN_DATE" ]]; then
    args+=(--date "$RUN_DATE")
  fi
  if [[ -n "$FROM_DATE" ]]; then
    args+=(--from "$FROM_DATE")
  fi
fi

"$MASTERSTOCK_BIN" "${args[@]}"
