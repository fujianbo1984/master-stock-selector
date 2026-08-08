from __future__ import annotations

import argparse

from .parser_backfill import REGISTRARS as BACKFILL_REGISTRARS
from .parser_daily import REGISTRARS as DAILY_REGISTRARS
from .parser_types import ParserRegistrar
from .parser_watchlist import REGISTRARS as WATCHLIST_REGISTRARS
from .parser_web import REGISTRARS as WEB_REGISTRARS

COMMAND_ORDER = ("daily", "market-backfill", "watchlist", "web")


def _registrars() -> dict[str, ParserRegistrar]:
    registrars = {
        **DAILY_REGISTRARS,
        **BACKFILL_REGISTRARS,
        **WATCHLIST_REGISTRARS,
        **WEB_REGISTRARS,
    }
    if set(registrars) != set(COMMAND_ORDER):
        raise RuntimeError("command parser registry does not match the product CLI")
    return registrars


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="masterstock")
    subparsers = parser.add_subparsers(dest="command", required=True)
    registrars = _registrars()
    for command in COMMAND_ORDER:
        registrars[command](subparsers)
    return parser
