from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import Any, Protocol


class SubparserRegistry(Protocol):
    def add_parser(self, name: str, **kwargs: Any) -> argparse.ArgumentParser: ...


ParserRegistrar = Callable[[SubparserRegistry], None]
