from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from .collector import (
    INDEX_SYMBOLS,
    MARKET,
    _assert_required_tables,
    _digest,
    _now,
    _valid_daily_row,
    _write_factors,
    _write_indices,
    _write_metrics,
)
from .market_provider import TushareMarketProvider

BACKFILL_RECEIPT_SQL = """
CREATE TABLE IF NOT EXISTS market_history_backfill_receipt (
    run_id TEXT NOT NULL PRIMARY KEY,
    from_date TEXT NOT NULL,
    to_date TEXT NOT NULL,
    provider TEXT NOT NULL,
    source_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('SUCCESS')),
    factor_date_count INTEGER NOT NULL,
    metric_date_count INTEGER NOT NULL,
    index_date_count INTEGER NOT NULL,
    factor_row_count INTEGER NOT NULL,
    metric_row_count INTEGER NOT NULL,
    index_row_count INTEGER NOT NULL,
    request_count INTEGER NOT NULL,
    plan_json TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TRIGGER IF NOT EXISTS trg_market_history_backfill_receipt_no_update
BEFORE UPDATE ON market_history_backfill_receipt BEGIN
    SELECT RAISE(ABORT, 'market_history_backfill_receipt is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_market_history_backfill_receipt_no_delete
BEFORE DELETE ON market_history_backfill_receipt BEGIN
    SELECT RAISE(ABORT, 'market_history_backfill_receipt is immutable');
END;
"""


class HistoricalMarketProvider(Protocol):
    source_name: str
    source_version: str
    request_count: int

    def assert_ready(self) -> None: ...

    def trade_calendar(self, start_date: str, end_date: str) -> list[str]: ...

    def market_adjustment_factors(self, trade_date: str) -> dict[str, float]: ...

    def market_daily_metrics(self, trade_date: str) -> dict[str, dict[str, Any]]: ...

    def index_daily_bars(
        self, trade_date: str, index_symbols: tuple[str, ...]
    ) -> dict[str, dict[str, Any]]: ...


@dataclass(frozen=True)
class HistoricalBackfillConfig:
    market_database: Path
    from_date: str
    to_date: str = ""
    minimum_stock_count: int = 4500
    minimum_coverage_ratio: float = 0.95


def plan_market_history_backfill(config: HistoricalBackfillConfig) -> dict[str, Any]:
    _assert_required_tables(config.market_database)
    if config.minimum_stock_count < 1:
        raise ValueError("minimum_stock_count must be positive")
    if not 0 < config.minimum_coverage_ratio <= 1:
        raise ValueError("minimum_coverage_ratio must be in (0, 1]")
    with sqlite3.connect(config.market_database) as connection:
        available = connection.execute(
            """
            SELECT MIN(trade_date), MAX(trade_date) FROM daily_bars
            WHERE market=? AND adj_type='raw' AND trade_date>=?
              AND (?='' OR trade_date<=?)
            """,
            (MARKET, config.from_date, config.to_date, config.to_date),
        ).fetchone()
        if not available or not available[0]:
            raise ValueError("指定区间没有本地原始股票日线，无法确定补采交易日")
        from_date = str(available[0])
        to_date = str(available[1])
        trading_dates = _trading_dates(connection, from_date, to_date)
        factor_dates, factor_rows = _stock_gap_summary(
            connection, from_date, to_date, "daily_adjustment_factors"
        )
        metric_dates, metric_rows = _stock_gap_summary(
            connection, from_date, to_date, "daily_metrics"
        )
        index_dates = _dates_with_index_gaps(connection, trading_dates)
    return {
        "from_date": from_date,
        "to_date": to_date,
        "trading_date_count": len(trading_dates),
        "factor_dates": factor_dates,
        "metric_dates": metric_dates,
        "index_dates": index_dates,
        "missing_factor_rows": factor_rows,
        "missing_metric_rows": metric_rows,
        "missing_index_rows": len(index_dates) * len(INDEX_SYMBOLS),
    }


