from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path
from typing import NoReturn

from .commands import parser as command_parser
from .commands.dispatch import dispatch

RUNTIME_ERROR_EXIT = 1
USAGE_ERROR_EXIT = 2


class ProductArgumentParser(argparse.ArgumentParser):
    """Keep parameter failures machine-readable without changing help output."""

    def error(self, message: str) -> NoReturn:
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "exit_code": USAGE_ERROR_EXIT,
                    "error": {"type": "USAGE_ERROR", "message": message},
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        raise SystemExit(USAGE_ERROR_EXIT)


def build_parser() -> argparse.ArgumentParser:
    """Build the compact master-watchlist CLI."""
    return command_parser.build_parser(parser_class=ProductArgumentParser)


def load_local_env(path: str | Path = ".env") -> set[str]:
    """Load local overrides before parser defaults are evaluated."""

    env_path = Path(path)
    if not env_path.exists():
        return set()
    inserted: set[str] = set()
    for line in env_path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            inserted.add(key)
    return inserted


def main(argv: list[str] | None = None) -> int:
    inserted = load_local_env()
    try:
        parser: argparse.ArgumentParser = build_parser()
        try:
            args = parser.parse_args(argv)
        except SystemExit as exc:
            if exc.code == 0:
                return 0
            raise
        try:
            return dispatch(args, parser)
        except KeyboardInterrupt:
            return 130
        except Exception as exc:
            if bool(getattr(args, "debug", False)):
                traceback.print_exc()
            else:
                print(
                    json.dumps(
                        {
                            "status": "ERROR",
                            "exit_code": RUNTIME_ERROR_EXIT,
                            "error": {
                                "type": type(exc).__name__,
                                "message": str(exc),
                            },
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    file=sys.stderr,
                )
            return RUNTIME_ERROR_EXIT
    finally:
        for key in inserted:
            os.environ.pop(key, None)
