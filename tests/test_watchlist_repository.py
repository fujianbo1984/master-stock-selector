from __future__ import annotations

import sqlite3

import pytest

from master_stock_selector.watchlist.methods import (
    MINERVINI_POLICY_VERSION,
    WEINSTEIN_POLICY_VERSION,
)
from master_stock_selector.watchlist.repository import MarketDataReader, WatchlistRepository


def test_repository_derives_enter_continue_exit_and_reentry(tmp_path):
    path = tmp_path / "watchlist.sqlite3"
    repository = WatchlistRepository(path)
    results = ["FAIL", "PASS", "PASS", "FAIL", "PASS"]
    facts = [
        {
            "as_of_date": f"2026-07-{day:02d}",
            "symbol": "000001.SZ",
            "method": "minervini",
            "result": result,
            "policy_version": MINERVINI_POLICY_VERSION,
            "evidence": {},
            "source_digest": "digest",
            "origin": "RECONSTRUCTED",
        }
        for day, result in zip(range(1, 6), results)
    ]
    repository.persist_run(
        stock_facts=facts,
        index_facts=[],
        receipt={
            "run_id": "run-1",
            "as_of_date": "2026-07-05",
            "from_date": "2026-07-01",
            "origin": "RECONSTRUCTED",
            "minervini_policy_version": MINERVINI_POLICY_VERSION,
            "weinstein_policy_version": "weinstein-stage-30w-v1",
            "market_database": "/tmp/market.sqlite3",
            "source_digest": "source",
            "counts": {},
            "status": "SUCCESS",
            "started_at": "2026-07-05T10:00:00+08:00",
            "finished_at": "",
        },
    )

    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            """
            SELECT as_of_date, state, first_qualified_on, streak_started_on,
                   consecutive_sessions
            FROM stock_method_transition ORDER BY as_of_date
            """
        ).fetchall()

    assert rows == [
        ("2026-07-02", "ENTERED", "2026-07-02", "2026-07-02", 1),
        ("2026-07-03", "CONTINUING", "2026-07-02", "2026-07-02", 2),
        ("2026-07-04", "EXITED", "2026-07-02", None, 0),
        ("2026-07-05", "REENTERED", "2026-07-02", "2026-07-05", 1),
    ]


def test_repository_returns_retained_method_events_for_chart(tmp_path):
    repository = WatchlistRepository(tmp_path / "watchlist.sqlite3")
    facts = []
    minervini_results = ["FAIL", "PASS", "UNKNOWN", "PASS"]
    weinstein_results = ["FAIL", "PASS", "TRANSITION", "PASS"]
    weinstein_stages = ["STAGE_1", "STAGE_2", "STAGE_3", "STAGE_2"]
    for index, day in enumerate(range(1, 5)):
        as_of_date = f"2026-07-{day:02d}"
        facts.extend(
            [
                {
                    "as_of_date": as_of_date,
                    "symbol": "000001.SZ",
                    "method": "minervini",
                    "result": minervini_results[index],
                    "policy_version": MINERVINI_POLICY_VERSION,
                    "evidence": {
                        "profile": {
                            "failed_checks": (
                                ["close_above_sma50"]
                                if minervini_results[index] == "FAIL"
                                else []
                            )
                        }
                    },
                    "source_digest": "digest",
                    "origin": "RECONSTRUCTED",
                },
                {
                    "as_of_date": as_of_date,
                    "symbol": "000001.SZ",
                    "method": "weinstein",
                    "result": weinstein_results[index],
                    "policy_version": WEINSTEIN_POLICY_VERSION,
                    "evidence": {
                        "profile": {
                            "stage": weinstein_stages[index],
                            "stage_started_on": as_of_date,
                            "duration_weeks": 1,
                            "effective_week_end": as_of_date,
                        }
                    },
                    "source_digest": "digest",
                    "origin": "RECONSTRUCTED",
                },
            ]
        )
    repository.persist_run(
        stock_facts=facts,
        index_facts=[],
        receipt={
            "run_id": "chart-events",
            "as_of_date": "2026-07-04",
            "from_date": "2026-07-01",
            "origin": "RECONSTRUCTED",
            "minervini_policy_version": MINERVINI_POLICY_VERSION,
            "weinstein_policy_version": WEINSTEIN_POLICY_VERSION,
            "market_database": "/tmp/market.sqlite3",
            "source_digest": "source",
            "counts": {},
            "status": "SUCCESS",
            "started_at": "2026-07-04T10:00:00+08:00",
            "finished_at": "",
        },
    )

    result = repository.stock_method_chart_events(
        "000001.sz", "2026-07-02", "2026-07-04"
    )

    assert result["coverage"]["weinstein"] == {
        "from_date": "2026-07-01",
        "to_date": "2026-07-04",
        "policy_version": WEINSTEIN_POLICY_VERSION,
    }
    assert [(item["method"], item["state"]) for item in result["events"]] == [
        ("minervini", "ENTERED"),
        ("weinstein", "ENTERED"),
        ("minervini", "DATA_GAP"),
        ("weinstein", "EXITED"),
        ("minervini", "REENTERED"),
        ("weinstein", "REENTERED"),
    ]
    assert result["events"][3]["summary"].endswith("Stage 3")
    assert result["events"][4]["label"] == "M再"


