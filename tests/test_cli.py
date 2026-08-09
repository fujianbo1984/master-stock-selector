from __future__ import annotations

import pytest

from master_stock_selector.cli import main
from master_stock_selector.commands.dispatch import COMMAND_HANDLERS


def test_cli_exposes_only_current_product_commands(capsys) -> None:
    assert main(["--help"]) == 0
    output = capsys.readouterr().out

    assert set(COMMAND_HANDLERS) == {
        "agent", "daily", "market-backfill", "reference-backfill", "reference-materialize",
        "database-optimize", "database-validate", "watchlist", "web",
    }
    assert "daily" in output
    assert "agent" in output
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


def test_agent_cli_reads_token_from_environment_and_defaults_import_to_validate(
    tmp_path, monkeypatch, capsys
) -> None:
    payload = tmp_path / "trades.json"
    payload.write_text(
        '[{"symbol":"000001.SZ","traded_on":"2026-08-01",'
        '"side":"BUY","quantity":100,"price":10}]',
        encoding="utf-8",
    )
    calls = []

    def fake_request(base_url, token, method, path, body=None, *, headers=None):
        calls.append((base_url, token, method, path, body, headers))
        return {"status": "VALID"}

    monkeypatch.setenv("MASTERSTOCK_AGENT_TOKEN", "mst_test-token")
    monkeypatch.setattr("master_stock_selector.commands.agent._request_json", fake_request)

    assert main(["agent", "trades", "import", str(payload)]) == 0
    assert calls == [
        (
            "http://127.0.0.1:8888",
            "mst_test-token",
            "POST",
            "/api/v1/trades/validate",
            {"trades": [{
                "symbol": "000001.SZ", "traded_on": "2026-08-01",
                "side": "BUY", "quantity": 100, "price": 10,
            }]},
            None,
        )
    ]
    assert '"status": "VALID"' in capsys.readouterr().out


def test_agent_cli_requires_explicit_commit_for_stop(monkeypatch) -> None:
    monkeypatch.setenv("MASTERSTOCK_AGENT_TOKEN", "mst_test-token")

    with pytest.raises(SystemExit) as exc_info:
        main(["agent", "trades", "set-stop", "execution-1", "9.5"])

    assert exc_info.value.code == 2


def test_agent_cli_sends_integer_revision_when_updating_stop(monkeypatch) -> None:
    calls = []

    def fake_request(base_url, token, method, path, body=None, *, headers=None):
        calls.append((method, path, body))
        return {"status": "UPDATED", "revision": 4}

    monkeypatch.setenv("MASTERSTOCK_AGENT_TOKEN", "mst_test-token")
    monkeypatch.setattr("master_stock_selector.commands.agent._request_json", fake_request)

    assert main(
        [
            "agent",
            "trades",
            "set-stop",
            "execution-1",
            "9.5",
            "--expected-revision",
            "3",
            "--commit",
        ]
    ) == 0
    assert calls == [
        (
            "PATCH",
            "/api/v1/trades/execution-1/stop",
            {"stop_price": 9.5, "expected_revision": 3},
        )
    ]


def test_agent_cli_refuses_bearer_token_over_public_http(monkeypatch) -> None:
    monkeypatch.setenv("MASTERSTOCK_AGENT_TOKEN", "mst_test-token")

    with pytest.raises(SystemExit) as exc_info:
        main(["agent", "--base-url", "http://example.com", "me"])

    assert exc_info.value.code == 2
