from __future__ import annotations

import argparse
import os

from .parser_types import ParserRegistrar, SubparserRegistry


def _database_arguments(parser: argparse.ArgumentParser, *, include_users: bool) -> None:
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
    if include_users:
        parser.add_argument(
            "--user-database",
            default=os.environ.get("MASTERSTOCK_USER_DATABASE", "data/users.sqlite3"),
        )


def _freshness_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--expected-date",
        default="",
        help="应当已完成的交易日 YYYY-MM-DD；省略时从凭证日历和日期间隔判断",
    )
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=4,
        help="无法取得本周权威交易日历时允许的最大日历日间隔（默认 4）",
    )


def register_doctor(subparsers: SubparserRegistry) -> None:
    parser = subparsers.add_parser("doctor", help="只读检查配置、数据库、运行凭证和数据新鲜度")
    _database_arguments(parser, include_users=True)
    _freshness_arguments(parser)
    parser.add_argument(
        "--deep",
        action="store_true",
        help="对市场库和观察池库额外执行 PRAGMA quick_check",
    )


def register_runs(subparsers: SubparserRegistry) -> None:
    parser = subparsers.add_parser("runs", help="只读查看采集与观察池运行凭证")
    _database_arguments(parser, include_users=False)
    actions = parser.add_subparsers(dest="runs_action", required=True)

    latest = actions.add_parser("latest", help="查看最近一次采集与观察池计算")
    _freshness_arguments(latest)
    latest.add_argument(
        "--fail-if-stale",
        action="store_true",
        help="数据过期或日期不一致时返回退出码 3",
    )

    list_parser = actions.add_parser("list", help="分别列出最近的采集与观察池凭证")
    list_parser.add_argument("--limit", type=int, default=20)

    show = actions.add_parser("show", help="按 run_id 查看一条运行凭证")
    show.add_argument("run_id")


REGISTRARS: dict[str, ParserRegistrar] = {
    "doctor": register_doctor,
    "runs": register_runs,
}