def test_repository_keeps_system_facts_immutable_but_allows_manual_review(tmp_path):
    path = tmp_path / "watchlist.sqlite3"
    repository = WatchlistRepository(path)
    repository.initialize()
    repository.save_review("000001.SZ", "FOCUS", "等待进一步人工分析")
    repository.save_review("000001.SZ", "WATCH", "继续观察")

    detail = repository.stock_detail("000001.SZ")

    assert detail["manual"]["manual_state"] == "WATCH"
    assert detail["manual"]["note"] == "继续观察"


def test_trade_review_matches_fifo_costs_and_rejects_oversell(tmp_path):
    repository = WatchlistRepository(tmp_path / "watchlist.sqlite3")
    repository.record_trade(
        traded_on="2026-07-01", symbol="000001.SZ", side="BUY", quantity=100,
        price=10.0, fee=5.0, method="WEINSTEIN", stop_price=8.0,
        rationale="突破", invalidation="跌破止损",
    )
    repository.record_trade(
        traded_on="2026-07-03", symbol="000001.SZ", side="SELL", quantity=40,
        price=12.0, fee=4.0, method="WEINSTEIN", exit_reason="计划止盈",
    )

    review = repository.trade_review()

    assert review["summary"]["count"] == 1
    assert review["closed"][0]["pnl"] == 74.0
    assert review["closed"][0]["holding_days"] == 2
    assert review["open_positions"][0]["symbol"] == "000001.SZ"
    assert review["open_positions"][0]["quantity"] == 60
    assert review["open_positions"][0]["cost"] == 603.0
    assert review["open_positions"][0]["setup_label"] == "回调"
    assert review["closed"][0]["stop_price"] == 8.0
    assert review["closed"][0]["entry_price"] == 10.0
    assert review["closed"][0]["exit_price"] == 12.0
    with pytest.raises(ValueError, match="超过可复盘持仓"):
        repository.record_trade(
            traded_on="2026-07-04", symbol="000001.SZ", side="SELL", quantity=61,
            price=11.0, fee=0.0, method="WEINSTEIN",
        )
    with pytest.raises(ValueError, match="低于买入成交价"):
        repository.record_trade(
            traded_on="2026-07-04", symbol="000002.SZ", side="BUY", quantity=1,
            price=10.0, fee=0.0, method="WEINSTEIN", stop_price=10.0,
        )


def test_trade_review_retains_authorized_unmatched_historical_sell(tmp_path):
    repository = WatchlistRepository(tmp_path / "watchlist.sqlite3")

    repository.record_trade(
        traded_on="2026-08-05", symbol="603259.SH", side="SELL", quantity=100,
        price=147.26, fee=0.0, method="MANUAL", permit_unmatched_sell=True,
    )

    review = repository.trade_review()

    assert review["summary"]["count"] == 0
    unmatched = review["unmatched_sells"]
    assert len(unmatched) == 1
    assert unmatched[0]["symbol"] == "603259.SH"
    assert unmatched[0]["quantity"] == 100
    assert unmatched[0]["setup_label"] == "回调"


