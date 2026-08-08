#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
USER_DATABASE="${MASTERSTOCK_USER_DATABASE:-$PROJECT_DIR/data/users.sqlite3}"
BACKUP_DIR="${MASTERSTOCK_BACKUP_DIR:-$PROJECT_DIR/backups}"
RETENTION_DAYS="${MASTERSTOCK_BACKUP_RETENTION_DAYS:-7}"

[[ -f "$USER_DATABASE" ]] || { echo "缺少用户数据库：$USER_DATABASE" >&2; exit 1; }

exec "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/scripts/backup_sqlite.py" \
  --source "$USER_DATABASE" --destination "$BACKUP_DIR" --retention-days "$RETENTION_DAYS"
