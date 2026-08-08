from __future__ import annotations

import argparse
from collections.abc import Callable
from importlib import import_module

from .policy import COMMAND_POLICIES, enforce_command_policy

Handler = Callable[[argparse.Namespace, argparse.ArgumentParser], int]

# The mapping is deliberately explicit and lazy: unrelated command domains are
# not imported until their command is dispatched.
COMMAND_HANDLERS: dict[str, str] = {
    "database-optimize": "reference",
    "database-validate": "reference",
    "daily": "daily",
    "market-backfill": "backfill",
    "reference-backfill": "reference",
    "reference-materialize": "reference",
    "watchlist": "watchlist",
    "web": "web",
}


def load_handler(command: str) -> Handler:
    module_name = COMMAND_HANDLERS[command]
    module = import_module(f"{__package__}.{module_name}")
    handler = getattr(module, "handle")
    if not callable(handler):
        raise TypeError(f"command handler is not callable: {command}")
    return handler


def dispatch(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.command not in COMMAND_HANDLERS:
        parser.error("unknown command")
        return 2
    if COMMAND_HANDLERS.keys() != COMMAND_POLICIES.keys():
        raise RuntimeError("command policy registry does not match dispatch")
    enforce_command_policy(args, parser)
    handler = load_handler(args.command)
    return handler(args, parser)
