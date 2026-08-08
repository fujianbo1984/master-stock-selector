#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
exec "$PROJECT_DIR/.venv/bin/python" -m master_stock_selector.web.users_cli "$@"