def backfill_market_history(
    config: HistoricalBackfillConfig,
    *,
    apply: bool,
    provider: HistoricalMarketProvider | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    plan = plan_market_history_backfill(config)
    if not apply:
        return {"status": "DRY_RUN", "plan": plan}
    if not any(plan[key] for key in ("factor_dates", "metric_dates", "index_dates")):
        return {"status": "SUCCESS", "backfill_state": "REUSED", "plan": plan}

    active_provider = provider or TushareMarketProvider()
    active_provider.assert_ready()
    open_dates = active_provider.trade_calendar(plan["from_date"], plan["to_date"])
    with sqlite3.connect(config.market_database) as connection:
        local_dates = _trading_dates(connection, plan["from_date"], plan["to_date"])
        expected_stock_counts = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                """
                SELECT trade_date, COUNT(*) FROM daily_bars
                WHERE market=? AND adj_type='raw' AND trade_date BETWEEN ? AND ?
                GROUP BY trade_date
                """,
                (MARKET, plan["from_date"], plan["to_date"]),
            ).fetchall()
        }
    missing_calendar_dates = sorted(set(local_dates) - set(open_dates))
    if missing_calendar_dates:
        raise ValueError(
            "Tushare 交易日历不包含本地原始日线日期: "
            + ", ".join(missing_calendar_dates[:5])
        )

    started_at = _now()
    run_id = f"history-backfill-{plan['from_date']}-{plan['to_date']}-{uuid4().hex[:10]}"
    inserted = {"factors": 0, "metrics": 0, "indices": 0}
    total_steps = len(plan["factor_dates"]) + len(plan["metric_dates"]) + len(plan["index_dates"])
    completed_steps = 0

    for trade_date in plan["factor_dates"]:
        factor_values = active_provider.market_adjustment_factors(trade_date)
        inserted["factors"] += _persist_missing_stock_dataset(
            config,
            run_id=run_id,
            trade_date=trade_date,
            values=factor_values,
            dataset="factors",
            provider=active_provider,
            expected_stock_count=expected_stock_counts[trade_date],
        )
        completed_steps += 1
        _report(progress, completed_steps, total_steps, "复权因子", trade_date)

    for trade_date in plan["metric_dates"]:
        metric_values = active_provider.market_daily_metrics(trade_date)
        inserted["metrics"] += _persist_missing_stock_dataset(
            config,
            run_id=run_id,
            trade_date=trade_date,
            values=metric_values,
            dataset="metrics",
            provider=active_provider,
            expected_stock_count=expected_stock_counts[trade_date],
        )
        completed_steps += 1
        _report(progress, completed_steps, total_steps, "市值指标", trade_date)

    index_values = _fetch_index_history(
        active_provider,
        plan["index_dates"],
        plan["from_date"],
        plan["to_date"],
    )
    for trade_date in plan["index_dates"]:
        rows = index_values.get(trade_date, {})
        missing = sorted(set(INDEX_SYMBOLS) - set(rows))
        invalid = sorted(
            symbol
            for symbol, row in rows.items()
            if symbol in INDEX_SYMBOLS and not _valid_daily_row(row, trade_date)
        )
        if missing or invalid:
            raise ValueError(
                f"{trade_date} 四指数日线质量门未通过; "
                f"missing={missing}; invalid={invalid}"
            )
        inserted["indices"] += _persist_missing_indices(
            config.market_database,
            run_id=run_id,
            trade_date=trade_date,
            values=rows,
            provider=active_provider,
        )
        completed_steps += 1
        _report(progress, completed_steps, total_steps, "四指数", trade_date)

    final_plan = plan_market_history_backfill(config)
    if any(final_plan[key] for key in ("factor_dates", "metric_dates", "index_dates")):
        raise ValueError("补采后仍有数据缺口，未写入成功凭证")
    finished_at = _now()
    with sqlite3.connect(config.market_database, timeout=30) as connection:
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.executescript(BACKFILL_RECEIPT_SQL)
        connection.execute(
            """
            INSERT INTO market_history_backfill_receipt (
                run_id, from_date, to_date, provider, source_version, status,
                factor_date_count, metric_date_count, index_date_count,
                factor_row_count, metric_row_count, index_row_count,
                request_count, plan_json, started_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, 'SUCCESS', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                plan["from_date"],
                plan["to_date"],
                active_provider.source_name,
                active_provider.source_version,
                len(plan["factor_dates"]),
                len(plan["metric_dates"]),
                len(plan["index_dates"]),
                inserted["factors"],
                inserted["metrics"],
                inserted["indices"],
                active_provider.request_count,
                json.dumps(plan, ensure_ascii=False, sort_keys=True),
                started_at,
                finished_at,
            ),
        )
    return {
        "status": "SUCCESS",
        "backfill_state": "COLLECTED",
        "run_id": run_id,
        "plan": plan,
        "inserted": inserted,
        "request_count": active_provider.request_count,
        "started_at": started_at,
        "finished_at": finished_at,
    }


def _trading_dates(
    connection: sqlite3.Connection, from_date: str, to_date: str
) -> list[str]:
    return [
        str(row[0])
        for row in connection.execute(
            """
            SELECT DISTINCT trade_date FROM daily_bars
            WHERE market=? AND adj_type='raw' AND trade_date BETWEEN ? AND ?
            ORDER BY trade_date
            """,
            (MARKET, from_date, to_date),
        ).fetchall()
    ]


def _stock_gap_summary(
    connection: sqlite3.Connection,
    from_date: str,
    to_date: str,
    table: str,
) -> tuple[list[str], int]:
    rows = connection.execute(
        f"""
        WITH raw_counts AS (
            SELECT trade_date, COUNT(*) AS row_count
            FROM daily_bars
            WHERE market=? AND adj_type='raw' AND trade_date BETWEEN ? AND ?
            GROUP BY trade_date
        ), stored_counts AS (
            SELECT trade_date, COUNT(*) AS row_count
            FROM {table}
            WHERE market=? AND trade_date BETWEEN ? AND ?
            GROUP BY trade_date
        )
        SELECT raw.trade_date,
               raw.row_count - COALESCE(stored.row_count, 0) AS missing_count
        FROM raw_counts AS raw
        LEFT JOIN stored_counts AS stored USING (trade_date)
        WHERE COALESCE(stored.row_count, 0) < raw.row_count
        ORDER BY raw.trade_date
        """,
        (MARKET, from_date, to_date, MARKET, from_date, to_date),
    ).fetchall()
    return [str(row[0]) for row in rows], sum(int(row[1]) for row in rows)


def _dates_with_index_gaps(
    connection: sqlite3.Connection, trading_dates: list[str]
) -> list[str]:
    return [
        trade_date
        for trade_date in trading_dates
        if int(
            connection.execute(
                """
                SELECT COUNT(DISTINCT index_symbol) FROM market_index_daily_bars
                WHERE market=? AND trade_date=?
                  AND index_symbol IN ('000300.SH','000852.SH','399006.SZ','000688.SH')
                """,
                (MARKET, trade_date),
            ).fetchone()[0]
        )
        < len(INDEX_SYMBOLS)
    ]


def _persist_missing_stock_dataset(
    config: HistoricalBackfillConfig,
    *,
    run_id: str,
    trade_date: str,
    values: Mapping[str, Any],
    dataset: str,
    provider: HistoricalMarketProvider,
    expected_stock_count: int,
) -> int:
    table = "daily_adjustment_factors" if dataset == "factors" else "daily_metrics"
    with sqlite3.connect(config.market_database, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        coverage = len(values) / expected_stock_count if expected_stock_count else 0.0
        if len(values) < config.minimum_stock_count or coverage < config.minimum_coverage_ratio:
            raise ValueError(
                f"{trade_date} {dataset} 质量门未通过; "
                f"rows={len(values)} coverage={coverage:.4%}"
            )
        existing = {
            str(row[0])
            for row in connection.execute(
                f"SELECT symbol FROM {table} WHERE market=? AND trade_date=?",
                (MARKET, trade_date),
            ).fetchall()
        }
        missing_values = {
            symbol: values[symbol] for symbol in sorted(set(values) - existing)
        }
        if not missing_values:
            return 0
        fetched_at = _now()
        batch_id = f"history-{dataset}-{trade_date}-{run_id[-10:]}"
        connection.execute("BEGIN IMMEDIATE")
        if dataset == "factors":
            _write_factors(
                connection,
                target_date=trade_date,
                run_id=run_id,
                batch_id=batch_id,
                fetched_at=fetched_at,
                source_version=provider.source_version,
                factors=missing_values,
            )
        else:
            _write_metrics(
                connection,
                target_date=trade_date,
                run_id=run_id,
                batch_id=batch_id,
                fetched_at=fetched_at,
                source_version=provider.source_version,
                content_hash=_digest(missing_values),
                metrics=missing_values,
            )
        connection.commit()
    return len(missing_values)


def _fetch_index_history(
    provider: HistoricalMarketProvider,
    dates: list[str],
    from_date: str,
    to_date: str,
) -> dict[str, dict[str, dict[str, Any]]]:
    if not dates:
        return {}
    range_method = getattr(provider, "index_daily_bars_range", None)
    if callable(range_method):
        return dict(range_method(from_date, to_date, INDEX_SYMBOLS))
    return {date: provider.index_daily_bars(date, INDEX_SYMBOLS) for date in dates}


def _persist_missing_indices(
    path: Path,
    *,
    run_id: str,
    trade_date: str,
    values: Mapping[str, Mapping[str, Any]],
    provider: HistoricalMarketProvider,
) -> int:
    with sqlite3.connect(path, timeout=30) as connection:
        connection.execute("PRAGMA busy_timeout = 30000")
        existing = {
            str(row[0])
            for row in connection.execute(
                """SELECT DISTINCT index_symbol FROM market_index_daily_bars
                   WHERE market=? AND trade_date=?""",
                (MARKET, trade_date),
            ).fetchall()
        }
        missing_values = {
            symbol: values[symbol]
            for symbol in INDEX_SYMBOLS
            if symbol not in existing
        }
        if not missing_values:
            return 0
        connection.execute("BEGIN IMMEDIATE")
        _write_indices(
            connection,
            target_date=trade_date,
            run_id=run_id,
            batch_id=f"history-index-{trade_date}-{run_id[-10:]}",
            fetched_at=_now(),
            source_name=provider.source_name,
            source_version=provider.source_version,
            indices=missing_values,
        )
        connection.commit()
    return len(missing_values)


def _report(
    progress: Callable[[str], None] | None,
    completed: int,
    total: int,
    dataset: str,
    trade_date: str,
) -> None:
    if progress:
        progress(f"[{completed}/{total}] {dataset} {trade_date}")
