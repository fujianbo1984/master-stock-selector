from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..watchlist.historical_backfill import (
    HistoricalBackfillConfig,
    backfill_market_history,
)


def handle(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    del parser
    result = backfill_market_history(
        HistoricalBackfillConfig(
            market_database=Path(args.market_database),
            from_date=str(args.from_date),
            to_date=str(args.to_date),
            minimum_stock_count=int(args.minimum_stock_count),
            minimum_coverage_ratio=float(args.minimum_coverage_ratio),
        ),
        apply=bool(args.apply),
        progress=lambda message: print(message, file=sys.stderr, flush=True),
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0
