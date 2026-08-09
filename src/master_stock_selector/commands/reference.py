from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..watchlist.database_optimization import (
    optimize_databases,
    validate_database_equivalence,
)
from ..watchlist.reference_data import ReferenceBackfillConfig, collect_reference_data
from ..watchlist.reference_materialization import (
    ReferenceMaterializationConfig,
    materialize_reference_history,
)


def handle(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.command == "database-optimize":
        payload = optimize_databases(
            market_source=Path(args.market_source),
            market_target=Path(args.market_target),
            watchlist_source=Path(args.watchlist_source),
            watchlist_target=Path(args.watchlist_target),
            market_retention_days=args.market_retention_days,
            watchlist_retention_days=args.watchlist_retention_days,
        )
    elif args.command == "database-validate":
        payload = validate_database_equivalence(
            market_source=Path(args.market_source),
            market_target=Path(args.market_target),
            watchlist_source=Path(args.watchlist_source),
            watchlist_target=Path(args.watchlist_target),
            market_retention_days=args.market_retention_days,
            watchlist_retention_days=args.watchlist_retention_days,
        )
    elif args.command == "reference-backfill":
        payload = collect_reference_data(
            ReferenceBackfillConfig(
                market_database=Path(args.market_database),
                from_date=str(args.from_date),
                to_date=str(args.to_date),
                include_names_st=not bool(args.industry_only),
            )
        )
    elif args.command == "reference-materialize":
        payload = materialize_reference_history(
            ReferenceMaterializationConfig(
                market_database=Path(args.market_database),
                watchlist_database=Path(args.watchlist_database),
                from_date=str(args.from_date),
                to_date=str(args.to_date),
            )
        )
    else:
        parser.error("unknown reference command")
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0
