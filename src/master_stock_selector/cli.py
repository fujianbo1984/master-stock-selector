from __future__ import annotations

import argparse
import os
from pathlib import Path

from .commands import parser as command_parser
from .commands.dispatch import dispatch


def build_parser() -> argparse.ArgumentParser:
    """Build the compact master-watchlist CLI."""
    return command_parser.build_parser()


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
        return dispatch(args, parser)
    finally:
        for key in inserted:
            os.environ.pop(key, None)
