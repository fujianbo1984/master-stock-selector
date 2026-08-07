from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..watchlist.service import WatchlistRunConfig, run_watchlist


def handle(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.command != "watchlist":
        parser.error("unknown watchlist command")
        return 2
    payload = run_watchlist(
        WatchlistRunConfig(
            market_database=Path(args.market_database),
            watchlist_database=Path(args.watchlist_database),
            as_of_date=str(args.date or ""),
            from_date=str(args.from_date or ""),
            origin=str(args.origin).upper(),
        )
    )
    print(json.dumps(payload, ensure_ascii=False))
    return 0
