from __future__ import annotations

import os

from .parser_types import ParserRegistrar, SubparserRegistry


def register_market_backfill(subparsers: SubparserRegistry) -> None:
    parser = subparsers.add_parser(
        "market-backfill",
        help="仅补齐本地行情库中已有股票交易日的 Tushare 历史缺口",
    )
    parser.add_argument("--from", dest="from_date", required=True)
    parser.add_argument("--to", dest="to_date", default="")
    parser.add_argument(
        "--market-database",
        default=os.environ.get("MASTERSTOCK_MARKET_DATABASE", "data/market.sqlite3"),
    )
    parser.add_argument("--minimum-stock-count", type=int, default=4500)
    parser.add_argument("--minimum-coverage-ratio", type=float, default=0.95)
    parser.add_argument("--apply", action="store_true")


REGISTRARS: dict[str, ParserRegistrar] = {"market-backfill": register_market_backfill}
