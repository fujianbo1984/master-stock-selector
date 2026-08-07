from __future__ import annotations

import sqlite3

from master_stock_selector.watchlist.methods import MINERVINI_POLICY_VERSION
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


def test_repository_keeps_system_facts_immutable_but_allows_manual_review(tmp_path):
    path = tmp_path / "watchlist.sqlite3"
    repository = WatchlistRepository(path)
    repository.initialize()
    repository.save_review("000001.SZ", "FOCUS", "等待进一步人工分析")
    repository.save_review("000001.SZ", "WATCH", "继续观察")

    detail = repository.stock_detail("000001.SZ")

    assert detail["manual"]["manual_state"] == "WATCH"
    assert detail["manual"]["note"] == "继续观察"


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
