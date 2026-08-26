from __future__ import annotations

import argparse

from .parser_agent import REGISTRARS as AGENT_REGISTRARS
from .parser_backfill import REGISTRARS as BACKFILL_REGISTRARS
from .parser_daily import REGISTRARS as DAILY_REGISTRARS
from .parser_diagnostics import REGISTRARS as DIAGNOSTIC_REGISTRARS
from .parser_reference import REGISTRARS as REFERENCE_REGISTRARS
from .parser_types import ParserRegistrar
from .parser_watchlist import REGISTRARS as WATCHLIST_REGISTRARS
from .parser_web import REGISTRARS as WEB_REGISTRARS

COMMAND_ORDER = (
    "agent", "doctor", "runs", "daily", "market-backfill", "reference-backfill",
    "reference-materialize",
    "database-optimize", "database-validate", "watchlist", "web",
)


def _registrars() -> dict[str, ParserRegistrar]:
    registrars = {
        **AGENT_REGISTRARS,
        **DAILY_REGISTRARS,
        **DIAGNOSTIC_REGISTRARS,
        **BACKFILL_REGISTRARS,
        **REFERENCE_REGISTRARS,
        **WATCHLIST_REGISTRARS,
        **WEB_REGISTRARS,
    }
    if set(registrars) != set(COMMAND_ORDER):
        raise RuntimeError("command parser registry does not match the product CLI")
    return registrars


def build_parser(
    parser_class: type[argparse.ArgumentParser] = argparse.ArgumentParser,
) -> argparse.ArgumentParser:
    parser = parser_class(prog="masterstock")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="运行失败时输出 Python traceback",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    registrars = _registrars()
    for command in COMMAND_ORDER:
        registrars[command](subparsers)
    return parser
