from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from master_stock_selector.cli import main
from master_stock_selector.commands.diagnostics import evaluate_freshness
from master_stock_selector.commands.dispatch import COMMAND_HANDLERS
from master_stock_selector.web.users import UserRepository


def test_cli_exposes_only_current_product_commands(capsys) -> None:
    assert main(["--help"]) == 0
    output = capsys.readouterr().out

    assert set(COMMAND_HANDLERS) == {
        "agent",
        "doctor",
        "runs",
        "daily",
        "market-backfill",
        "reference-backfill",
        "reference-materialize",
        "database-optimize",
        "database-validate",
        "watchlist",
        "web",
    }
    assert "daily" in output
    assert "agent" in output
    assert "doctor" in output
    assert "runs" in output
    assert "watchlist" in output
    assert "market-backfill" in output
    assert "database-optimize" in output
    assert "web" in output
    assert "vcp" not in output.lower()
    assert "etf" not in output.lower()
    assert "northstar" not in output.lower()


def _seed_diagnostic_databases(tmp_path, trade_date="2026-08-25"):
    market = tmp_path / "market.sqlite3"
    watchlist = tmp_path / "watchlist.sqlite3"
    users = tmp_path / "users.sqlite3"
    with sqlite3.connect(market) as connection:
        connection.executescript(
            """
            CREATE TABLE daily_bars (market TEXT, adj_type TEXT, trade_date TEXT);
            CREATE TABLE watchlist_market_collection_receipt (
                run_id TEXT, trade_date TEXT, status TEXT, quality_json TEXT,
                started_at TEXT
            );
            """
        )
        connection.execute(
            "INSERT INTO daily_bars VALUES ('ashare','qfq',?)", (trade_date,)
        )
        connection.execute(
            "INSERT INTO watchlist_market_collection_receipt VALUES (?,?,?,?,?)",
            (
                "collect-test",
                trade_date,
                "SUCCESS",
                json.dumps({"week_open_dates": [trade_date]}),
                f"{trade_date}T17:00:00+08:00",
            ),
        )
    with sqlite3.connect(watchlist) as connection:
        connection.executescript(
            """
            CREATE TABLE stock_method_daily_fact (as_of_date TEXT);
            CREATE TABLE watchlist_run_receipt (
                run_id TEXT, as_of_date TEXT, status TEXT, counts_json TEXT,
                started_at TEXT
            );
            """
        )
        connection.execute("INSERT INTO stock_method_daily_fact VALUES (?)", (trade_date,))
        connection.execute(
            "INSERT INTO watchlist_run_receipt VALUES (?,?,?,?,?)",
            (
                "watchlist-test",
                trade_date,
                "SUCCESS",
                json.dumps({"stock_facts": 10}),
                f"{trade_date}T17:10:00+08:00",
            ),
        )
    UserRepository(users).migrate_schema()
    return market, watchlist, users


def _diagnostic_database_args(market, watchlist):
    return [
        "--market-database",
        str(market),
        "--watchlist-database",
        str(watchlist),
    ]


