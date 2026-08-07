from __future__ import annotations

import os

from .parser_types import ParserRegistrar, SubparserRegistry


def register_watchlist(subparsers: SubparserRegistry) -> None:
    parser = subparsers.add_parser(
        "watchlist",
        help="用 Weinstein 与 Minervini 从本地行情生成大师观察池事实",
    )
    parser.add_argument("--date", default="", help="数据截止交易日；默认使用本地最新日期")
    parser.add_argument("--from", dest="from_date", default="", help="可选历史重建起点")
    parser.add_argument(
        "--origin",
        choices=("observed", "reconstructed"),
        default="observed",
        help="真实当日保存或历史重建；历史日期必须使用 reconstructed",
    )
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
    parser.add_argument("--apply", action="store_true")


REGISTRARS: dict[str, ParserRegistrar] = {"watchlist": register_watchlist}
