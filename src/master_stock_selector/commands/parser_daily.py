from __future__ import annotations

import os

from ..watchlist.collector import configured_minimum_stock_count
from .parser_types import ParserRegistrar, SubparserRegistry


def register_daily(subparsers: SubparserRegistry) -> None:
    parser = subparsers.add_parser(
        "daily",
        help="采集当天 Tushare 行情，通过质量门后生成大师观察池",
    )
    parser.add_argument("--date", default="", help="真实运行日；默认今天")
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
    parser.add_argument(
        "--minimum-stock-count",
        type=int,
        default=configured_minimum_stock_count(),
        help="采集质量门的最低完整股票数",
    )
    parser.add_argument(
        "--minimum-coverage-ratio",
        type=float,
        default=0.95,
        help="证券主数据、日线和市值数据的最低覆盖率",
    )
    parser.add_argument("--apply", action="store_true")


REGISTRARS: dict[str, ParserRegistrar] = {"daily": register_daily}
