from __future__ import annotations

import pytest

from master_stock_selector.cli import main
from master_stock_selector.commands.dispatch import COMMAND_HANDLERS


def test_cli_exposes_only_current_product_commands(capsys) -> None:
    assert main(["--help"]) == 0
    output = capsys.readouterr().out

    assert set(COMMAND_HANDLERS) == {
        "daily", "market-backfill", "reference-backfill", "reference-materialize",
        "database-optimize", "database-validate", "watchlist", "web",
    }
    assert "daily" in output
    assert "watchlist" in output
    assert "market-backfill" in output
    assert "database-optimize" in output
    assert "web" in output
    assert "vcp" not in output.lower()
    assert "etf" not in output.lower()
    assert "northstar" not in output.lower()


def test_watchlist_write_requires_explicit_apply() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["watchlist"])

    assert exc_info.value.code == 2


def test_daily_write_requires_explicit_apply() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["daily"])

    assert exc_info.value.code == 2
