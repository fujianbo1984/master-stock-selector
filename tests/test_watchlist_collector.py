from __future__ import annotations

import json
import sqlite3

import pytest

from master_stock_selector.watchlist.collector import CollectionConfig, collect_market_data
from master_stock_selector.watchlist.historical_backfill import (
    HistoricalBackfillConfig,
    backfill_market_history,
)

MARKET_SCHEMA = """
CREATE TABLE daily_bars (
    market TEXT NOT NULL, symbol TEXT NOT NULL, trade_date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL, amount REAL,
    adj_type TEXT NOT NULL DEFAULT 'qfq', data_source TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source_version TEXT NOT NULL DEFAULT '', batch_id TEXT NOT NULL DEFAULT '',
    run_id TEXT NOT NULL DEFAULT '', data_as_of_date TEXT NOT NULL DEFAULT '',
    input_hash TEXT NOT NULL DEFAULT '', fetched_at TEXT NOT NULL DEFAULT '',
    price_scale_id TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (market, symbol, trade_date, adj_type)
);
CREATE TABLE daily_metrics (
    market TEXT NOT NULL, symbol TEXT NOT NULL, trade_date TEXT NOT NULL,
    turnover_rate REAL, total_mv REAL, circ_mv REAL, data_source TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source_version TEXT NOT NULL DEFAULT '', batch_id TEXT NOT NULL DEFAULT '',
    run_id TEXT NOT NULL DEFAULT '', data_as_of_date TEXT NOT NULL DEFAULT '',
    input_hash TEXT NOT NULL DEFAULT '', fetched_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (market, symbol, trade_date)
);
CREATE TABLE daily_adjustment_factors (
    market TEXT NOT NULL, symbol TEXT NOT NULL, trade_date TEXT NOT NULL,
    adj_factor REAL NOT NULL, data_source TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    factor_as_of_date TEXT NOT NULL DEFAULT '', source_version TEXT NOT NULL DEFAULT '',
    batch_id TEXT NOT NULL DEFAULT '', run_id TEXT NOT NULL DEFAULT '',
    fetched_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (market, symbol, trade_date)
);
CREATE TABLE market_index_daily_bars (
    batch_id TEXT NOT NULL, run_id TEXT NOT NULL, market TEXT NOT NULL,
    index_symbol TEXT NOT NULL, trade_date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL, amount REAL,
    provider TEXT NOT NULL, source_version TEXT NOT NULL, fetched_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, index_symbol, trade_date)
);
CREATE TABLE security_master_snapshots (
    snapshot_id TEXT NOT NULL PRIMARY KEY, run_id TEXT NOT NULL, market TEXT NOT NULL,
    as_of_date TEXT NOT NULL, provider TEXT NOT NULL, source_version TEXT NOT NULL,
    symbol_count INTEGER NOT NULL, symbols_hash TEXT NOT NULL, symbols_json TEXT NOT NULL,
    members_json TEXT NOT NULL DEFAULT '[]', fetched_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


class FakeProvider:
    source_name = "FakeTushare"
    source_version = "fake-v1"

    def __init__(self, *, open_day: bool = True, missing_index: str = "") -> None:
        self.open_day = open_day
        self.missing_index = missing_index
        self.request_count = 0

    def assert_ready(self) -> None:
        return None

    def trade_calendar(self, start_date: str, end_date: str) -> list[str]:
        self.request_count += 1
        return [end_date] if self.open_day else []

    def stock_basic(self) -> list[dict[str, object]]:
        self.request_count += 1
        return [
            {
                "ts_code": "000001.SZ",
                "symbol": "000001",
                "name": "正常公司",
                "industry": "银行",
                "market": "主板",
                "list_date": "19910403",
            },
            {
                "ts_code": "600001.SH",
                "symbol": "600001",
                "name": "*ST样本",
                "industry": "制造",
                "market": "主板",
                "list_date": "19990101",
            },
        ]

    def market_daily_bars(self, trade_date: str) -> dict[str, dict[str, object]]:
        self.request_count += 1
        return {
            "000001.SZ": _bar(trade_date, 12.0),
            "600001.SH": _bar(trade_date, 22.0),
        }

    def market_adjustment_factors(self, trade_date: str) -> dict[str, float]:
        self.request_count += 1
        return {"000001.SZ": 4.0, "600001.SH": 3.0}

    def market_daily_metrics(self, trade_date: str) -> dict[str, dict[str, object]]:
        self.request_count += 1
        return {
            "000001.SZ": {
                "trade_date": trade_date,
                "turnover_rate": 1.2,
                "total_mv": 800000.0,
                "circ_mv": 700000.0,
            },
            "600001.SH": {
                "trade_date": trade_date,
                "turnover_rate": 2.2,
                "total_mv": 300000.0,
                "circ_mv": 250000.0,
            },
        }

    def index_daily_bars(
        self, trade_date: str, index_symbols: tuple[str, ...]
    ) -> dict[str, dict[str, object]]:
        self.request_count += 1
        return {
            symbol: _bar(trade_date, 1000.0 + index)
            for index, symbol in enumerate(index_symbols)
            if symbol != self.missing_index
        }


def _bar(trade_date: str, close: float) -> dict[str, object]:
    return {
        "trade_date": trade_date,
        "open": close - 1,
        "high": close + 1,
        "low": close - 2,
        "close": close,
        "pre_close": close - 0.5,
        "pct_chg": round(0.5 / (close - 0.5) * 100, 4),
        "vol": 1000.0,
        "amount": 2000.0,
    }


def _seed_market(path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(MARKET_SCHEMA)
        for symbol, close, factor in (
            ("000001.SZ", 10.0, 2.0),
            ("600001.SH", 20.0, 3.0),
        ):
            connection.execute(
                """
                INSERT INTO daily_bars (
                    market,symbol,trade_date,open,high,low,close,volume,amount,
                    adj_type,data_source,source_version,batch_id,run_id,
                    data_as_of_date,input_hash,fetched_at,price_scale_id
                ) VALUES ('ashare', ?, '2026-07-31', ?, ?, ?, ?, 1000, 2000,
                          'qfq', 'tushare_adj_factor_qfq', 'fake-v1', 'old', 'old',
                          '2026-07-31', 'old-hash', '2026-07-31T16:00:00+08:00', ?)
                """,
                (symbol, close, close, close, close, f"old-scale-{symbol}"),
            )
            connection.execute(
                """
                INSERT INTO daily_bars (
                    market,symbol,trade_date,open,high,low,close,volume,amount,
                    adj_type,data_source,source_version,batch_id,run_id,
                    data_as_of_date,input_hash,fetched_at,price_scale_id
                ) VALUES ('ashare', ?, '2026-07-31', ?, ?, ?, ?, 1000, 2000,
                          'raw', 'tushare_daily', 'fake-v1', 'old', 'old',
                          '2026-07-31', 'old-hash', '2026-07-31T16:00:00+08:00', '')
                """,
                (symbol, close * factor / factor, close, close, close),
            )
            connection.execute(
                """
                INSERT INTO daily_adjustment_factors (
                    market,symbol,trade_date,adj_factor,data_source,
                    factor_as_of_date,source_version,batch_id,run_id,fetched_at
                ) VALUES ('ashare', ?, '2026-07-31', ?, 'tushare_adj_factor',
                          '2026-07-31', 'fake-v1', 'old', 'old',
                          '2026-07-31T16:00:00+08:00')
                """,
                (symbol, factor),
            )
        connection.execute(
            """
            INSERT INTO daily_bars (
                market,symbol,trade_date,open,high,low,close,volume,amount,
                adj_type,data_source,source_version,batch_id,run_id,
                data_as_of_date,input_hash,fetched_at,price_scale_id
            ) VALUES ('ashare', '000001.SZ', '2026-07-30', 8, 8, 8, 8, 0, 0,
                      'qfq', 'tushare_pct_chg_chain', 'legacy', 'old', 'old',
                      '2026-07-31', 'old-hash', '2026-07-31T16:00:00+08:00', '')
            """
        )


def test_collector_atomically_adds_day_and_rescales_only_changed_factor(tmp_path) -> None:
    path = tmp_path / "market.sqlite3"
    _seed_market(path)
    provider = FakeProvider()

    result = collect_market_data(
        CollectionConfig(
            market_database=path,
            trade_date="2026-08-03",
            minimum_stock_count=2,
            minimum_coverage_ratio=1.0,
        ),
        provider,
    )

    assert result["status"] == "SUCCESS"
    assert result["collection_state"] == "COLLECTED"
    assert result["stock_count"] == 2
    assert result["index_count"] == 4
    assert result["rescaled_symbol_count"] == 1
    with sqlite3.connect(path) as connection:
        old_rows = connection.execute(
            """
            SELECT symbol, close FROM daily_bars
            WHERE trade_date='2026-07-31' AND adj_type='qfq' ORDER BY symbol
            """
        ).fetchall()
        fallback_close = connection.execute(
            """
            SELECT close FROM daily_bars
            WHERE symbol='000001.SZ' AND trade_date='2026-07-30' AND adj_type='qfq'
            """
        ).fetchone()[0]
        new_rows = connection.execute(
            """
            SELECT symbol, adj_type, close FROM daily_bars
            WHERE trade_date='2026-08-03' ORDER BY symbol, adj_type
            """
        ).fetchall()
        quote = connection.execute(
            """
            SELECT close, pre_close, pct_chg FROM daily_bars
            WHERE symbol='000001.SZ' AND trade_date='2026-08-03' AND adj_type='raw'
            """
        ).fetchone()
        members = json.loads(
            connection.execute(
                """
                SELECT members_json FROM security_master_snapshots
                WHERE as_of_date='2026-08-03'
                """
            ).fetchone()[0]
        )
        receipt_count = connection.execute(
            "SELECT COUNT(*) FROM watchlist_market_collection_receipt"
        ).fetchone()[0]
    assert old_rows == [("000001.SZ", 5.0), ("600001.SH", 20.0)]
    assert fallback_close == 4.0
    assert new_rows == [
        ("000001.SZ", "qfq", 12.0),
        ("000001.SZ", "raw", 12.0),
        ("600001.SH", "qfq", 22.0),
        ("600001.SH", "raw", 22.0),
    ]
    assert quote == (12.0, 11.5, pytest.approx(4.3478))
    assert next(row for row in members if row["ts_code"] == "600001.SH")["is_st"] is True
    assert receipt_count == 1

    requests_before_retry = provider.request_count
    reused = collect_market_data(
        CollectionConfig(
            market_database=path,
            trade_date="2026-08-03",
            minimum_stock_count=2,
        ),
        provider,
    )
    assert reused["collection_state"] == "REUSED"
    assert provider.request_count == requests_before_retry


def test_collector_rejects_incomplete_index_batch_without_market_rows(tmp_path) -> None:
    path = tmp_path / "market.sqlite3"
    _seed_market(path)

    with pytest.raises(ValueError, match="四指数日线缺失"):
        collect_market_data(
            CollectionConfig(
                market_database=path,
                trade_date="2026-08-03",
                minimum_stock_count=2,
                minimum_coverage_ratio=1.0,
            ),
            FakeProvider(missing_index="000688.SH"),
        )

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM daily_bars WHERE trade_date='2026-08-03'"
        ).fetchone()[0] == 0
        receipt_table = connection.execute(
            """
            SELECT COUNT(*) FROM sqlite_master
            WHERE type='table' AND name='watchlist_market_collection_receipt'
            """
        ).fetchone()[0]
    assert receipt_table == 0


def test_collector_skips_exchange_closed_date_without_writes(tmp_path) -> None:
    path = tmp_path / "market.sqlite3"
    _seed_market(path)
    provider = FakeProvider(open_day=False)

    result = collect_market_data(
        CollectionConfig(
            market_database=path,
            trade_date="2026-08-02",
            minimum_stock_count=2,
        ),
        provider,
    )

    assert result["status"] == "SKIPPED"
    assert result["collection_state"] == "NON_TRADING_DAY"
    assert provider.request_count == 1


class BackfillFakeProvider(FakeProvider):
    def trade_calendar(self, start_date: str, end_date: str) -> list[str]:
        self.request_count += 1
        return ["2026-07-31", "2026-08-03"]


def test_historical_backfill_plans_then_fills_only_missing_market_rows(tmp_path) -> None:
    path = tmp_path / "market.sqlite3"
    _seed_market(path)
    with sqlite3.connect(path) as connection:
        for symbol, close in (("000001.SZ", 12.0), ("600001.SH", 22.0)):
            connection.execute(
                """
                INSERT INTO daily_bars (
                    market,symbol,trade_date,open,high,low,close,volume,amount,
                    adj_type,data_source,source_version,batch_id,run_id,
                    data_as_of_date,input_hash,fetched_at,price_scale_id
                ) VALUES ('ashare', ?, '2026-08-03', ?, ?, ?, ?, 1000, 2000,
                          'raw', 'tushare_daily', 'fake-v1', 'old', 'old',
                          '2026-08-03', 'old-hash', '2026-08-03T16:00:00+08:00', '')
                """,
                (symbol, close - 1, close + 1, close - 2, close),
            )
            connection.execute(
                """
                INSERT INTO daily_metrics (
                    market,symbol,trade_date,turnover_rate,total_mv,circ_mv,
                    data_source,source_version,batch_id,run_id,data_as_of_date,
                    input_hash,fetched_at
                ) VALUES ('ashare', ?, '2026-07-31', 1, 10, 9,
                          'tushare_daily_basic','fake-v1','old','old',
                          '2026-07-31','old','2026-07-31T16:00:00+08:00')
                """,
                (symbol,),
            )
        for index, symbol in enumerate(("000300.SH", "000852.SH", "399006.SZ", "000688.SH")):
            connection.execute(
                """
                INSERT INTO market_index_daily_bars (
                    batch_id,run_id,market,index_symbol,trade_date,open,high,low,close,
                    volume,amount,provider,source_version,fetched_at
                ) VALUES ('old', 'old', 'ashare', ?, '2026-07-31', ?, ?, ?, ?,
                          1000,2000,'FakeTushare','fake-v1','2026-07-31T16:00:00+08:00')
                """,
                (symbol, 999 + index, 1001 + index, 998 + index, 1000 + index),
            )

    config = HistoricalBackfillConfig(
        market_database=path,
        from_date="2026-07-31",
        to_date="2026-08-03",
        minimum_stock_count=2,
        minimum_coverage_ratio=1.0,
    )
    dry_run = backfill_market_history(config, apply=False)

    assert dry_run["status"] == "DRY_RUN"
    assert dry_run["plan"]["factor_dates"] == ["2026-08-03"]
    assert dry_run["plan"]["metric_dates"] == ["2026-08-03"]
    assert dry_run["plan"]["index_dates"] == ["2026-08-03"]

    result = backfill_market_history(config, apply=True, provider=BackfillFakeProvider())

    assert result["status"] == "SUCCESS"
    assert result["inserted"] == {"factors": 2, "metrics": 2, "indices": 4}
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM daily_adjustment_factors WHERE trade_date='2026-08-03'"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM daily_metrics WHERE trade_date='2026-08-03'"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM market_index_daily_bars WHERE trade_date='2026-08-03'"
        ).fetchone()[0] == 4
        assert connection.execute(
            "SELECT COUNT(*) FROM market_history_backfill_receipt"
        ).fetchone()[0] == 1