def test_doctor_reports_ready_and_uses_exit_zero(tmp_path, capsys) -> None:
    market, watchlist, users = _seed_diagnostic_databases(tmp_path)

    exit_code = main(
        [
            "doctor",
            *_diagnostic_database_args(market, watchlist),
            "--user-database",
            str(users),
            "--expected-date",
            "2026-08-25",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["status"] == "READY"
    assert payload["data"]["freshness"]["status"] == "FRESH"
    assert all(payload["checks"].values())


def test_runs_latest_list_show_and_stale_exit(tmp_path, capsys) -> None:
    market, watchlist, _ = _seed_diagnostic_databases(tmp_path)
    databases = _diagnostic_database_args(market, watchlist)

    assert main(["runs", *databases, "list", "--limit", "1"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["collections"][0]["run_id"] == "collect-test"
    assert listed["watchlist_runs"][0]["run_id"] == "watchlist-test"

    assert main(["runs", *databases, "show", "watchlist-test"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["kind"] == "watchlist"
    assert shown["run"]["counts"] == {"stock_facts": 10}

    assert (
        main(
            [
                "runs",
                *databases,
                "latest",
                "--expected-date",
                "2026-08-26",
                "--fail-if-stale",
            ]
        )
        == 3
    )
    latest = json.loads(capsys.readouterr().out)
    assert latest["status"] == "DEGRADED"
    assert latest["freshness"]["status"] == "STALE"


def test_runtime_errors_are_json_without_traceback(capsys) -> None:
    exit_code = main(
        [
            "database-validate",
            "--market-source",
            "/tmp/missing-market-source.sqlite3",
            "--market-target",
            "/tmp/missing-market-target.sqlite3",
            "--watchlist-source",
            "/tmp/missing-watchlist-source.sqlite3",
            "--watchlist-target",
            "/tmp/missing-watchlist-target.sqlite3",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert "Traceback" not in captured.err
    assert payload["error"]["type"] == "FileNotFoundError"
    assert payload["exit_code"] == 1


def test_usage_errors_are_json_and_exit_two(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["runs", "list", "--limit", "not-an-integer"])

    payload = json.loads(capsys.readouterr().err)
    assert exc_info.value.code == 2
    assert payload["error"]["type"] == "USAGE_ERROR"
    assert payload["exit_code"] == 2


def test_freshness_uses_receipt_calendar_and_close_cutoff() -> None:
    receipt = {
        "quality": {
            "week_open_dates": [
                "2026-08-24",
                "2026-08-25",
                "2026-08-26",
                "2026-08-27",
                "2026-08-28",
            ]
        }
    }
    shanghai = ZoneInfo("Asia/Shanghai")

    before_cutoff = evaluate_freshness(
        market_date="2026-08-25",
        fact_date="2026-08-25",
        latest_collection=receipt,
        now=datetime(2026, 8, 26, 17, 0, tzinfo=shanghai),
    )
    after_cutoff = evaluate_freshness(
        market_date="2026-08-25",
        fact_date="2026-08-25",
        latest_collection=receipt,
        now=datetime(2026, 8, 26, 18, 0, tzinfo=shanghai),
    )

    assert before_cutoff["status"] == "FRESH"
    assert before_cutoff["expected_date"] == "2026-08-25"
    assert before_cutoff["source"] == "collection_trade_calendar"
    assert after_cutoff["status"] == "STALE"
    assert after_cutoff["expected_date"] == "2026-08-26"


def test_freshness_detects_market_and_watchlist_date_mismatch() -> None:
    result = evaluate_freshness(
        market_date="2026-08-26",
        fact_date="2026-08-25",
        latest_collection=None,
        expected_date="2026-08-26",
    )

    assert result["status"] == "OUT_OF_SYNC"
    assert result["reason"] == "MARKET_AND_WATCHLIST_DATES_DIFFER"


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
        '[{"symbol":"000001.SZ","traded_on":"2026-08-01","side":"BUY","quantity":100,"price":10}]',
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
            "https://longchangwudao.cn",
            "mst_test-token",
            "POST",
            "/api/v1/trades/validate",
            {
                "trades": [
                    {
                        "symbol": "000001.SZ",
                        "traded_on": "2026-08-01",
                        "side": "BUY",
                        "quantity": 100,
                        "price": 10,
                    }
                ]
            },
            None,
        )
    ]
    assert '"status": "VALID"' in capsys.readouterr().out


def test_agent_cli_help_discloses_the_production_domain(capsys) -> None:
    assert main(["agent", "--help"]) == 0

    output = capsys.readouterr().out
    assert "https://longchangwudao.cn" in output
    assert "MASTERSTOCK_AGENT_URL" in output


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

    assert (
        main(
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
        )
        == 0
    )
    assert calls == [
        (
            "PATCH",
            "/api/v1/trades/execution-1/stop",
            {"stop_price": 9.5, "expected_revision": 3},
        )
    ]


def test_agent_cli_requires_commit_and_sends_revision_when_completing_trade_time(
    monkeypatch,
) -> None:
    calls = []

    def fake_request(base_url, token, method, path, body=None, *, headers=None):
        calls.append((method, path, body))
        return {"status": "UPDATED", "revision": 2}

    monkeypatch.setenv("MASTERSTOCK_AGENT_TOKEN", "mst_test-token")
    monkeypatch.setattr("master_stock_selector.commands.agent._request_json", fake_request)

    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "agent",
                "trades",
                "set-time",
                "execution-1",
                "09:30:24",
                "--expected-revision",
                "1",
            ]
        )
    assert exc_info.value.code == 2
    assert calls == []

    assert (
        main(
            [
                "agent",
                "trades",
                "set-time",
                "execution-1",
                "09:30:24",
                "--expected-revision",
                "1",
                "--commit",
            ]
        )
        == 0
    )
    assert calls == [
        (
            "PATCH",
            "/api/v1/trades/execution-1/time",
            {"traded_at": "09:30:24", "expected_revision": 1},
        )
    ]


def test_agent_cli_refuses_bearer_token_over_public_http(monkeypatch) -> None:
    monkeypatch.setenv("MASTERSTOCK_AGENT_TOKEN", "mst_test-token")

    with pytest.raises(SystemExit) as exc_info:
        main(["agent", "--base-url", "http://example.com", "me"])

    assert exc_info.value.code == 2


def test_agent_plan_cli_routes_json_and_requires_commit(tmp_path, monkeypatch) -> None:
    answer_file = tmp_path / "answer.json"
    answer_file.write_text(
        '{"question_id":"scope.market","expected_revision":2,'
        '"decision":{"state":"CONFIRMED","value":"A股"}}',
        encoding="utf-8",
    )
    calls = []

    def fake_request(base_url, token, method, path, body=None, *, headers=None):
        calls.append((method, path, body))
        return {"status": "OK"}

    monkeypatch.setenv("MASTERSTOCK_AGENT_TOKEN", "mst_test-token")
    monkeypatch.setattr("master_stock_selector.commands.agent._request_json", fake_request)

    assert main(["agent", "plans", "grill", "answer", "session-1", "--file", str(answer_file)]) == 0
    assert calls[-1] == (
        "POST",
        "/api/v1/plans/grill/sessions/session-1/answers",
        {
            "question_id": "scope.market",
            "expected_revision": 2,
            "decision": {"state": "CONFIRMED", "value": "A股"},
        },
    )
    assert main(["agent", "plans", "current"]) == 0
    assert calls[-1] == ("GET", "/api/v1/plans/current", None)

    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "agent",
                "plans",
                "grill",
                "publish",
                "session-1",
                "--expected-revision",
                "3",
            ]
        )
    assert exc_info.value.code == 2

    assert (
        main(
            [
                "agent",
                "plans",
                "grill",
                "publish",
                "session-1",
                "--expected-revision",
                "3",
                "--commit",
            ]
        )
        == 0
    )
    assert calls[-1] == (
        "POST",
        "/api/v1/plans/grill/sessions/session-1/publish",
        {"expected_revision": 3},
    )