def test_trade_update_preserves_fifo_validation_and_refreshes_fields(tmp_path):
    repository = WatchlistRepository(tmp_path / "watchlist.sqlite3")
    execution_id = repository.record_trade(
        traded_on="2026-07-01", traded_at="09:30:00", symbol="000001.SZ", side="BUY",
        quantity=100, price=10.0, fee=0.0, method="MANUAL",
    )
    repository.record_trade(
        traded_on="2026-07-02", symbol="000001.SZ", side="SELL", quantity=100,
        price=12.0, fee=0.0, method="MANUAL",
    )

    repository.update_trade(
        execution_id, traded_on="2026-07-01", traded_at="09:31:00", side="BUY",
        quantity=100, price=11.0, fee=0.0, method="MANUAL", rationale="修正后的依据",
    )

    assert repository.trade_execution(execution_id)["traded_at"] == "09:31:00"
    assert repository.trade_review()["closed"][0]["pnl"] == 100.0


def test_market_reader_builds_disclosed_equal_weight_industry_proxy(tmp_path):
    path = tmp_path / "market.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE daily_bars (
                market TEXT NOT NULL, symbol TEXT NOT NULL, trade_date TEXT NOT NULL,
                open REAL, high REAL, low REAL, close REAL,
                adj_type TEXT NOT NULL,
                PRIMARY KEY (market, symbol, trade_date, adj_type)
            )
            """
        )
        connection.executemany(
            "INSERT INTO daily_bars VALUES ('ashare', ?, ?, ?, ?, ?, ?, 'qfq')",
            (
                ("000001.SZ", "2026-07-30", 10.0, 10.0, 10.0, 10.0),
                ("000001.SZ", "2026-07-31", 10.0, 12.0, 9.0, 11.0),
                ("000002.SZ", "2026-07-30", 20.0, 20.0, 20.0, 20.0),
                ("000002.SZ", "2026-07-31", 21.0, 22.0, 20.0, 21.0),
            ),
        )
        connection.executemany(
            "INSERT INTO daily_bars VALUES ('ashare', ?, ?, ?, ?, ?, ?, 'raw')",
            (
                ("000001.SZ", "2026-07-30", 10.0, 10.0, 10.0, 10.0),
                ("000001.SZ", "2026-07-31", 10.0, 12.0, 9.0, 11.0),
            ),
        )

    bars = MarketDataReader(path).industry_proxy_bars(
        ["000001.SZ", "000002.SZ"],
        "2026-07-31",
        limit=20,
    )

    assert bars == [
        {
            "trade_date": "2026-07-31",
            "open": 1025.0,
            "high": 1150.0,
            "low": 950.0,
            "close": 1075.0,
            "member_count": 2,
        }
    ]
    assert MarketDataReader(path).trade_drawdown_low("000001.SZ", "2026-07-30", "2026-07-31") == 9.0


def test_industry_proxy_calendar_ignores_unrelated_newer_symbols(tmp_path):
    path = tmp_path / "market.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE daily_bars (
                market TEXT NOT NULL, symbol TEXT NOT NULL, trade_date TEXT NOT NULL,
                open REAL, high REAL, low REAL, close REAL,
                adj_type TEXT NOT NULL,
                PRIMARY KEY (market, symbol, trade_date, adj_type)
            )
            """
        )
        connection.executemany(
            "INSERT INTO daily_bars VALUES ('ashare', ?, ?, ?, ?, ?, ?, 'qfq')",
            (
                ("000001.SZ", "2026-07-29", 10.0, 10.0, 10.0, 10.0),
                ("000001.SZ", "2026-07-30", 10.0, 11.0, 9.0, 10.5),
                ("000001.SZ", "2026-07-31", 10.5, 12.0, 10.0, 11.0),
                ("999999.SZ", "2026-08-01", 20.0, 20.0, 20.0, 20.0),
                ("999999.SZ", "2026-08-04", 20.0, 20.0, 20.0, 20.0),
                ("999999.SZ", "2026-08-05", 20.0, 20.0, 20.0, 20.0),
            ),
        )

    bars = MarketDataReader(path).industry_proxy_bars(
        ["000001.SZ"],
        "2026-08-05",
        limit=2,
    )

    assert [bar["trade_date"] for bar in bars] == ["2026-07-30", "2026-07-31"]
