from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from statistics import fmean, median
from threading import Lock
from typing import Any

from .industry import (
    INDUSTRY_LEVEL,
    INDUSTRY_SOURCE,
    INDUSTRY_TAXONOMY,
    build_industry_observations,
)

WATCHLIST_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS stock_method_daily_fact (
    as_of_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    method TEXT NOT NULL CHECK (method IN ('minervini', 'weinstein')),
    result TEXT NOT NULL CHECK (result IN ('PASS', 'FAIL', 'UNKNOWN', 'TRANSITION')),
    policy_version TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    origin TEXT NOT NULL CHECK (origin IN ('RECONSTRUCTED', 'OBSERVED')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (as_of_date, symbol, method, policy_version)
);
CREATE TABLE IF NOT EXISTS stock_method_transition (
    as_of_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    method TEXT NOT NULL CHECK (method IN ('minervini', 'weinstein')),
    state TEXT NOT NULL CHECK (
        state IN ('ENTERED', 'CONTINUING', 'EXITED', 'REENTERED', 'DATA_GAP')
    ),
    policy_version TEXT NOT NULL,
    first_qualified_on TEXT,
    streak_started_on TEXT,
    consecutive_sessions INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL CHECK (origin IN ('RECONSTRUCTED', 'OBSERVED')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (as_of_date, symbol, method, policy_version)
);
CREATE TABLE IF NOT EXISTS index_weinstein_weekly_fact (
    effective_date TEXT NOT NULL,
    index_symbol TEXT NOT NULL,
    index_name TEXT NOT NULL,
    stage TEXT NOT NULL CHECK (
        stage IN ('STAGE_1', 'STAGE_2', 'STAGE_3', 'STAGE_4', 'TRANSITION', 'UNKNOWN')
    ),
    stage_started_on TEXT NOT NULL,
    duration_weeks INTEGER NOT NULL DEFAULT 0,
    policy_version TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    origin TEXT NOT NULL CHECK (origin IN ('RECONSTRUCTED', 'OBSERVED')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (effective_date, index_symbol, policy_version)
);
CREATE TABLE IF NOT EXISTS index_minervini_stage2_daily_fact (
    as_of_date TEXT NOT NULL,
    index_symbol TEXT NOT NULL,
    index_name TEXT NOT NULL,
    result TEXT NOT NULL CHECK (result IN ('PASS', 'FAIL', 'UNKNOWN')),
    policy_version TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    origin TEXT NOT NULL CHECK (origin IN ('RECONSTRUCTED', 'OBSERVED')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (as_of_date, index_symbol, policy_version)
);
CREATE TABLE IF NOT EXISTS manual_watch_review (
    symbol TEXT NOT NULL PRIMARY KEY,
    manual_state TEXT NOT NULL CHECK (
        manual_state IN ('UNREVIEWED', 'WATCH', 'FOCUS', 'DROPPED')
    ),
    note TEXT NOT NULL DEFAULT '',
    reviewed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS chart_drawing (
    drawing_id TEXT NOT NULL PRIMARY KEY,
    symbol TEXT NOT NULL,
    price_scale_id TEXT NOT NULL,
    tool TEXT NOT NULL CHECK (tool IN ('trendline', 'horizontal')),
    anchors_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_chart_drawing_symbol_scale
ON chart_drawing(symbol, price_scale_id, created_at);
CREATE TABLE IF NOT EXISTS security_identity_snapshot (
    snapshot_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    industry TEXT NOT NULL DEFAULT '',
    list_date TEXT NOT NULL DEFAULT '',
    is_st INTEGER NOT NULL DEFAULT 0 CHECK (is_st IN (0, 1)),
    is_suspended INTEGER NOT NULL DEFAULT 0 CHECK (is_suspended IN (0, 1)),
    listing_status TEXT NOT NULL DEFAULT '',
    trading_status TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL CHECK (origin IN ('RECONSTRUCTED', 'OBSERVED')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (snapshot_date, symbol)
);
CREATE TABLE IF NOT EXISTS watchlist_run_receipt (
    run_id TEXT NOT NULL PRIMARY KEY,
    as_of_date TEXT NOT NULL,
    from_date TEXT NOT NULL,
    origin TEXT NOT NULL CHECK (origin IN ('RECONSTRUCTED', 'OBSERVED')),
    minervini_policy_version TEXT NOT NULL,
    weinstein_policy_version TEXT NOT NULL,
    market_database TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    counts_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('SUCCESS', 'FAILED')),
    error_message TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS industry_dimension_snapshot (
    snapshot_date TEXT NOT NULL,
    taxonomy TEXT NOT NULL,
    industry_level TEXT NOT NULL,
    industry_code TEXT NOT NULL,
    industry_name TEXT NOT NULL,
    parent_industry_code TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL,
    source_version TEXT NOT NULL DEFAULT '',
    source_digest TEXT NOT NULL,
    origin TEXT NOT NULL CHECK (origin IN ('RECONSTRUCTED', 'OBSERVED')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (snapshot_date, taxonomy, industry_level, industry_code)
);
CREATE TABLE IF NOT EXISTS security_industry_membership_snapshot (
    snapshot_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    taxonomy TEXT NOT NULL,
    industry_level TEXT NOT NULL,
    industry_code TEXT NOT NULL,
    industry_name TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL DEFAULT '',
    assignment_state TEXT NOT NULL CHECK (assignment_state IN ('VERIFIED', 'AMBIGUOUS')),
    source TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    origin TEXT NOT NULL CHECK (origin IN ('RECONSTRUCTED', 'OBSERVED')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (snapshot_date, symbol, taxonomy, industry_level, industry_code)
);
CREATE TABLE IF NOT EXISTS industry_observation_daily_fact (
    as_of_date TEXT NOT NULL,
    taxonomy TEXT NOT NULL,
    industry_level TEXT NOT NULL,
    industry_code TEXT NOT NULL,
    industry_name TEXT NOT NULL,
    eligible_member_count INTEGER NOT NULL,
    mapped_member_count INTEGER NOT NULL,
    weinstein_evaluable_count INTEGER NOT NULL,
    weinstein_pass_count INTEGER NOT NULL,
    weinstein_pass_rate REAL,
    minervini_evaluable_count INTEGER NOT NULL,
    minervini_pass_count INTEGER NOT NULL,
    minervini_pass_rate REAL,
    both_pass_count INTEGER NOT NULL,
    union_pass_count INTEGER NOT NULL,
    w_entered_count INTEGER NOT NULL,
    w_reentered_count INTEGER NOT NULL,
    w_continuing_count INTEGER NOT NULL,
    w_exited_count INTEGER NOT NULL,
    w_data_gap_count INTEGER NOT NULL,
    m_entered_count INTEGER NOT NULL,
    m_reentered_count INTEGER NOT NULL,
    m_continuing_count INTEGER NOT NULL,
    m_exited_count INTEGER NOT NULL,
    m_data_gap_count INTEGER NOT NULL,
    membership_coverage_pct REAL NOT NULL,
    membership_snapshot_date TEXT NOT NULL,
    weinstein_policy_version TEXT NOT NULL,
    minervini_policy_version TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    quality_state TEXT NOT NULL CHECK (quality_state IN ('COMPLETE', 'SMALL_SAMPLE', 'UNKNOWN')),
    source_digest TEXT NOT NULL,
    origin TEXT NOT NULL CHECK (origin IN ('RECONSTRUCTED', 'OBSERVED')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (as_of_date, taxonomy, industry_level, industry_code, policy_version)
);
CREATE INDEX IF NOT EXISTS idx_stock_method_fact_date_result
ON stock_method_daily_fact(as_of_date, method, result);
CREATE INDEX IF NOT EXISTS idx_stock_method_fact_symbol_date
ON stock_method_daily_fact(symbol, as_of_date);
CREATE INDEX IF NOT EXISTS idx_stock_method_transition_date_state
ON stock_method_transition(as_of_date, method, state);
CREATE INDEX IF NOT EXISTS idx_stock_method_transition_symbol_date
ON stock_method_transition(symbol, as_of_date);
CREATE INDEX IF NOT EXISTS idx_index_weinstein_symbol_date
ON index_weinstein_weekly_fact(index_symbol, effective_date);
CREATE INDEX IF NOT EXISTS idx_index_minervini_symbol_date
ON index_minervini_stage2_daily_fact(index_symbol, as_of_date);
CREATE INDEX IF NOT EXISTS idx_security_identity_symbol_date
ON security_identity_snapshot(symbol, snapshot_date);
CREATE INDEX IF NOT EXISTS idx_industry_membership_symbol_date
ON security_industry_membership_snapshot(symbol, snapshot_date);
CREATE INDEX IF NOT EXISTS idx_industry_observation_date_activity
ON industry_observation_daily_fact(as_of_date, union_pass_count);

CREATE TRIGGER IF NOT EXISTS trg_stock_method_daily_fact_no_update
BEFORE UPDATE ON stock_method_daily_fact BEGIN
    SELECT RAISE(ABORT, 'stock_method_daily_fact is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_stock_method_daily_fact_no_delete
BEFORE DELETE ON stock_method_daily_fact BEGIN
    SELECT RAISE(ABORT, 'stock_method_daily_fact is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_stock_method_transition_no_update
BEFORE UPDATE ON stock_method_transition BEGIN
    SELECT RAISE(ABORT, 'stock_method_transition is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_stock_method_transition_no_delete
BEFORE DELETE ON stock_method_transition BEGIN
    SELECT RAISE(ABORT, 'stock_method_transition is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_index_weinstein_weekly_fact_no_update
BEFORE UPDATE ON index_weinstein_weekly_fact BEGIN
    SELECT RAISE(ABORT, 'index_weinstein_weekly_fact is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_index_weinstein_weekly_fact_no_delete
BEFORE DELETE ON index_weinstein_weekly_fact BEGIN
    SELECT RAISE(ABORT, 'index_weinstein_weekly_fact is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_index_minervini_stage2_daily_fact_no_update
BEFORE UPDATE ON index_minervini_stage2_daily_fact BEGIN
    SELECT RAISE(ABORT, 'index_minervini_stage2_daily_fact is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_index_minervini_stage2_daily_fact_no_delete
BEFORE DELETE ON index_minervini_stage2_daily_fact BEGIN
    SELECT RAISE(ABORT, 'index_minervini_stage2_daily_fact is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_watchlist_run_receipt_no_update
BEFORE UPDATE ON watchlist_run_receipt BEGIN
    SELECT RAISE(ABORT, 'watchlist_run_receipt is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_watchlist_run_receipt_no_delete
BEFORE DELETE ON watchlist_run_receipt BEGIN
    SELECT RAISE(ABORT, 'watchlist_run_receipt is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_security_identity_snapshot_no_update
BEFORE UPDATE ON security_identity_snapshot BEGIN
    SELECT RAISE(ABORT, 'security_identity_snapshot is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_security_identity_snapshot_no_delete
BEFORE DELETE ON security_identity_snapshot BEGIN
    SELECT RAISE(ABORT, 'security_identity_snapshot is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_industry_dimension_snapshot_no_update
BEFORE UPDATE ON industry_dimension_snapshot BEGIN
    SELECT RAISE(ABORT, 'industry_dimension_snapshot is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_industry_dimension_snapshot_no_delete
BEFORE DELETE ON industry_dimension_snapshot BEGIN
    SELECT RAISE(ABORT, 'industry_dimension_snapshot is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_security_industry_membership_snapshot_no_update
BEFORE UPDATE ON security_industry_membership_snapshot BEGIN
    SELECT RAISE(ABORT, 'security_industry_membership_snapshot is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_security_industry_membership_snapshot_no_delete
BEFORE DELETE ON security_industry_membership_snapshot BEGIN
    SELECT RAISE(ABORT, 'security_industry_membership_snapshot is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_industry_observation_daily_fact_no_update
BEFORE UPDATE ON industry_observation_daily_fact BEGIN
    SELECT RAISE(ABORT, 'industry_observation_daily_fact is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_industry_observation_daily_fact_no_delete
BEFORE DELETE ON industry_observation_daily_fact BEGIN
    SELECT RAISE(ABORT, 'industry_observation_daily_fact is immutable');
END;
"""


class MarketDataReader:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        if not self.path.is_file():
            raise FileNotFoundError(f"market database does not exist: {self.path}")
        connection = sqlite3.connect(f"file:{self.path.resolve()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    def latest_market_date(self) -> str:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT MAX(trade_date)
                FROM daily_bars
                WHERE market = 'ashare' AND adj_type = 'qfq'
                """
            ).fetchone()
        return str(row[0] or "") if row else ""

    def safe_latest_market_date(self) -> str:
        try:
            return self.latest_market_date()
        except (FileNotFoundError, sqlite3.Error):
            return ""

    def readiness(self) -> dict[str, Any]:
        try:
            receipts = self.collection_receipts(1)
            return {
                "database": str(self.path),
                "exists": self.path.exists(),
                "latest_date": self.latest_market_date(),
                "latest_collection": receipts[0] if receipts else None,
            }
        except (FileNotFoundError, sqlite3.Error) as exc:
            return {
                "database": str(self.path),
                "exists": self.path.exists(),
                "latest_date": "",
                "error": str(exc),
            }

    def collection_receipts(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as connection:
            table = connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type='table' AND name='watchlist_market_collection_receipt'
                """
            ).fetchone()
            if not table:
                return []
            rows = connection.execute(
                """
                SELECT * FROM watchlist_market_collection_receipt
                ORDER BY started_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                **dict(row),
                "quality": json.loads(str(row["quality_json"] or "{}")),
            }
            for row in rows
        ]

    def trading_dates(self, end_date: str) -> list[str]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT trade_date
                FROM daily_bars
                WHERE market = 'ashare' AND adj_type = 'qfq' AND trade_date <= ?
                ORDER BY trade_date
                """,
                (end_date,),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def iter_stock_bars(self, end_date: str) -> Iterator[tuple[str, list[dict[str, Any]]]]:
        connection = self.connect()
        try:
            cursor = connection.execute(
                """
                SELECT symbol, trade_date, open, high, low, close, volume,
                       data_source, source_version, input_hash, price_scale_id
                FROM daily_bars
                WHERE market = 'ashare' AND adj_type = 'qfq' AND trade_date <= ?
                ORDER BY symbol, trade_date
                """,
                (end_date,),
            )
            current_symbol = ""
            values: list[dict[str, Any]] = []
            for row in cursor:
                symbol = str(row["symbol"]).upper()
                if current_symbol and symbol != current_symbol:
                    yield current_symbol, values
                    values = []
                current_symbol = symbol
                values.append(dict(row))
            if current_symbol:
                yield current_symbol, values
        finally:
            connection.close()

    def index_bars(self, index_symbol: str, end_date: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT trade_date, MAX(open) AS open, MAX(high) AS high,
                       MAX(low) AS low, MAX(close) AS close,
                       MAX(volume) AS volume
                FROM market_index_daily_bars
                WHERE market = 'ashare' AND index_symbol = ? AND trade_date <= ?
                GROUP BY trade_date
                ORDER BY trade_date
                """,
                (index_symbol, end_date),
            ).fetchall()
        return [dict(row) for row in rows]

    def stock_chart_bars(
        self,
        symbol: str,
        end_date: str,
        *,
        limit: int = 320,
    ) -> list[dict[str, Any]]:
        """Return one point-in-time, front-adjusted daily OHLC series for chart review."""

        if limit < 30 or limit > 1000:
            raise ValueError("limit must be between 30 and 1000")
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT trade_date, open, high, low, close, volume, amount, price_scale_id
                FROM daily_bars
                WHERE market = 'ashare' AND symbol = ? AND adj_type = 'qfq'
                  AND trade_date <= ? AND data_as_of_date <= ?
                  AND open IS NOT NULL AND high IS NOT NULL AND low IS NOT NULL
                  AND close IS NOT NULL
                ORDER BY trade_date DESC
                LIMIT ?
                """,
                (symbol.upper(), end_date, end_date, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def safe_stock_chart_bars(
        self,
        symbol: str,
        end_date: str,
        *,
        limit: int = 320,
    ) -> list[dict[str, Any]]:
        try:
            return self.stock_chart_bars(symbol, end_date, limit=limit)
        except (FileNotFoundError, sqlite3.Error, ValueError):
            return []

    def security_members(self, as_of_date: str) -> dict[str, dict[str, Any]]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT members_json
                FROM security_master_snapshots
                WHERE market = 'ashare' AND as_of_date <= ?
                ORDER BY as_of_date DESC, created_at DESC
                LIMIT 1
                """,
                (as_of_date,),
            ).fetchone()
        if not row:
            return {}
        members = json.loads(str(row[0]) or "[]")
        result: dict[str, dict[str, Any]] = {}
        for member in members:
            symbol = str(member.get("ts_code") or member.get("symbol") or "").upper()
            if symbol:
                result[symbol] = dict(member)
        return result

    def stock_market_metrics(
        self,
        as_of_date: str,
        symbols: Sequence[str],
        *,
        include_liquidity: bool = True,
    ) -> dict[str, dict[str, Any]]:
        """Return point-in-time size and liquidity context without scoring stocks."""

        requested = sorted({str(symbol).upper() for symbol in symbols if symbol})
        if not requested:
            return {}
        result: dict[str, dict[str, Any]] = {
            symbol: {
                "total_market_cap_yi": None,
                "float_market_cap_yi": None,
                "median_amount_20d_yi": None,
                "amount_session_count": 0,
            }
            for symbol in requested
        }
        amounts: dict[str, list[float]] = {symbol: [] for symbol in requested}
        with self.connect() as connection:
            for start in range(0, len(requested), 800):
                batch = requested[start : start + 800]
                placeholders = ",".join("?" for _ in batch)
                metric_rows = connection.execute(
                    f"""
                    SELECT symbol, total_mv, circ_mv
                    FROM daily_metrics
                    WHERE market = 'ashare' AND trade_date = ?
                      AND symbol IN ({placeholders})
                    """,
                    (as_of_date, *batch),
                ).fetchall()
                for row in metric_rows:
                    symbol = str(row["symbol"]).upper()
                    result[symbol]["total_market_cap_yi"] = (
                        round(float(row["total_mv"]) / 10000.0, 1)
                        if row["total_mv"] is not None
                        else None
                    )
                    result[symbol]["float_market_cap_yi"] = (
                        round(float(row["circ_mv"]) / 10000.0, 1)
                        if row["circ_mv"] is not None
                        else None
                    )
                if include_liquidity:
                    amount_rows = connection.execute(
                        f"""
                        SELECT symbol, amount
                        FROM (
                            SELECT symbol, amount,
                                   ROW_NUMBER() OVER (
                                       PARTITION BY symbol ORDER BY trade_date DESC
                                   ) AS recent_rank
                            FROM daily_bars
                            WHERE market = 'ashare' AND adj_type = 'qfq'
                              AND trade_date <= ? AND amount IS NOT NULL
                              AND symbol IN ({placeholders})
                        )
                        WHERE recent_rank <= 20
                        """,
                        (as_of_date, *batch),
                    ).fetchall()
                    for row in amount_rows:
                        amounts[str(row["symbol"]).upper()].append(float(row["amount"]))
        for symbol, values in amounts.items():
            if values:
                # Tushare amount is stored in thousands of CNY; 100,000 equals 1 yi.
                result[symbol]["median_amount_20d_yi"] = round(
                    median(values) / 100000.0,
                    2,
                )
                result[symbol]["amount_session_count"] = len(values)
        return result

    def safe_stock_market_metrics(
        self,
        as_of_date: str,
        symbols: Sequence[str],
        *,
        include_liquidity: bool = True,
    ) -> dict[str, dict[str, Any]]:
        try:
            return self.stock_market_metrics(
                as_of_date,
                symbols,
                include_liquidity=include_liquidity,
            )
        except (FileNotFoundError, sqlite3.Error):
            return {}

    def stock_quote_changes(
        self,
        as_of_date: str,
        symbols: Sequence[str],
    ) -> dict[str, dict[str, Any]]:
        """Return provider-supplied raw daily quote changes when available."""

        requested = sorted({str(symbol).upper() for symbol in symbols if symbol})
        if not requested:
            return {}
        result: dict[str, dict[str, Any]] = {
            symbol: {"close": None, "change_amount": None, "change_pct": None}
            for symbol in requested
        }
        with self.connect() as connection:
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(daily_bars)").fetchall()
            }
            if "close" not in columns:
                return result
            has_change_fields = {"pre_close", "pct_chg"}.issubset(columns)
            for start in range(0, len(requested), 800):
                batch = requested[start : start + 800]
                placeholders = ",".join("?" for _ in batch)
                rows = connection.execute(
                    f"""
                    SELECT symbol, close,
                           {"pre_close" if has_change_fields else "NULL AS pre_close"},
                           {"pct_chg" if has_change_fields else "NULL AS pct_chg"}
                    FROM daily_bars
                    WHERE market = 'ashare' AND adj_type = 'raw' AND trade_date = ?
                      AND symbol IN ({placeholders})
                    """,
                    (as_of_date, *batch),
                ).fetchall()
                for row in rows:
                    symbol = str(row["symbol"]).upper()
                    close = row["close"]
                    pre_close = row["pre_close"]
                    pct_chg = row["pct_chg"]
                    result[symbol] = {
                        "close": round(float(close), 3) if close is not None else None,
                        "change_amount": (
                            round(float(close) - float(pre_close), 3)
                            if close is not None and pre_close is not None
                            else None
                        ),
                        "change_pct": round(float(pct_chg), 2) if pct_chg is not None else None,
                    }
        return result

    def safe_stock_quote_changes(
        self,
        as_of_date: str,
        symbols: Sequence[str],
    ) -> dict[str, dict[str, Any]]:
        try:
            return self.stock_quote_changes(as_of_date, symbols)
        except (FileNotFoundError, sqlite3.Error):
            return {}

    def industry_proxy_bars(
        self,
        symbols: Sequence[str],
        end_date: str,
        *,
        limit: int = 180,
    ) -> list[dict[str, Any]]:
        """Build an equal-weight OHLC proxy from point-in-time industry members.

        This is deliberately not labelled as an official SW index. Each session's
        composite OHLC is the arithmetic mean of member price relatives against
        their previous available close, chained from a base value of 1000.
        """

        requested = sorted({str(symbol).upper() for symbol in symbols if symbol})
        if not requested or not end_date or limit < 2:
            return []
        with self.connect() as connection:
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(daily_bars)").fetchall()
            }
            required = {"symbol", "trade_date", "open", "high", "low", "close"}
            if not required.issubset(columns):
                return []
            date_rows = connection.execute(
                """
                SELECT DISTINCT trade_date
                FROM daily_bars
                WHERE market = 'ashare' AND adj_type = 'qfq' AND trade_date <= ?
                ORDER BY trade_date DESC
                LIMIT ?
                """,
                (end_date, limit + 1),
            ).fetchall()
            dates = sorted(str(row[0]) for row in date_rows)
            if len(dates) < 2:
                return []
            rows: list[sqlite3.Row] = []
            date_placeholders = ",".join("?" for _ in dates)
            for start in range(0, len(requested), 700):
                batch = requested[start : start + 700]
                symbol_placeholders = ",".join("?" for _ in batch)
                rows.extend(
                    connection.execute(
                        f"""
                        SELECT symbol, trade_date, open, high, low, close
                        FROM daily_bars
                        WHERE market = 'ashare' AND adj_type = 'qfq'
                          AND symbol IN ({symbol_placeholders})
                          AND trade_date IN ({date_placeholders})
                        ORDER BY symbol, trade_date
                        """,
                        (*batch, *dates),
                    ).fetchall()
                )

        by_symbol: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in rows:
            by_symbol[str(row["symbol"]).upper()].append(row)
        relatives: dict[str, list[tuple[float, float, float, float]]] = defaultdict(list)
        for symbol_rows in by_symbol.values():
            previous_close: float | None = None
            for row in symbol_rows:
                close = float(row["close"] or 0)
                open_price = float(row["open"] or 0)
                high = float(row["high"] or 0)
                low = float(row["low"] or 0)
                if previous_close and min(open_price, high, low, close) > 0:
                    relatives[str(row["trade_date"])].append(
                        (
                            open_price / previous_close,
                            high / previous_close,
                            low / previous_close,
                            close / previous_close,
                        )
                    )
                if close > 0:
                    previous_close = close

        result: list[dict[str, Any]] = []
        previous_proxy_close = 1000.0
        for trade_date in sorted(relatives):
            values = relatives[trade_date]
            if not values:
                continue
            proxy_open = previous_proxy_close * fmean(value[0] for value in values)
            proxy_close = previous_proxy_close * fmean(value[3] for value in values)
            proxy_high = max(
                proxy_open,
                proxy_close,
                previous_proxy_close * fmean(value[1] for value in values),
            )
            proxy_low = min(
                proxy_open,
                proxy_close,
                previous_proxy_close * fmean(value[2] for value in values),
            )
            result.append(
                {
                    "trade_date": trade_date,
                    "open": round(proxy_open, 2),
                    "high": round(proxy_high, 2),
                    "low": round(proxy_low, 2),
                    "close": round(proxy_close, 2),
                    "member_count": len(values),
                }
            )
            previous_proxy_close = proxy_close
        return result[-limit:]

    def safe_industry_proxy_bars(
        self,
        symbols: Sequence[str],
        end_date: str,
        *,
        limit: int = 180,
    ) -> list[dict[str, Any]]:
        try:
            return self.industry_proxy_bars(symbols, end_date, limit=limit)
        except (FileNotFoundError, sqlite3.Error, TypeError, ValueError):
            return []

    def source_summary(self, as_of_date: str) -> dict[str, Any]:
        with self.connect() as connection:
            bars = connection.execute(
                """
                SELECT COUNT(*), COUNT(DISTINCT symbol), MIN(trade_date), MAX(trade_date),
                       COUNT(DISTINCT source_version), COUNT(DISTINCT price_scale_id)
                FROM daily_bars
                WHERE market = 'ashare' AND adj_type = 'qfq' AND trade_date <= ?
                """,
                (as_of_date,),
            ).fetchone()
            indices = connection.execute(
                """
                SELECT COUNT(DISTINCT index_symbol), COUNT(DISTINCT trade_date),
                       MIN(trade_date), MAX(trade_date)
                FROM market_index_daily_bars
                WHERE market = 'ashare' AND trade_date <= ?
                """,
                (as_of_date,),
            ).fetchone()
        return {
            "stock_bar_rows": int(bars[0] or 0),
            "stock_symbols": int(bars[1] or 0),
            "stock_start": str(bars[2] or ""),
            "stock_end": str(bars[3] or ""),
            "source_version_count": int(bars[4] or 0),
            "price_scale_count": int(bars[5] or 0),
            "index_symbols": int(indices[0] or 0),
            "index_days": int(indices[1] or 0),
            "index_start": str(indices[2] or ""),
            "index_end": str(indices[3] or ""),
        }


class WatchlistRepository:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._initialized = False
        self._initialize_lock = Lock()

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._initialize_lock:
            if self._initialized:
                return
            with self.connect() as connection:
                connection.executescript(WATCHLIST_SCHEMA_SQL)
            self._initialized = True

    def persist_run(
        self,
        *,
        stock_facts: Iterable[Mapping[str, Any]],
        index_facts: Sequence[Mapping[str, Any]],
        receipt: dict[str, Any],
        identities: Sequence[Mapping[str, Any]] = (),
        index_minervini_facts: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        self.initialize()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            fact_columns = (
                "as_of_date", "symbol", "method", "result", "policy_version",
                "evidence_json", "source_digest", "origin",
            )
            fact_placeholders = ",".join("?" for _ in fact_columns)
            connection.executemany(
                "INSERT OR IGNORE INTO stock_method_daily_fact "
                f"({','.join(fact_columns)}) VALUES ({fact_placeholders})",
                (
                    (
                        row["as_of_date"], row["symbol"], row["method"], row["result"],
                        row["policy_version"], _json(row.get("evidence") or {}),
                        row["source_digest"], row["origin"],
                    )
                    for row in stock_facts
                ),
            )
            self._derive_transitions(connection)
            self._insert_immutable_rows(
                connection,
                "index_weinstein_weekly_fact",
                (
                    "effective_date", "index_symbol", "index_name", "stage",
                    "stage_started_on", "duration_weeks", "policy_version",
                    "evidence_json", "source_digest", "origin",
                ),
                [
                    (
                        row["effective_date"], row["index_symbol"], row["index_name"],
                        row["stage"], row["stage_started_on"],
                        int(row.get("duration_weeks") or 0), row["policy_version"],
                        _json(row.get("evidence") or {}), row["source_digest"], row["origin"],
                    )
                    for row in index_facts
                ],
            )
            self._insert_index_minervini_facts(connection, index_minervini_facts)
            self._insert_immutable_rows(
                connection,
                "security_identity_snapshot",
                (
                    "snapshot_date", "symbol", "name", "industry", "list_date",
                    "is_st", "is_suspended", "listing_status", "trading_status", "origin",
                ),
                [
                    (
                        row["snapshot_date"], row["symbol"], str(row.get("name") or ""),
                        str(row.get("industry") or ""), str(row.get("list_date") or ""),
                        int(bool(row.get("is_st"))), int(bool(row.get("is_suspended"))),
                        str(row.get("listing_status") or ""),
                        str(row.get("trading_status") or ""), row["origin"],
                    )
                    for row in identities
                ],
            )
            self._persist_industry_observations_for_range(
                connection,
                from_date=str(receipt["from_date"]),
                as_of_date=str(receipt["as_of_date"]),
                minervini_policy_version=str(receipt["minervini_policy_version"]),
                weinstein_policy_version=str(receipt["weinstein_policy_version"]),
                origin=str(receipt["origin"]),
            )
            receipt["finished_at"] = datetime.now().astimezone().isoformat(
                timespec="seconds"
            )
            self._insert_immutable_rows(
                connection,
                "watchlist_run_receipt",
                (
                    "run_id", "as_of_date", "from_date", "origin",
                    "minervini_policy_version", "weinstein_policy_version",
                    "market_database", "source_digest", "counts_json", "status",
                    "error_message", "started_at", "finished_at",
                ),
                [
                    (
                        receipt["run_id"], receipt["as_of_date"], receipt["from_date"],
                        receipt["origin"], receipt["minervini_policy_version"],
                        receipt["weinstein_policy_version"], receipt["market_database"],
                        receipt["source_digest"], _json(receipt.get("counts") or {}),
                        receipt["status"], str(receipt.get("error_message") or ""),
                        receipt["started_at"], receipt["finished_at"],
                    )
                ],
            )

    def persist_index_minervini_facts(
        self, facts: Sequence[Mapping[str, Any]]
    ) -> int:
        """Add index Stage 2 facts without rewriting an existing watchlist run."""

        self.initialize()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            before = connection.total_changes
            self._insert_index_minervini_facts(connection, facts)
            return connection.total_changes - before

    def import_industry_snapshot(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Persist a verified mapping and its current observation in one transaction."""

        self.initialize()
        snapshot_date = str(payload.get("snapshot_date") or "")
        dimensions = list(payload.get("dimensions") or [])
        memberships = list(payload.get("memberships") or [])
        if not snapshot_date or not dimensions or not memberships:
            raise ValueError("industry snapshot requires a date, dimensions, and memberships")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._insert_industry_dimensions(connection, dimensions)
            self._insert_industry_memberships(connection, memberships)
            policies = self._fact_policy_versions(connection, snapshot_date)
            observations = self._build_industry_observations_from_connection(
                connection,
                as_of_date=snapshot_date,
                minervini_policy_version=policies["minervini"],
                weinstein_policy_version=policies["weinstein"],
                origin=str(dimensions[0].get("origin") or "RECONSTRUCTED"),
            )
            if not observations:
                raise ValueError(f"no industry observations could be built for {snapshot_date}")
            self._insert_industry_observations(connection, observations)
        return {
            "snapshot_date": snapshot_date,
            "taxonomy": str(payload.get("taxonomy") or INDUSTRY_TAXONOMY),
            "industry_level": str(payload.get("industry_level") or INDUSTRY_LEVEL),
            "source": str(payload.get("source") or INDUSTRY_SOURCE),
            "source_digest": str(payload.get("source_digest") or ""),
            "dimension_count": len(dimensions),
            "membership_count": len(memberships),
            "ambiguous_symbol_count": int(payload.get("ambiguous_symbol_count") or 0),
            "observation_count": len(observations),
            "quality_counts": _count_values(observations, "quality_state"),
        }

    @staticmethod
    def _fact_policy_versions(
        connection: sqlite3.Connection, as_of_date: str
    ) -> dict[str, str]:
        rows = connection.execute(
            """
            SELECT method, policy_version, COUNT(*) AS row_count
            FROM stock_method_daily_fact
            WHERE as_of_date = ? AND method IN ('minervini', 'weinstein')
            GROUP BY method, policy_version
            ORDER BY method, row_count DESC, policy_version DESC
            """,
            (as_of_date,),
        ).fetchall()
        result: dict[str, str] = {}
        for row in rows:
            result.setdefault(str(row["method"]), str(row["policy_version"]))
        missing = {"minervini", "weinstein"} - result.keys()
        if missing:
            raise ValueError(
                f"stock method facts are incomplete for {as_of_date}: {sorted(missing)}"
            )
        return result

    def _persist_industry_observations_for_range(
        self,
        connection: sqlite3.Connection,
        *,
        from_date: str,
        as_of_date: str,
        minervini_policy_version: str,
        weinstein_policy_version: str,
        origin: str,
    ) -> None:
        membership = connection.execute(
            """
            SELECT MAX(snapshot_date) FROM security_industry_membership_snapshot
            WHERE snapshot_date <= ? AND taxonomy = ? AND industry_level = ?
            """,
            (as_of_date, INDUSTRY_TAXONOMY, INDUSTRY_LEVEL),
        ).fetchone()
        if not membership or not membership[0]:
            return
        dates = connection.execute(
            """
            SELECT DISTINCT as_of_date FROM stock_method_daily_fact
            WHERE as_of_date BETWEEN ? AND ? ORDER BY as_of_date
            """,
            (from_date, as_of_date),
        ).fetchall()
        for row in dates:
            observation_date = str(row[0])
            observations = self._build_industry_observations_from_connection(
                connection,
                as_of_date=observation_date,
                minervini_policy_version=minervini_policy_version,
                weinstein_policy_version=weinstein_policy_version,
                origin=origin,
            )
            self._insert_industry_observations(connection, observations)

    @staticmethod
    def _build_industry_observations_from_connection(
        connection: sqlite3.Connection,
        *,
        as_of_date: str,
        minervini_policy_version: str,
        weinstein_policy_version: str,
        origin: str,
    ) -> list[dict[str, Any]]:
        membership_row = connection.execute(
            """
            SELECT MAX(snapshot_date) FROM security_industry_membership_snapshot
            WHERE snapshot_date <= ? AND taxonomy = ? AND industry_level = ?
            """,
            (as_of_date, INDUSTRY_TAXONOMY, INDUSTRY_LEVEL),
        ).fetchone()
        membership_date = str(membership_row[0] or "") if membership_row else ""
        if not membership_date:
            return []
        identity_row = connection.execute(
            "SELECT MAX(snapshot_date) FROM security_identity_snapshot WHERE snapshot_date <= ?",
            (as_of_date,),
        ).fetchone()
        identity_date = str(identity_row[0] or "") if identity_row else ""
        if not identity_date:
            return []
        eligible_symbols = [
            str(row[0])
            for row in connection.execute(
                """
                SELECT symbol FROM security_identity_snapshot
                WHERE snapshot_date = ? AND is_st = 0
                """,
                (identity_date,),
            ).fetchall()
        ]
        membership_rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT * FROM security_industry_membership_snapshot
                WHERE snapshot_date = ? AND taxonomy = ? AND industry_level = ?
                ORDER BY symbol, industry_code
                """,
                (membership_date, INDUSTRY_TAXONOMY, INDUSTRY_LEVEL),
            ).fetchall()
        ]
        method_rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT f.symbol, f.method, f.result, f.policy_version, f.source_digest,
                       COALESCE(t.state, '') AS state
                FROM stock_method_daily_fact AS f
                LEFT JOIN stock_method_transition AS t
                  ON t.as_of_date = f.as_of_date AND t.symbol = f.symbol
                 AND t.method = f.method AND t.policy_version = f.policy_version
                WHERE f.as_of_date = ?
                  AND ((f.method = 'minervini' AND f.policy_version = ?)
                    OR (f.method = 'weinstein' AND f.policy_version = ?))
                ORDER BY f.symbol, f.method
                """,
                (as_of_date, minervini_policy_version, weinstein_policy_version),
            ).fetchall()
        ]
        source_digests = sorted(
            {str(row.get("source_digest") or "") for row in membership_rows}
        )
        return build_industry_observations(
            as_of_date=as_of_date,
            eligible_symbols=eligible_symbols,
            memberships=membership_rows,
            method_facts=method_rows,
            membership_snapshot_date=membership_date,
            membership_source_digest=_sha256_json(source_digests),
            minervini_policy_version=minervini_policy_version,
            weinstein_policy_version=weinstein_policy_version,
            origin=origin,
        )

    @classmethod
    def _insert_industry_dimensions(
        cls, connection: sqlite3.Connection, rows: Sequence[Mapping[str, Any]]
    ) -> None:
        columns = (
            "snapshot_date", "taxonomy", "industry_level", "industry_code",
            "industry_name", "parent_industry_code", "source", "source_version",
            "source_digest", "origin",
        )
        cls._insert_or_verify_rows(
            connection, "industry_dimension_snapshot", columns,
            ("snapshot_date", "taxonomy", "industry_level", "industry_code"), rows,
        )

    @classmethod
    def _insert_industry_memberships(
        cls, connection: sqlite3.Connection, rows: Sequence[Mapping[str, Any]]
    ) -> None:
        columns = (
            "snapshot_date", "symbol", "taxonomy", "industry_level", "industry_code",
            "industry_name", "valid_from", "valid_to", "assignment_state", "source",
            "source_digest", "origin",
        )
        cls._insert_or_verify_rows(
            connection, "security_industry_membership_snapshot", columns,
            ("snapshot_date", "symbol", "taxonomy", "industry_level", "industry_code"), rows,
        )

    @classmethod
    def _insert_industry_observations(
        cls, connection: sqlite3.Connection, rows: Sequence[Mapping[str, Any]]
    ) -> None:
        columns = (
            "as_of_date", "taxonomy", "industry_level", "industry_code", "industry_name",
            "eligible_member_count", "mapped_member_count", "weinstein_evaluable_count",
            "weinstein_pass_count", "weinstein_pass_rate", "minervini_evaluable_count",
            "minervini_pass_count", "minervini_pass_rate", "both_pass_count",
            "union_pass_count", "w_entered_count", "w_reentered_count",
            "w_continuing_count", "w_exited_count", "w_data_gap_count", "m_entered_count",
            "m_reentered_count", "m_continuing_count", "m_exited_count", "m_data_gap_count",
            "membership_coverage_pct", "membership_snapshot_date", "weinstein_policy_version",
            "minervini_policy_version", "policy_version", "quality_state", "source_digest",
            "origin",
        )
        cls._insert_or_verify_rows(
            connection, "industry_observation_daily_fact", columns,
            ("as_of_date", "taxonomy", "industry_level", "industry_code", "policy_version"), rows,
        )

    @staticmethod
    def _insert_or_verify_rows(
        connection: sqlite3.Connection,
        table: str,
        columns: tuple[str, ...],
        primary_key: tuple[str, ...],
        rows: Sequence[Mapping[str, Any]],
    ) -> None:
        placeholders = ",".join("?" for _ in columns)
        insert_sql = f"INSERT OR IGNORE INTO {table} ({','.join(columns)}) VALUES ({placeholders})"
        for row in rows:
            values = tuple(row.get(column) for column in columns)
            cursor = connection.execute(insert_sql, values)
            if cursor.rowcount:
                continue
            where = " AND ".join(f"{column} = ?" for column in primary_key)
            existing = connection.execute(
                f"SELECT {','.join(columns)} FROM {table} WHERE {where}",
                tuple(row.get(column) for column in primary_key),
            ).fetchone()
            if existing is None or tuple(existing[column] for column in columns) != values:
                raise sqlite3.IntegrityError(
                    f"conflicting immutable row in {table}: "
                    + ", ".join(f"{key}={row.get(key)!r}" for key in primary_key)
                )

    @staticmethod
    def _derive_transitions(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO stock_method_transition (
                as_of_date, symbol, method, state, policy_version,
                first_qualified_on, streak_started_on, consecutive_sessions,
                reason, origin
            )
            WITH ordered AS (
                SELECT f.*,
                       LAG(result) OVER (
                           PARTITION BY symbol, method, policy_version ORDER BY as_of_date
                       ) AS previous_result,
                       SUM(CASE WHEN result = 'PASS' THEN 1 ELSE 0 END) OVER (
                           PARTITION BY symbol, method, policy_version
                           ORDER BY as_of_date ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                       ) AS earlier_passes,
                       SUM(CASE WHEN result <> 'PASS' THEN 1 ELSE 0 END) OVER (
                           PARTITION BY symbol, method, policy_version ORDER BY as_of_date
                       ) AS pass_group,
                       MIN(CASE WHEN result = 'PASS' THEN as_of_date END) OVER (
                           PARTITION BY symbol, method, policy_version
                       ) AS first_qualified_on
                FROM stock_method_daily_fact AS f
            ), grouped AS (
                SELECT ordered.*,
                       MIN(CASE WHEN result = 'PASS' THEN as_of_date END) OVER (
                           PARTITION BY symbol, method, policy_version, pass_group
                       ) AS streak_started_on,
                       COUNT(CASE WHEN result = 'PASS' THEN 1 END) OVER (
                           PARTITION BY symbol, method, policy_version, pass_group
                           ORDER BY as_of_date ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                       ) AS consecutive_sessions
                FROM ordered
            )
            SELECT as_of_date, symbol, method,
                   CASE
                       WHEN result = 'PASS' AND previous_result = 'PASS' THEN 'CONTINUING'
                       WHEN result = 'PASS' AND COALESCE(earlier_passes, 0) > 0 THEN 'REENTERED'
                       WHEN result = 'PASS' THEN 'ENTERED'
                       WHEN result = 'UNKNOWN' AND previous_result = 'PASS' THEN 'DATA_GAP'
                       WHEN result <> 'PASS' AND previous_result = 'PASS' THEN 'EXITED'
                   END AS state,
                   policy_version, first_qualified_on,
                   CASE WHEN result = 'PASS' THEN streak_started_on END,
                   CASE WHEN result = 'PASS' THEN consecutive_sessions ELSE 0 END,
                   CASE
                       WHEN result = 'PASS' AND previous_result = 'PASS' THEN 'RULES_STILL_PASS'
                       WHEN result = 'PASS' AND COALESCE(earlier_passes, 0) > 0 THEN 'RULES_PASS_AGAIN'
                       WHEN result = 'PASS' THEN 'FIRST_PASS_IN_STORED_HISTORY'
                       WHEN result = 'UNKNOWN' AND previous_result = 'PASS' THEN 'REQUIRED_INPUT_MISSING'
                       WHEN result <> 'PASS' AND previous_result = 'PASS' THEN 'RULES_NO_LONGER_PASS'
                   END AS reason,
                   origin
            FROM grouped
            WHERE result = 'PASS'
               OR (previous_result = 'PASS' AND result <> 'PASS')
            """
        )

    @staticmethod
    def _insert_index_minervini_facts(
        connection: sqlite3.Connection,
        facts: Sequence[Mapping[str, Any]],
    ) -> None:
        WatchlistRepository._insert_immutable_rows(
            connection,
            "index_minervini_stage2_daily_fact",
            (
                "as_of_date", "index_symbol", "index_name", "result",
                "policy_version", "evidence_json", "source_digest", "origin",
            ),
            [
                (
                    row["as_of_date"], row["index_symbol"], row["index_name"],
                    row["result"], row["policy_version"],
                    _json(row.get("evidence") or {}), row["source_digest"], row["origin"],
                )
                for row in facts
            ],
        )

    @staticmethod
    def _insert_immutable_rows(
        connection: sqlite3.Connection,
        table: str,
        columns: tuple[str, ...],
        rows: Sequence[tuple[Any, ...]],
    ) -> None:
        if not rows:
            return
        placeholders = ",".join("?" for _ in columns)
        column_sql = ",".join(columns)
        sql = f"INSERT OR IGNORE INTO {table} ({column_sql}) VALUES ({placeholders})"
        connection.executemany(sql, rows)

    def latest_fact_date(self) -> str:
        self.initialize()
        with self.connect() as connection:
            row = connection.execute("SELECT MAX(as_of_date) FROM stock_method_daily_fact").fetchone()
        return str(row[0] or "") if row else ""

    def has_fact_date(self, as_of_date: str) -> bool:
        """Return whether a requested snapshot date exists without scanning history."""

        self.initialize()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM stock_method_daily_fact WHERE as_of_date = ? LIMIT 1",
                (as_of_date,),
            ).fetchone()
        return row is not None

    def available_dates(self, limit: int = 30) -> list[str]:
        self.initialize()
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT as_of_date FROM stock_method_daily_fact
                ORDER BY as_of_date DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [str(row[0]) for row in reversed(rows)]

    def watchlist_rows(self, as_of_date: str) -> list[dict[str, Any]]:
        self.initialize()
        with self.connect() as connection:
            fact_rows = connection.execute(
                """
                SELECT f.*, t.state, t.first_qualified_on, t.streak_started_on,
                       t.consecutive_sessions, t.reason AS transition_reason
                FROM stock_method_daily_fact AS f
                LEFT JOIN stock_method_transition AS t
                  ON t.as_of_date = f.as_of_date AND t.symbol = f.symbol
                 AND t.method = f.method AND t.policy_version = f.policy_version
                WHERE f.as_of_date = ?
                  AND (f.result = 'PASS' OR t.state IN ('EXITED', 'DATA_GAP'))
                ORDER BY f.symbol, f.method
                """,
                (as_of_date,),
            ).fetchall()
            review_rows = connection.execute(
                "SELECT symbol, manual_state, note, reviewed_at FROM manual_watch_review"
            ).fetchall()
            identity_rows = connection.execute(
                """
                SELECT identity.* FROM security_identity_snapshot AS identity
                JOIN (
                    SELECT symbol, MAX(snapshot_date) AS snapshot_date
                    FROM security_identity_snapshot
                    WHERE snapshot_date <= ?
                    GROUP BY symbol
                ) AS latest
                  ON latest.symbol = identity.symbol
                 AND latest.snapshot_date = identity.snapshot_date
                """,
                (as_of_date,),
            ).fetchall()
            industry_rows = connection.execute(
                """
                SELECT membership.*
                FROM security_industry_membership_snapshot AS membership
                WHERE membership.snapshot_date = (
                    SELECT MAX(snapshot_date)
                    FROM security_industry_membership_snapshot
                    WHERE snapshot_date <= ? AND taxonomy = ? AND industry_level = ?
                )
                  AND membership.taxonomy = ? AND membership.industry_level = ?
                  AND membership.assignment_state = 'VERIFIED'
                """,
                (
                    as_of_date, INDUSTRY_TAXONOMY, INDUSTRY_LEVEL,
                    INDUSTRY_TAXONOMY, INDUSTRY_LEVEL,
                ),
            ).fetchall()
        reviews = {str(row["symbol"]): dict(row) for row in review_rows}
        identities = {str(row["symbol"]): dict(row) for row in identity_rows}
        industries = {str(row["symbol"]): dict(row) for row in industry_rows}
        grouped: dict[str, dict[str, Any]] = {}
        for row in fact_rows:
            evidence = json.loads(str(row["evidence_json"]) or "{}")
            symbol = str(row["symbol"])
            identity = identities.get(symbol) or dict(evidence.get("eligibility") or {})
            industry = industries.get(symbol) or {}
            item = grouped.setdefault(
                symbol,
                {
                    "symbol": symbol,
                    "name": str(identity.get("name") or ""),
                    "industry": str(industry.get("industry_name") or ""),
                    "industry_code": str(industry.get("industry_code") or ""),
                    "industry_taxonomy": str(industry.get("taxonomy") or ""),
                    "industry_level": str(industry.get("industry_level") or ""),
                    "legacy_industry": str(identity.get("industry") or ""),
                    "is_st": bool(identity.get("is_st")),
                    "is_suspended": bool(identity.get("is_suspended")),
                    "methods": {},
                    "manual": reviews.get(
                        symbol,
                        {"manual_state": "UNREVIEWED", "note": "", "reviewed_at": ""},
                    ),
                    "origin": str(row["origin"]),
                },
            )
            item["methods"][str(row["method"])] = {
                "result": str(row["result"]),
                "state": str(row["state"] or ""),
                "first_qualified_on": str(row["first_qualified_on"] or ""),
                "streak_started_on": str(row["streak_started_on"] or ""),
                "consecutive_sessions": int(row["consecutive_sessions"] or 0),
                "reason": str(row["transition_reason"] or ""),
                "evidence": evidence,
            }
        return list(grouped.values())

    def industry_observations(self, as_of_date: str) -> list[dict[str, Any]]:
        self.initialize()
        with self.connect() as connection:
            try:
                policies = self._fact_policy_versions(connection, as_of_date)
            except ValueError:
                return []
            origin_row = connection.execute(
                """
                SELECT origin FROM stock_method_daily_fact
                WHERE as_of_date = ? ORDER BY origin LIMIT 1
                """,
                (as_of_date,),
            ).fetchone()
            rows = self._build_industry_observations_from_connection(
                connection,
                as_of_date=as_of_date,
                minervini_policy_version=policies["minervini"],
                weinstein_policy_version=policies["weinstein"],
                origin=str(origin_row[0] if origin_row else "RECONSTRUCTED"),
            )
        return sorted(
            rows,
            key=lambda row: (
                -int(row.get("union_pass_count") or 0),
                -int(row.get("both_pass_count") or 0),
                -int(row.get("weinstein_pass_count") or 0),
                -int(row.get("minervini_pass_count") or 0),
                str(row.get("industry_code") or ""),
            ),
        )

    def industry_detail(self, as_of_date: str, industry_code: str) -> dict[str, Any]:
        self.initialize()
        code = industry_code.upper()
        observation = next(
            (
                row
                for row in self.industry_observations(as_of_date)
                if str(row.get("industry_code") or "").upper() == code
            ),
            None,
        )
        if not observation:
            return {}
        with self.connect() as connection:
            membership_date = str(observation["membership_snapshot_date"])
            members = connection.execute(
                """
                SELECT membership.symbol, membership.industry_code,
                       membership.industry_name, identity.name,
                       wf.result AS weinstein_result, wt.state AS weinstein_state,
                       mf.result AS minervini_result, mt.state AS minervini_state
                FROM security_industry_membership_snapshot AS membership
                LEFT JOIN security_identity_snapshot AS identity
                  ON identity.snapshot_date = (
                      SELECT MAX(i2.snapshot_date) FROM security_identity_snapshot AS i2
                      WHERE i2.symbol = membership.symbol AND i2.snapshot_date <= ?
                  ) AND identity.symbol = membership.symbol
                LEFT JOIN stock_method_daily_fact AS wf
                  ON wf.as_of_date = ? AND wf.symbol = membership.symbol
                 AND wf.method = 'weinstein'
                 AND wf.policy_version = ?
                LEFT JOIN stock_method_transition AS wt
                  ON wt.as_of_date = wf.as_of_date AND wt.symbol = wf.symbol
                 AND wt.method = wf.method AND wt.policy_version = wf.policy_version
                LEFT JOIN stock_method_daily_fact AS mf
                  ON mf.as_of_date = ? AND mf.symbol = membership.symbol
                 AND mf.method = 'minervini'
                 AND mf.policy_version = ?
                LEFT JOIN stock_method_transition AS mt
                  ON mt.as_of_date = mf.as_of_date AND mt.symbol = mf.symbol
                 AND mt.method = mf.method AND mt.policy_version = mf.policy_version
                WHERE membership.snapshot_date = ?
                  AND membership.taxonomy = ? AND membership.industry_level = ?
                  AND membership.industry_code = ?
                  AND membership.assignment_state = 'VERIFIED'
                  AND COALESCE(identity.is_st, 0) = 0
                ORDER BY CASE
                    WHEN wf.result = 'PASS' AND mf.result = 'PASS' THEN 1
                    WHEN wf.result = 'PASS' THEN 2
                    WHEN mf.result = 'PASS' THEN 3 ELSE 4 END,
                    membership.symbol
                """,
                (
                    as_of_date,
                    as_of_date,
                    str(observation["weinstein_policy_version"]),
                    as_of_date,
                    str(observation["minervini_policy_version"]),
                    membership_date,
                    INDUSTRY_TAXONOMY,
                    INDUSTRY_LEVEL,
                    code,
                ),
            ).fetchall()
        return {"observation": dict(observation), "members": [dict(row) for row in members]}

    def index_facts(self, as_of_date: str) -> list[dict[str, Any]]:
        self.initialize()
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT f.*
                FROM index_weinstein_weekly_fact AS f
                JOIN (
                    SELECT index_symbol, MAX(effective_date) AS effective_date
                    FROM index_weinstein_weekly_fact
                    WHERE effective_date <= ?
                    GROUP BY index_symbol
                ) AS latest
                  ON latest.index_symbol = f.index_symbol
                 AND latest.effective_date = f.effective_date
                ORDER BY CASE f.index_symbol
                    WHEN '000300.SH' THEN 1 WHEN '000852.SH' THEN 2
                    WHEN '399006.SZ' THEN 3 WHEN '000688.SH' THEN 4 ELSE 9 END
                """,
                (as_of_date,),
            ).fetchall()
            minervini_rows = connection.execute(
                """
                SELECT f.*
                FROM index_minervini_stage2_daily_fact AS f
                JOIN (
                    SELECT index_symbol, MAX(as_of_date) AS as_of_date
                    FROM index_minervini_stage2_daily_fact
                    WHERE as_of_date <= ?
                    GROUP BY index_symbol
                ) AS latest
                  ON latest.index_symbol = f.index_symbol
                 AND latest.as_of_date = f.as_of_date
                """,
                (as_of_date,),
            ).fetchall()
        minervini_by_symbol = {
            str(row["index_symbol"]): {
                **dict(row),
                "evidence": json.loads(str(row["evidence_json"]) or "{}"),
                "is_stage2": str(row["result"]) == "PASS",
            }
            for row in minervini_rows
        }
        result: list[dict[str, Any]] = []
        for row in rows:
            item = {
                **dict(row),
                "evidence": json.loads(str(row["evidence_json"]) or "{}"),
            }
            item["minervini"] = minervini_by_symbol.get(
                str(row["index_symbol"]),
                {
                    "as_of_date": as_of_date,
                    "index_symbol": str(row["index_symbol"]),
                    "index_name": str(row["index_name"]),
                    "result": "UNKNOWN",
                    "policy_version": "",
                    "evidence": {"reason": "NO_MINERVINI_INDEX_FACT"},
                    "is_stage2": False,
                },
            )
            result.append(item)
        return result

    def stock_detail(self, symbol: str, limit: int = 260) -> dict[str, Any]:
        self.initialize()
        symbol = symbol.upper()
        with self.connect() as connection:
            facts = connection.execute(
                """
                SELECT f.*, t.state, t.first_qualified_on, t.streak_started_on,
                       t.consecutive_sessions, t.reason AS transition_reason
                FROM stock_method_daily_fact AS f
                LEFT JOIN stock_method_transition AS t
                  ON t.as_of_date = f.as_of_date AND t.symbol = f.symbol
                 AND t.method = f.method AND t.policy_version = f.policy_version
                WHERE f.symbol = ?
                ORDER BY f.as_of_date DESC, f.method
                LIMIT ?
                """,
                (symbol, limit * 2),
            ).fetchall()
            review = connection.execute(
                "SELECT * FROM manual_watch_review WHERE symbol = ?", (symbol,)
            ).fetchone()
            identity_row = connection.execute(
                """
                SELECT * FROM security_identity_snapshot
                WHERE symbol = ?
                  AND snapshot_date <= COALESCE((
                      SELECT MAX(as_of_date) FROM stock_method_daily_fact WHERE symbol = ?
                  ), '')
                ORDER BY snapshot_date DESC LIMIT 1
                """,
                (symbol, symbol),
            ).fetchone()
            industry_row = connection.execute(
                """
                SELECT * FROM security_industry_membership_snapshot
                WHERE symbol = ? AND taxonomy = ? AND industry_level = ?
                  AND assignment_state = 'VERIFIED'
                  AND snapshot_date <= COALESCE((
                      SELECT MAX(as_of_date) FROM stock_method_daily_fact WHERE symbol = ?
                  ), '')
                ORDER BY snapshot_date DESC LIMIT 1
                """,
                (symbol, INDUSTRY_TAXONOMY, INDUSTRY_LEVEL, symbol),
            ).fetchone()
            history_total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM stock_method_daily_fact WHERE symbol = ?",
                    (symbol,),
                ).fetchone()[0]
            )
        decoded = [
            {
                **dict(row),
                "evidence": json.loads(str(row["evidence_json"]) or "{}"),
            }
            for row in facts
        ]
        latest: dict[str, dict[str, Any]] = {}
        identity: dict[str, Any] = dict(identity_row) if identity_row else {}
        industry: dict[str, Any] = dict(industry_row) if industry_row else {}
        for row in decoded:
            method = str(row["method"])
            latest.setdefault(method, row)
            if not identity:
                identity = dict(row["evidence"].get("eligibility") or {})
        return {
            "symbol": symbol,
            "identity": identity,
            "industry": industry,
            "latest": latest,
            "history": decoded,
            "history_total": history_total,
            "manual": dict(review) if review else {
                "symbol": symbol,
                "manual_state": "UNREVIEWED",
                "note": "",
                "reviewed_at": "",
            },
        }

    def save_review(self, symbol: str, manual_state: str, note: str) -> None:
        allowed = {"UNREVIEWED", "WATCH", "FOCUS", "DROPPED"}
        state = manual_state.upper()
        if state not in allowed:
            raise ValueError(f"unsupported manual state: {manual_state}")
        self.initialize()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO manual_watch_review(symbol, manual_state, note, reviewed_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(symbol) DO UPDATE SET
                    manual_state=excluded.manual_state,
                    note=excluded.note,
                    reviewed_at=CURRENT_TIMESTAMP
                """,
                (symbol.upper(), state, note.strip()),
            )

    def chart_drawings(self, symbol: str, price_scale_id: str) -> list[dict[str, Any]]:
        self.initialize()
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT drawing_id, tool, anchors_json, created_at, updated_at
                FROM chart_drawing
                WHERE symbol = ? AND price_scale_id = ?
                ORDER BY created_at, drawing_id
                """,
                (symbol.upper(), price_scale_id),
            ).fetchall()
        return [{**dict(row), "anchors": json.loads(str(row["anchors_json"]))} for row in rows]

    def save_chart_drawing(
        self,
        drawing_id: str,
        symbol: str,
        price_scale_id: str,
        tool: str,
        anchors: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        if tool not in {"trendline", "horizontal"}:
            raise ValueError("unsupported drawing tool")
        if not price_scale_id:
            raise ValueError("missing price scale identifier")
        expected_count = 2 if tool == "trendline" else 1
        if len(anchors) != expected_count:
            raise ValueError(f"{tool} requires {expected_count} anchors")
        normalized: list[dict[str, float | str]] = []
        for anchor in anchors:
            date = str(anchor.get("date") or "")
            logical = anchor.get("logical")
            price = anchor.get("price")
            if not isinstance(price, (int, float)) or float(price) <= 0:
                raise ValueError("invalid drawing anchor")
            if len(date) == 10:
                normalized.append({"date": date, "price": round(float(price), 6)})
            elif isinstance(logical, (int, float)):
                normalized.append({"logical": round(float(logical), 4), "price": round(float(price), 6)})
            else:
                raise ValueError("invalid drawing anchor")
        self.initialize()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO chart_drawing(drawing_id, symbol, price_scale_id, tool, anchors_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(drawing_id) DO UPDATE SET
                    anchors_json=excluded.anchors_json, updated_at=CURRENT_TIMESTAMP
                """,
                (
                    drawing_id,
                    symbol.upper(),
                    price_scale_id,
                    tool,
                    json.dumps(normalized, ensure_ascii=False, separators=(",", ":")),
                ),
            )
        return {"drawing_id": drawing_id, "tool": tool, "anchors": normalized}

    def delete_chart_drawing(self, drawing_id: str, symbol: str, price_scale_id: str) -> bool:
        self.initialize()
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM chart_drawing WHERE drawing_id = ? AND symbol = ? AND price_scale_id = ?",
                (drawing_id, symbol.upper(), price_scale_id),
            )
        return cursor.rowcount == 1

    def run_receipts(self, limit: int = 20) -> list[dict[str, Any]]:
        self.initialize()
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM watchlist_run_receipt ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {**dict(row), "counts": json.loads(str(row["counts_json"]) or "{}")}
            for row in rows
        ]

    def readiness(self) -> dict[str, Any]:
        self.initialize()
        with self.connect() as connection:
            connected = int(connection.execute("SELECT 1").fetchone()[0]) == 1
            fact_state = connection.execute(
                """
                SELECT EXISTS(SELECT 1 FROM stock_method_daily_fact LIMIT 1),
                       (SELECT MAX(as_of_date) FROM stock_method_daily_fact)
                """
            ).fetchone()
        return {
            "database": str(self.path),
            "exists": self.path.exists(),
            "connected": connected,
            "integrity": "not_run_by_liveness_check",
            "has_facts": bool(fact_state[0]) if fact_state else False,
            "latest_fact_date": str(fact_state[1] or "") if fact_state else "",
        }


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_json(value: Any) -> str:
    payload = _json(value).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _count_values(rows: Sequence[Mapping[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "")
        counts[value] = counts.get(value, 0) + 1
    return counts
