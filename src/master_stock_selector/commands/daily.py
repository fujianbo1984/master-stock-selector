from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..watchlist.daily import DailyRunConfig, run_daily_closed_loop


def handle(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.command != "daily":
        parser.error("unknown daily command")
        return 2
    payload = run_daily_closed_loop(
        DailyRunConfig(
            market_database=Path(args.market_database),
            watchlist_database=Path(args.watchlist_database),
            trade_date=str(args.date or ""),
            minimum_stock_count=max(1, int(args.minimum_stock_count)),
            minimum_coverage_ratio=float(args.minimum_coverage_ratio),
        )
    )
    print(json.dumps(payload, ensure_ascii=False))
    return 0
