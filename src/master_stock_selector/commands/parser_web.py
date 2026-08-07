from __future__ import annotations

import os

from .parser_types import ParserRegistrar, SubparserRegistry


def register_web(subparsers: SubparserRegistry) -> None:
    parser = subparsers.add_parser("web", help="启动大师选股网站")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--market-database",
        default=os.environ.get("MASTERSTOCK_MARKET_DATABASE", "data/market.sqlite3"),
    )
    parser.add_argument(
        "--watchlist-database",
        default=os.environ.get(
            "MASTERSTOCK_WATCHLIST_DATABASE", "data/master_watchlist.sqlite3"
        ),
    )


REGISTRARS: dict[str, ParserRegistrar] = {"web": register_web}
