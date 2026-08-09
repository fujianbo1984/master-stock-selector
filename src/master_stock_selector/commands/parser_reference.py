from __future__ import annotations

import os

from .parser_types import ParserRegistrar, SubparserRegistry


def register_reference_backfill(subparsers: SubparserRegistry) -> None:
    parser = subparsers.add_parser(
        "reference-backfill",
        help="回补名称变更、历史ST与SW2021三级行业时点事实",
    )
    parser.add_argument("--from", dest="from_date", required=True)
    parser.add_argument("--to", dest="to_date", required=True)
    parser.add_argument(
        "--market-database",
        default=os.environ.get("MASTERSTOCK_MARKET_DATABASE", "data/market.sqlite3"),
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--industry-only", action="store_true", help="仅补采SW2021行业成员；复用已落库的名称和ST事实")


def register_reference_materialize(subparsers: SubparserRegistry) -> None:
    parser = subparsers.add_parser(
        "reference-materialize",
        help="把时点名称、ST和SW行业事实压缩为候选库有效期历史",
    )
    parser.add_argument("--from", dest="from_date", required=True)
    parser.add_argument("--to", dest="to_date", required=True)
    parser.add_argument("--market-database", default=os.environ.get("MASTERSTOCK_MARKET_DATABASE", "data/market.sqlite3"))
    parser.add_argument("--watchlist-database", required=True, help="必须指定新的候选库")
    parser.add_argument("--apply", action="store_true")


def register_database_optimize(subparsers: SubparserRegistry) -> None:
    parser = subparsers.add_parser(
        "database-optimize",
        help="从现有公共数据库生成有效期历史结构的部署候选库",
    )
    parser.add_argument("--market-source", required=True)
    parser.add_argument("--market-target", required=True)
    parser.add_argument("--watchlist-source", required=True)
    parser.add_argument("--watchlist-target", required=True)
    parser.add_argument("--market-retention-days", type=int)
    parser.add_argument("--watchlist-retention-days", type=int)
    parser.add_argument("--apply", action="store_true")


def register_database_validate(subparsers: SubparserRegistry) -> None:
    parser = subparsers.add_parser(
        "database-validate",
        help="逐日验证旧快照库与有效期历史候选库等价",
    )
    parser.add_argument("--market-source", required=True)
    parser.add_argument("--market-target", required=True)
    parser.add_argument("--watchlist-source", required=True)
    parser.add_argument("--watchlist-target", required=True)
    parser.add_argument("--market-retention-days", type=int)
    parser.add_argument("--watchlist-retention-days", type=int)


REGISTRARS: dict[str, ParserRegistrar] = {
    "database-optimize": register_database_optimize,
    "database-validate": register_database_validate,
    "reference-backfill": register_reference_backfill,
    "reference-materialize": register_reference_materialize,
}
