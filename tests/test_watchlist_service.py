from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

import pytest

from master_stock_selector.watchlist.repository import WatchlistRepository
from master_stock_selector.watchlist.service import WatchlistRunConfig, run_watchlist

MARKET_TEST_SCHEMA_SQL = """
CREATE TABLE daily_bars (
    market TEXT, symbol TEXT, trade_date TEXT, open REAL, high REAL, low REAL,
    close REAL, volume REAL, amount REAL, adj_type TEXT, data_source TEXT,
    source_version TEXT, batch_id TEXT, run_id TEXT, data_as_of_date TEXT,
    input_hash TEXT, fetched_at TEXT, price_scale_id TEXT
);
CREATE TABLE market_index_daily_bars (
    batch_id TEXT, run_id TEXT, market TEXT, index_symbol TEXT, trade_date TEXT,
    open REAL, high REAL, low REAL, close REAL, volume REAL, amount REAL,
    provider TEXT, source_version TEXT, fetched_at TEXT
);
CREATE TABLE security_master_snapshots (
    snapshot_id TEXT, run_id TEXT, market TEXT, as_of_date TEXT, provider TEXT,
    source_version TEXT, symbol_count INTEGER, symbols_hash TEXT,
    symbols_json TEXT, members_json TEXT, fetched_at TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


def _weekdays(count: int, start: date = date(2025, 1, 2)) -> list[str]:
    values: list[str] = []
    current = start
    while len(values) < count:
        if current.weekday() < 5:
            values.append(current.isoformat())
        current += timedelta(days=1)
    return values


def _seed_market(path, *, missing_index: str = "") -> list[str]:
    dates = _weekdays(260)
    members = [
        {
            "ts_code": "000001.SZ",
            "name": "上升样本",
            "industry": "测试行业",
            "list_date": "2000-01-01",
            "listing_status_as_of": "listed",
            "is_st": False,
            "is_suspended": False,
            "trading_status": "trading",
        },
        {
            "ts_code": "600001.SH",
            "name": "下降样本",
            "industry": "测试行业",
            "list_date": "2000-01-01",
            "listing_status_as_of": "listed",
            "is_st": False,
            "is_suspended": False,
            "trading_status": "trading",
        },
    ]
    with sqlite3.connect(path) as connection:
        connection.executescript(MARKET_TEST_SCHEMA_SQL)
        stock_rows = []
        for index, trade_date in enumerate(dates):
            for symbol, close in (
                ("000001.SZ", 100 + index),
                ("600001.SH", 400 - index),
            ):
                stock_rows.append(
                    (
                        "ashare", symbol, trade_date, close, close, close, close, 1000.0,
                        100000.0, "qfq", "test", "v1", "batch", "run", dates[-1],
                        "input", "2026-01-01T00:00:00+08:00", f"scale-{symbol}",
                    )
                )
        connection.executemany(
            """
            INSERT INTO daily_bars(
                market,symbol,trade_date,open,high,low,close,volume,amount,adj_type,
                data_source,source_version,batch_id,run_id,data_as_of_date,input_hash,
                fetched_at,price_scale_id
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            stock_rows,
        )
        index_rows = []
        for index, trade_date in enumerate(dates):
            for symbol in ("000300.SH", "000852.SH", "399006.SZ", "000688.SH"):
                if symbol == missing_index:
                    continue
                close = 1000 + index * 3
                index_rows.append(
                    (
                        "batch", "run", "ashare", symbol, trade_date, close, close,
                        close, close, 1000.0, 100000.0, "test", "v1",
                        "2026-01-01T00:00:00+08:00",
                    )
                )
        connection.executemany(
            """
            INSERT INTO market_index_daily_bars(
                batch_id,run_id,market,index_symbol,trade_date,open,high,low,close,
                volume,amount,provider,source_version,fetched_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            index_rows,
        )
        connection.execute(
            """
            INSERT INTO security_master_snapshots(
                snapshot_id,run_id,market,as_of_date,provider,source_version,
                symbol_count,symbols_hash,symbols_json,members_json,fetched_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "snapshot", "run", "ashare", dates[-1], "test", "v1", 2, "hash",
                json.dumps(["000001.SZ", "600001.SH"]), json.dumps(members, ensure_ascii=False),
                "2026-01-01T00:00:00+08:00",
            ),
        )
    return dates


def test_service_builds_two_independent_stock_methods_and_four_index_stages(tmp_path):
    market_path = tmp_path / "market.sqlite3"
    runtime_path = tmp_path / "master_watchlist.sqlite3"
    dates = _seed_market(market_path)

    payload = run_watchlist(
        WatchlistRunConfig(
            market_database=market_path,
            watchlist_database=runtime_path,
            as_of_date=dates[-1],
            from_date=dates[-2],
            origin="RECONSTRUCTED",
        )
    )
    repository = WatchlistRepository(runtime_path)
    rows = repository.watchlist_rows(dates[-1])
    provisional = repository.provisional_rows(dates[-1])
    indices = repository.index_facts(dates[-1])

    assert payload["status"] == "SUCCESS"
    assert payload["counts"]["stock_facts"] == 8
    assert payload["counts"]["weinstein_provisional_facts"] == 4
    assert len(provisional) == 2
    assert {row["projected_stage"] for row in provisional} == {
        "STAGE_2", "STAGE_4"
    }
    assert all(row["sessions_elapsed"] >= 1 for row in provisional)
    assert {row["index_symbol"] for row in indices} == {
        "000300.SH", "000852.SH", "399006.SZ", "000688.SH"
    }
    assert payload["counts"]["index_minervini_facts"] == 4
    assert payload["counts"]["index_minervini_stage2"] == 4
    assert {row["minervini"]["result"] for row in indices} == {"PASS"}
    rising = next(row for row in rows if row["symbol"] == "000001.SZ")
    assert rising["methods"]["minervini"]["result"] == "PASS"
    assert rising["methods"]["weinstein"]["result"] == "PASS"
    assert rising["name"] == "上升样本"
    assert all("oneil" not in row["methods"] for row in rows)

    with pytest.raises(ValueError, match="append-only"):
        run_watchlist(
            WatchlistRunConfig(
                market_database=market_path,
                watchlist_database=runtime_path,
                as_of_date=dates[-1],
                from_date=dates[-2],
                origin="RECONSTRUCTED",
            )
        )


def test_service_persists_explicit_unknown_for_missing_index(tmp_path):
    market_path = tmp_path / "market.sqlite3"
    runtime_path = tmp_path / "master_watchlist.sqlite3"
    dates = _seed_market(market_path, missing_index="000688.SH")

    run_watchlist(
        WatchlistRunConfig(
            market_database=market_path,
            watchlist_database=runtime_path,
            as_of_date=dates[-1],
            from_date=dates[-1],
            origin="RECONSTRUCTED",
        )
    )

    rows = WatchlistRepository(runtime_path).index_facts(dates[-1])
    assert len(rows) == 4
    missing = next(row for row in rows if row["index_symbol"] == "000688.SH")
    assert missing["stage"] == "UNKNOWN"
    assert missing["evidence"]["reason"] == "NO_COMPLETED_LOCAL_INDEX_WEEK"
    assert missing["minervini"]["result"] == "UNKNOWN"
    assert (
        missing["minervini"]["evidence"]["reason"]
        == "MISSING_BAR_FOR_AS_OF_DATE"
    )
