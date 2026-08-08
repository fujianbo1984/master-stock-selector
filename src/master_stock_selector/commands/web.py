from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from ..web.app import create_app


def handle(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.command != "web":
        parser.error("unknown web command")
        return 2
    uvicorn.run(
        create_app(
            market_database=Path(args.market_database),
            watchlist_database=Path(args.watchlist_database),
            user_database=Path(args.user_database),
        ),
        host=args.host,
        port=args.port,
    )
    return 0
