from __future__ import annotations

import json
import sqlite3

from master_stock_selector.watchlist.database_optimization import (
    optimize_databases,
    validate_database_equivalence,
)
from master_stock_selector.watchlist.repository import MarketDataReader, WatchlistRepository


def test_builds_compact_temporal_candidates_without_mutating_sources(tmp_path) -> None:
    market_source = tmp_path / "market-source.sqlite3"
    market_target = tmp_path / "market-target.sqlite3"
    watchlist_source = tmp_path / "watchlist-source.sqlite3"
    watchlist_target = tmp_path / "watchlist-target.sqlite3"
    _seed_legacy_market(market_source)
    _seed_legacy_watchlist(watchlist_source)

    result = optimize_databases(
        market_source=market_source,
        market_target=market_target,
        watchlist_source=watchlist_source,
        watchlist_target=watchlist_target,
    )

    assert result["market"]["security_history_count"] == 2
    assert result["watchlist"]["identity_history_count"] == 2
    assert result["watchlist"]["industry_membership_history_count"] == 1
    with sqlite3.connect(market_source) as connection:
        assert connection.execute("SELECT COUNT(*) FROM security_master_snapshots").fetchone()[0] == 2
    with sqlite3.connect(watchlist_source) as connection:
        assert connection.execute("SELECT COUNT(*) FROM security_identity_snapshot").fetchone()[0] == 2
    with sqlite3.connect(market_target) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "security_master_history" in tables
        assert "security_master_snapshots" not in tables
    with sqlite3.connect(watchlist_target) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "security_identity_history" in tables
        assert "security_identity_snapshot" not in tables

    assert MarketDataReader(market_target).security_members("2026-08-01")["000001.SZ"]["name"] == "旧名"
    assert MarketDataReader(market_target).security_members("2026-08-02")["000001.SZ"]["name"] == "新名"
    assert WatchlistRepository(watchlist_target).stock_names({"000001.SZ"}) == {"000001.SZ": "新名"}
    validation = validate_database_equivalence(
        market_source=market_source,
        market_target=market_target,
        watchlist_source=watchlist_source,
        watchlist_target=watchlist_target,
    )
    assert validation["status"] == "EQUIVALENT"
    assert validation["market_snapshot_dates"] == 2


def _seed_legacy_market(path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE daily_bars (
                market TEXT, adj_type TEXT, symbol TEXT, trade_date TEXT, amount REAL
            );
            CREATE TABLE security_master_snapshots (
                snapshot_id TEXT PRIMARY KEY, run_id TEXT, market TEXT, as_of_date TEXT,
                provider TEXT, source_version TEXT, symbol_count INTEGER, symbols_hash TEXT,
                symbols_json TEXT, members_json TEXT, fetched_at TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        for as_of_date, name in (("2026-08-01", "旧名"), ("2026-08-02", "新名")):
            member = {
                "ts_code": "000001.SZ", "symbol": "000001", "name": name,
                "industry": "银行", "market": "主板", "list_date": "1991-04-03",
                "provider_list_status": "L", "listing_status_as_of": "listed",
                "is_st": False, "st_status": "normal", "is_suspended": False,
                "trading_status": "trading",
            }
            connection.execute(
                "INSERT INTO security_master_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                (
                    f"snapshot-{as_of_date}", f"run-{as_of_date}", "ashare", as_of_date,
                    "provider", "v1", 1, "hash", '["000001.SZ"]',
                    json.dumps([member], ensure_ascii=False), as_of_date,
                ),
            )


def _seed_legacy_watchlist(path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE security_identity_snapshot (
                snapshot_date TEXT, symbol TEXT, name TEXT, industry TEXT, list_date TEXT,
                is_st INTEGER, is_suspended INTEGER, listing_status TEXT,
                trading_status TEXT, origin TEXT
            );
            CREATE TABLE industry_dimension_snapshot (
                snapshot_date TEXT, taxonomy TEXT, industry_level TEXT, industry_code TEXT,
                industry_name TEXT, parent_industry_code TEXT, source TEXT,
                source_version TEXT, source_digest TEXT, origin TEXT
            );
            CREATE TABLE security_industry_membership_snapshot (
                snapshot_date TEXT, symbol TEXT, taxonomy TEXT, industry_level TEXT,
                industry_code TEXT, industry_name TEXT, valid_from TEXT, valid_to TEXT,
                assignment_state TEXT, source TEXT, source_digest TEXT, origin TEXT
            );
            """
        )
        connection.executemany(
            "INSERT INTO security_identity_snapshot VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                ("2026-08-01", "000001.SZ", "旧名", "银行", "1991-04-03", 0, 0, "L", "trading", "RECONSTRUCTED"),
                ("2026-08-02", "000001.SZ", "新名", "银行", "1991-04-03", 0, 0, "L", "trading", "RECONSTRUCTED"),
            ],
        )
        connection.execute(
            "INSERT INTO industry_dimension_snapshot VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("2026-08-01", "SW2021", "L3", "850001.SI", "银行", "", "source", "v1", "dimension", "RECONSTRUCTED"),
        )
        connection.execute(
            "INSERT INTO security_industry_membership_snapshot VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("2026-08-01", "000001.SZ", "SW2021", "L3", "850001.SI", "银行", "2026-01-01", "", "VERIFIED", "source", "membership", "RECONSTRUCTED"),
        )
