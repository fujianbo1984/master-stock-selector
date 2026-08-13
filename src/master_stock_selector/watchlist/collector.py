from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from .market_provider import TushareMarketProvider
from .service import INDEX_UNIVERSE

MARKET = "ashare"
QFQ_DERIVATION_VERSION = "qfq-adj-factor-ratio-v2"
INDEX_SYMBOLS = tuple(symbol for symbol, _ in INDEX_UNIVERSE)
BREADTH_BENCHMARK_SYMBOLS = ("000985.CSI", "000001.SH")
MARKET_INDEX_SYMBOLS = (*INDEX_SYMBOLS, *BREADTH_BENCHMARK_SYMBOLS)

COLLECTION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS watchlist_market_collection_receipt (
    run_id TEXT NOT NULL PRIMARY KEY,
    trade_date TEXT NOT NULL,
    provider TEXT NOT NULL,
    source_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('SUCCESS')),
    stock_count INTEGER NOT NULL,
    expected_universe_count INTEGER NOT NULL,
    coverage_ratio REAL NOT NULL,
    metrics_count INTEGER NOT NULL,
    index_count INTEGER NOT NULL,
    rescaled_symbol_count INTEGER NOT NULL,
    request_count INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    quality_json TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_watchlist_collection_success_date
ON watchlist_market_collection_receipt(trade_date, status);
CREATE TABLE IF NOT EXISTS security_master_history (
    valid_from TEXT NOT NULL,
    symbol TEXT NOT NULL,
    code TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    industry TEXT NOT NULL DEFAULT '',
    board TEXT NOT NULL DEFAULT '',
    list_date TEXT NOT NULL DEFAULT '',
    delist_date TEXT NOT NULL DEFAULT '',
    provider_list_status TEXT NOT NULL DEFAULT '',
    listing_status_as_of TEXT NOT NULL DEFAULT '',
    is_st INTEGER NOT NULL DEFAULT 0 CHECK (is_st IN (0, 1)),
    st_status TEXT NOT NULL DEFAULT '',
    st_type TEXT NOT NULL DEFAULT '',
    is_suspended INTEGER NOT NULL DEFAULT 0 CHECK (is_suspended IN (0, 1)),
    suspend_reason TEXT NOT NULL DEFAULT '',
    trading_status TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL,
    source_version TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (symbol, valid_from)
);
CREATE INDEX IF NOT EXISTS idx_security_master_history_asof
ON security_master_history(symbol, valid_from);
CREATE INDEX IF NOT EXISTS idx_daily_bars_qfq_amount_lookup
ON daily_bars(market, adj_type, symbol, trade_date DESC, amount)
WHERE amount IS NOT NULL;
CREATE TRIGGER IF NOT EXISTS trg_watchlist_market_collection_receipt_no_update
BEFORE UPDATE ON watchlist_market_collection_receipt BEGIN
    SELECT RAISE(ABORT, 'watchlist_market_collection_receipt is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_watchlist_market_collection_receipt_no_delete
BEFORE DELETE ON watchlist_market_collection_receipt BEGIN
    SELECT RAISE(ABORT, 'watchlist_market_collection_receipt is immutable');
END;
"""

REQUIRED_TABLES = {
    "daily_bars",
    "daily_metrics",
    "daily_adjustment_factors",
    "market_index_daily_bars",
}


class MarketProvider(Protocol):
    source_name: str
    source_version: str
    request_count: int

    def assert_ready(self) -> None: ...

    def trade_calendar(self, start_date: str, end_date: str) -> list[str]: ...

    def stock_basic(self) -> list[dict[str, Any]]: ...

    def market_daily_bars(self, trade_date: str) -> dict[str, dict[str, Any]]: ...

    def market_adjustment_factors(self, trade_date: str) -> dict[str, float]: ...

    def market_daily_metrics(self, trade_date: str) -> dict[str, dict[str, Any]]: ...

    def index_daily_bars(
        self,
        trade_date: str,
        index_symbols: tuple[str, ...],
    ) -> dict[str, dict[str, Any]]: ...


@dataclass(frozen=True)
class CollectionConfig:
    market_database: Path
    trade_date: str = ""
    minimum_stock_count: int = 4500
    minimum_coverage_ratio: float = 0.95


def collect_market_data(
    config: CollectionConfig,
    provider: MarketProvider | None = None,
) -> dict[str, Any]:
    target_date = config.trade_date or date.today().isoformat()
    _validate_date(target_date)
    if config.minimum_stock_count < 1:
        raise ValueError("minimum_stock_count must be positive")
    if not 0 < config.minimum_coverage_ratio <= 1:
        raise ValueError("minimum_coverage_ratio must be in (0, 1]")
    if not config.market_database.is_file():
        raise FileNotFoundError(f"market database does not exist: {config.market_database}")
    _assert_required_tables(config.market_database)

    existing = _existing_success(config.market_database, target_date)
    if existing:
        return {**existing, "collection_state": "REUSED"}
    latest_date = _latest_qfq_date(config.market_database)
    if latest_date and target_date <= latest_date:
        raise ValueError(
            f"目标日期 {target_date} 不晚于本地最新行情 {latest_date}，且没有本闭环的成功凭证"
        )

    active_provider = provider or TushareMarketProvider()
    active_provider.assert_ready()
    started_at = _now()
    target = date.fromisoformat(target_date)
    week_start = target - timedelta(days=target.weekday())
    week_end = week_start + timedelta(days=6)
    open_dates = active_provider.trade_calendar(
        week_start.isoformat(), week_end.isoformat()
    )
    if target_date not in open_dates:
        return {
            "trade_date": target_date,
            "status": "SKIPPED",
            "collection_state": "NON_TRADING_DAY",
            "provider": active_provider.source_name,
            "started_at": started_at,
            "finished_at": _now(),
        }

    members_raw = active_provider.stock_basic()
    daily = active_provider.market_daily_bars(target_date)
    factors = active_provider.market_adjustment_factors(target_date)
    metrics = active_provider.market_daily_metrics(target_date)
    indices = active_provider.index_daily_bars(target_date, MARKET_INDEX_SYMBOLS)
    prepared = _prepare_batch(
        target_date=target_date,
        members_raw=members_raw,
        daily=daily,
        factors=factors,
        metrics=metrics,
        indices=indices,
        minimum_stock_count=config.minimum_stock_count,
        minimum_coverage_ratio=config.minimum_coverage_ratio,
    )
    prepared["quality"]["week_open_dates"] = sorted(set(open_dates))
    prepared["quality"]["week_calendar_source"] = active_provider.source_name

    run_id = f"collect-{target_date}-{uuid4().hex[:12]}"
    fetched_at = _now()
    receipt = _persist_batch(
        config.market_database,
        target_date=target_date,
        run_id=run_id,
        provider=active_provider,
        prepared=prepared,
        started_at=started_at,
        fetched_at=fetched_at,
    )
    return {**receipt, "collection_state": "COLLECTED"}


def _prepare_batch(
    *,
    target_date: str,
    members_raw: list[dict[str, Any]],
    daily: Mapping[str, Mapping[str, Any]],
    factors: Mapping[str, float],
    metrics: Mapping[str, Mapping[str, Any]],
    indices: Mapping[str, Mapping[str, Any]],
    minimum_stock_count: int,
    minimum_coverage_ratio: float,
) -> dict[str, Any]:
    members: dict[str, dict[str, Any]] = {}
    for raw in members_raw:
        symbol = str(raw.get("ts_code") or "").strip().upper()
        if not symbol.endswith((".SH", ".SZ")):
            continue
        name = str(raw.get("name") or "").strip()
        members[symbol] = {
            "ts_code": symbol,
            "symbol": str(raw.get("symbol") or symbol.split(".", 1)[0]),
            "name": name,
            "industry": str(raw.get("industry") or ""),
            "market": str(raw.get("market") or ""),
            "list_date": _normalized_optional_date(raw.get("list_date")),
            "delist_date": "",
            "provider_list_status": "L",
            "listing_status_as_of": "listed",
            "is_st": _is_st_name(name),
            "st_status": "st" if _is_st_name(name) else "normal",
            "st_type": "",
            "suspend_reason": "",
            "is_suspended": symbol not in daily,
            "trading_status": "trading" if symbol in daily else "suspended",
        }

    daily_symbols = {symbol for symbol in daily if symbol in members}
    factor_symbols = {symbol for symbol in factors if symbol in members and factors[symbol] > 0}
    metric_symbols = {symbol for symbol in metrics if symbol in members}
    complete_symbols = daily_symbols & factor_symbols
    expected_count = len(members)
    stock_count = len(complete_symbols)
    coverage = stock_count / expected_count if expected_count else 0.0
    metrics_coverage = len(metric_symbols & complete_symbols) / stock_count if stock_count else 0.0
    errors: list[str] = []
    if expected_count < minimum_stock_count:
        errors.append(f"证券主数据仅 {expected_count} 只，最低要求 {minimum_stock_count}")
    if stock_count < minimum_stock_count:
        errors.append(f"完整日线和复权因子仅 {stock_count} 只，最低要求 {minimum_stock_count}")
    if coverage < minimum_coverage_ratio:
        errors.append(
            f"日线覆盖率 {coverage:.2%}，最低要求 {minimum_coverage_ratio:.2%}"
        )
    if metrics_coverage < minimum_coverage_ratio:
        errors.append(
            f"市值数据覆盖率 {metrics_coverage:.2%}，最低要求 {minimum_coverage_ratio:.2%}"
        )
    for symbol in metric_symbols:
        if str(metrics[symbol].get("trade_date") or "") != target_date:
            errors.append(f"市值数据日期无效: {symbol}")
            break
    missing_indices = sorted(set(MARKET_INDEX_SYMBOLS) - set(indices))
    if missing_indices:
        errors.append("指数日线缺失: " + ", ".join(missing_indices))
    for symbol in complete_symbols:
        if not _valid_daily_row(daily[symbol], target_date):
            errors.append(f"股票日线字段无效: {symbol}")
            break
    for symbol in MARKET_INDEX_SYMBOLS:
        row = indices.get(symbol)
        if row is not None and not _valid_daily_row(row, target_date):
            errors.append(f"指数日线字段无效: {symbol}")
    if errors:
        raise ValueError("采集质量门未通过；" + "；".join(errors))

    selected_daily = {symbol: dict(daily[symbol]) for symbol in sorted(complete_symbols)}
    selected_factors = {symbol: float(factors[symbol]) for symbol in sorted(complete_symbols)}
    selected_metrics = {
        symbol: dict(metrics[symbol])
        for symbol in sorted(complete_symbols & metric_symbols)
    }
    selected_indices = {
        symbol: dict(indices[symbol]) for symbol in MARKET_INDEX_SYMBOLS
    }
    quality = {
        "trade_date": target_date,
        "expected_universe_count": expected_count,
        "stock_count": stock_count,
        "coverage_ratio": round(coverage, 6),
        "metrics_count": len(selected_metrics),
        "metrics_coverage_ratio": round(metrics_coverage, 6),
        "index_symbols": list(MARKET_INDEX_SYMBOLS),
        "daily_without_factor_count": len(daily_symbols - factor_symbols),
        "factor_without_daily_count": len(factor_symbols - daily_symbols),
    }
    return {
        "members": [members[symbol] for symbol in sorted(members)],
        "daily": selected_daily,
        "factors": selected_factors,
        "metrics": selected_metrics,
        "indices": selected_indices,
        "quality": quality,
    }


def _persist_batch(
    path: Path,
    *,
    target_date: str,
    run_id: str,
    provider: MarketProvider,
    prepared: Mapping[str, Any],
    started_at: str,
    fetched_at: str,
) -> dict[str, Any]:
    daily = dict(prepared["daily"])
    factors = dict(prepared["factors"])
    metrics = dict(prepared["metrics"])
    indices = dict(prepared["indices"])
    members = list(prepared["members"])
    quality = dict(prepared["quality"])
    content_hash = _digest(
        {
            "trade_date": target_date,
            "daily": daily,
            "factors": factors,
            "metrics": metrics,
            "indices": indices,
            "members": members,
        }
    )
    batch_id = f"market-{run_id}"

    with sqlite3.connect(path, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.executescript(COLLECTION_SCHEMA_SQL)
        connection.execute("BEGIN IMMEDIATE")
        try:
            _ensure_quote_columns(connection)
            previous_date, previous_factors, previous_scales = _previous_scale_state(
                connection, target_date, tuple(sorted(daily))
            )
            rescaled = _write_qfq_rows(
                connection,
                target_date=target_date,
                run_id=run_id,
                batch_id=batch_id,
                fetched_at=fetched_at,
                source_version=provider.source_version,
                content_hash=content_hash,
                daily=daily,
                factors=factors,
                previous_factors=previous_factors,
                previous_scales=previous_scales,
            )
            _write_raw_rows(
                connection,
                target_date=target_date,
                run_id=run_id,
                batch_id=batch_id,
                fetched_at=fetched_at,
                source_version=provider.source_version,
                content_hash=content_hash,
                daily=daily,
            )
            _write_factors(
                connection,
                target_date=target_date,
                run_id=run_id,
                batch_id=batch_id,
                fetched_at=fetched_at,
                source_version=provider.source_version,
                factors=factors,
            )
            _write_metrics(
                connection,
                target_date=target_date,
                run_id=run_id,
                batch_id=batch_id,
                fetched_at=fetched_at,
                source_version=provider.source_version,
                content_hash=content_hash,
                metrics=metrics,
            )
            _write_security_history(
                connection,
                target_date=target_date,
                fetched_at=fetched_at,
                source_name=provider.source_name,
                source_version=provider.source_version,
                members=members,
            )
            _write_indices(
                connection,
                target_date=target_date,
                run_id=run_id,
                batch_id=batch_id,
                fetched_at=fetched_at,
                source_name=provider.source_name,
                source_version=provider.source_version,
                indices=indices,
            )
            quality["previous_factor_date"] = previous_date
            quality["rescaled_symbol_count"] = rescaled
            finished_at = _now()
            receipt = {
                "run_id": run_id,
                "trade_date": target_date,
                "provider": provider.source_name,
                "source_version": provider.source_version,
                "status": "SUCCESS",
                "stock_count": int(quality["stock_count"]),
                "expected_universe_count": int(quality["expected_universe_count"]),
                "coverage_ratio": float(quality["coverage_ratio"]),
                "metrics_count": int(quality["metrics_count"]),
                "index_count": len(indices),
                "rescaled_symbol_count": rescaled,
                "request_count": int(getattr(provider, "request_count", 0)),
                "content_hash": content_hash,
                "quality": quality,
                "started_at": started_at,
                "finished_at": finished_at,
            }
            connection.execute(
                """
                INSERT INTO watchlist_market_collection_receipt (
                    run_id, trade_date, provider, source_version, status,
                    stock_count, expected_universe_count, coverage_ratio,
                    metrics_count, index_count, rescaled_symbol_count,
                    request_count, content_hash, quality_json, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    target_date,
                    provider.source_name,
                    provider.source_version,
                    "SUCCESS",
                    receipt["stock_count"],
                    receipt["expected_universe_count"],
                    receipt["coverage_ratio"],
                    receipt["metrics_count"],
                    receipt["index_count"],
                    rescaled,
                    receipt["request_count"],
                    content_hash,
                    json.dumps(quality, ensure_ascii=False, sort_keys=True),
                    started_at,
                    finished_at,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return receipt


def _previous_scale_state(
    connection: sqlite3.Connection,
    target_date: str,
    symbols: tuple[str, ...],
) -> tuple[str, dict[str, float], dict[str, str]]:
    factors: dict[str, float] = {}
    scales: dict[str, str] = {}
    previous_dates: list[str] = []
    for start in range(0, len(symbols), 700):
        batch = symbols[start : start + 700]
        placeholders = ",".join("?" for _ in batch)
        rows = connection.execute(
            f"""
            SELECT factor.symbol, factor.trade_date, factor.adj_factor,
                   bars.price_scale_id
            FROM daily_adjustment_factors AS factor
            LEFT JOIN daily_bars AS bars
              ON bars.market=factor.market AND bars.symbol=factor.symbol
             AND bars.trade_date=factor.trade_date AND bars.adj_type='qfq'
            WHERE factor.market=? AND factor.symbol IN ({placeholders})
              AND factor.trade_date=(
                  SELECT MAX(prior.trade_date)
                  FROM daily_adjustment_factors AS prior
                  WHERE prior.market=factor.market AND prior.symbol=factor.symbol
                    AND prior.trade_date < ?
              )
            """,
            (MARKET, *batch, target_date),
        ).fetchall()
        for row in rows:
            symbol = str(row["symbol"]).upper()
            factors[symbol] = float(row["adj_factor"])
            scales[symbol] = str(row["price_scale_id"] or "")
            previous_dates.append(str(row["trade_date"] or ""))
    return max(previous_dates, default=""), factors, scales


def _write_qfq_rows(
    connection: sqlite3.Connection,
    *,
    target_date: str,
    run_id: str,
    batch_id: str,
    fetched_at: str,
    source_version: str,
    content_hash: str,
    daily: Mapping[str, Mapping[str, Any]],
    factors: Mapping[str, float],
    previous_factors: Mapping[str, float],
    previous_scales: Mapping[str, str],
) -> int:
    rows: list[tuple[Any, ...]] = []
    rescaled = 0
    for symbol in sorted(daily):
        factor = float(factors[symbol])
        scale_id = _price_scale_id(symbol, factor, source_version)
        previous = previous_factors.get(symbol)
        if previous and abs(previous - factor) > 1e-12:
            history = _rederived_qfq_history(
                connection,
                symbol=symbol,
                target_date=target_date,
                previous_factor=previous,
                latest_factor=factor,
                source_version=source_version,
                batch_id=batch_id,
                run_id=run_id,
                content_hash=content_hash,
                fetched_at=fetched_at,
                price_scale_id=scale_id,
            )
            _upsert_bars(connection, history)
            if history:
                rescaled += 1
        elif previous_scales.get(symbol):
            scale_id = previous_scales[symbol]
        row = daily[symbol]
        rows.append(
            _bar_tuple(
                symbol,
                target_date,
                row,
                adj_type="qfq",
                data_source="tushare_adj_factor_qfq",
                source_version=source_version,
                batch_id=f"qfq-{batch_id}",
                run_id=run_id,
                input_hash=content_hash,
                fetched_at=fetched_at,
                price_scale_id=scale_id,
            )
        )
    _upsert_bars(connection, rows)
    return rescaled


def _rederived_qfq_history(
    connection: sqlite3.Connection,
    *,
    symbol: str,
    target_date: str,
    previous_factor: float,
    latest_factor: float,
    source_version: str,
    batch_id: str,
    run_id: str,
    content_hash: str,
    fetched_at: str,
    price_scale_id: str,
) -> list[tuple[Any, ...]]:
    source_rows = connection.execute(
        """
        SELECT qfq.trade_date,
               qfq.open AS qfq_open, qfq.high AS qfq_high,
               qfq.low AS qfq_low, qfq.close AS qfq_close,
               qfq.volume, qfq.amount,
               raw.open AS raw_open, raw.high AS raw_high,
               raw.low AS raw_low, raw.close AS raw_close,
               factor.adj_factor
        FROM daily_bars AS qfq
        LEFT JOIN daily_bars AS raw
          ON raw.market=qfq.market AND raw.symbol=qfq.symbol
         AND raw.trade_date=qfq.trade_date AND raw.adj_type='raw'
        LEFT JOIN daily_adjustment_factors AS factor
          ON factor.market=qfq.market AND factor.symbol=qfq.symbol
         AND factor.trade_date=qfq.trade_date
        WHERE qfq.market=? AND qfq.symbol=? AND qfq.trade_date < ?
          AND qfq.adj_type='qfq'
        ORDER BY qfq.trade_date
        """,
        (MARKET, symbol, target_date),
    ).fetchall()
    result: list[tuple[Any, ...]] = []
    for row in source_rows:
        adjusted: dict[str, float | None]
        has_exact_inputs = row["adj_factor"] is not None and all(
            row[f"raw_{key}"] is not None for key in ("open", "high", "low", "close")
        )
        if has_exact_inputs:
            ratio = float(row["adj_factor"]) / latest_factor
            adjusted = {
                key: round(float(row[f"raw_{key}"]) * ratio, 6)
                for key in ("open", "high", "low", "close")
            }
        else:
            ratio = previous_factor / latest_factor
            adjusted = {
                key: (
                    round(float(row[f"qfq_{key}"]) * ratio, 6)
                    if row[f"qfq_{key}"] is not None
                    else None
                )
                for key in ("open", "high", "low", "close")
            }
        adjusted["volume"] = row["volume"]
        adjusted["amount"] = row["amount"]
        result.append(
            _bar_tuple(
                symbol,
                str(row["trade_date"]),
                adjusted,
                adj_type="qfq",
                data_source="tushare_adj_factor_qfq",
                source_version=source_version,
                batch_id=f"qfq-{batch_id}",
                run_id=run_id,
                data_as_of_date=target_date,
                input_hash=content_hash,
                fetched_at=fetched_at,
                price_scale_id=price_scale_id,
            )
        )
    return result


def _write_raw_rows(
    connection: sqlite3.Connection,
    *,
    target_date: str,
    run_id: str,
    batch_id: str,
    fetched_at: str,
    source_version: str,
    content_hash: str,
    daily: Mapping[str, Mapping[str, Any]],
) -> None:
    rows = [
        _bar_tuple(
            symbol,
            target_date,
            row,
            adj_type="raw",
            data_source="tushare_daily",
            source_version=source_version,
            batch_id=batch_id,
            run_id=run_id,
            input_hash=content_hash,
            fetched_at=fetched_at,
            price_scale_id="",
        )
        for symbol, row in sorted(daily.items())
    ]
    _upsert_bars(connection, rows)


def _bar_tuple(
    symbol: str,
    target_date: str,
    row: Mapping[str, Any],
    *,
    adj_type: str,
    data_source: str,
    source_version: str,
    batch_id: str,
    run_id: str,
    data_as_of_date: str | None = None,
    input_hash: str,
    fetched_at: str,
    price_scale_id: str,
) -> tuple[Any, ...]:
    return (
        MARKET,
        symbol,
        target_date,
        _number(row.get("open")),
        _number(row.get("high")),
        _number(row.get("low")),
        _number(row.get("close")),
        _number(row.get("vol") if row.get("vol") is not None else row.get("volume")),
        _number(row.get("amount")),
        _number(row.get("pre_close")),
        _number(row.get("pct_chg")),
        adj_type,
        data_source,
        source_version,
        batch_id,
        run_id,
        data_as_of_date or target_date,
        input_hash,
        fetched_at,
        price_scale_id,
    )


def _upsert_bars(connection: sqlite3.Connection, rows: list[tuple[Any, ...]]) -> None:
    connection.executemany(
        """
        INSERT INTO daily_bars (
            market, symbol, trade_date, open, high, low, close, volume, amount,
            pre_close, pct_chg,
            adj_type, data_source, source_version, batch_id, run_id,
            data_as_of_date, input_hash, fetched_at, price_scale_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(market, symbol, trade_date, adj_type) DO UPDATE SET
            open=excluded.open, high=excluded.high, low=excluded.low,
            close=excluded.close, volume=excluded.volume, amount=excluded.amount,
            pre_close=excluded.pre_close, pct_chg=excluded.pct_chg,
            data_source=excluded.data_source, source_version=excluded.source_version,
            batch_id=excluded.batch_id, run_id=excluded.run_id,
            data_as_of_date=excluded.data_as_of_date, input_hash=excluded.input_hash,
            fetched_at=excluded.fetched_at, price_scale_id=excluded.price_scale_id,
            updated_at=CURRENT_TIMESTAMP
        """,
        rows,
    )


def _ensure_quote_columns(connection: sqlite3.Connection) -> None:
    """Keep raw daily quote fields available without changing historical facts."""

    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(daily_bars)").fetchall()
    }
    for name in ("pre_close", "pct_chg"):
        if name not in columns:
            connection.execute(f"ALTER TABLE daily_bars ADD COLUMN {name} REAL")


def _write_factors(
    connection: sqlite3.Connection,
    *,
    target_date: str,
    run_id: str,
    batch_id: str,
    fetched_at: str,
    source_version: str,
    factors: Mapping[str, float],
) -> None:
    connection.executemany(
        """
        INSERT INTO daily_adjustment_factors (
            market, symbol, trade_date, adj_factor, data_source,
            factor_as_of_date, source_version, batch_id, run_id, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(market, symbol, trade_date) DO UPDATE SET
            adj_factor=excluded.adj_factor, data_source=excluded.data_source,
            factor_as_of_date=excluded.factor_as_of_date,
            source_version=excluded.source_version, batch_id=excluded.batch_id,
            run_id=excluded.run_id, fetched_at=excluded.fetched_at,
            updated_at=CURRENT_TIMESTAMP
        """,
        [
            (
                MARKET,
                symbol,
                target_date,
                factor,
                "tushare_adj_factor",
                target_date,
                source_version,
                batch_id,
                run_id,
                fetched_at,
            )
            for symbol, factor in sorted(factors.items())
        ],
    )


def _write_metrics(
    connection: sqlite3.Connection,
    *,
    target_date: str,
    run_id: str,
    batch_id: str,
    fetched_at: str,
    source_version: str,
    content_hash: str,
    metrics: Mapping[str, Mapping[str, Any]],
) -> None:
    connection.executemany(
        """
        INSERT INTO daily_metrics (
            market, symbol, trade_date, turnover_rate, total_mv, circ_mv,
            data_source, source_version, batch_id, run_id, data_as_of_date,
            input_hash, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(market, symbol, trade_date) DO UPDATE SET
            turnover_rate=excluded.turnover_rate, total_mv=excluded.total_mv,
            circ_mv=excluded.circ_mv, data_source=excluded.data_source,
            source_version=excluded.source_version, batch_id=excluded.batch_id,
            run_id=excluded.run_id, data_as_of_date=excluded.data_as_of_date,
            input_hash=excluded.input_hash, fetched_at=excluded.fetched_at,
            updated_at=CURRENT_TIMESTAMP
        """,
        [
            (
                MARKET,
                symbol,
                target_date,
                _number(row.get("turnover_rate")),
                _number(row.get("total_mv")),
                _number(row.get("circ_mv")),
                "tushare_daily_basic",
                source_version,
                batch_id,
                run_id,
                target_date,
                content_hash,
                fetched_at,
            )
            for symbol, row in sorted(metrics.items())
        ],
    )


def _write_security_history(
    connection: sqlite3.Connection,
    *,
    target_date: str,
    fetched_at: str,
    source_name: str,
    source_version: str,
    members: list[dict[str, Any]],
) -> None:
    columns = (
        "code", "name", "industry", "board", "list_date", "delist_date",
        "provider_list_status", "listing_status_as_of", "is_st", "st_status",
        "st_type", "is_suspended", "suspend_reason", "trading_status",
    )
    latest_rows = connection.execute(
        """
        SELECT history.* FROM security_master_history AS history
        JOIN (
            SELECT symbol, MAX(valid_from) AS valid_from
            FROM security_master_history GROUP BY symbol
        ) AS latest
          ON latest.symbol = history.symbol AND latest.valid_from = history.valid_from
        """
    ).fetchall()
    latest = {
        str(row["symbol"]): tuple(row[column] for column in columns)
        for row in latest_rows
    }
    inserts: list[tuple[Any, ...]] = []
    for member in sorted(members, key=lambda row: str(row.get("ts_code") or "")):
        symbol = str(member.get("ts_code") or "").upper()
        state: tuple[Any, ...] = (
            str(member.get("symbol") or ""), str(member.get("name") or ""),
            str(member.get("industry") or ""), str(member.get("market") or ""),
            str(member.get("list_date") or ""), str(member.get("delist_date") or ""),
            str(member.get("provider_list_status") or ""),
            str(member.get("listing_status_as_of") or ""), int(bool(member.get("is_st"))),
            str(member.get("st_status") or ""), str(member.get("st_type") or ""),
            int(bool(member.get("is_suspended"))), str(member.get("suspend_reason") or ""),
            str(member.get("trading_status") or ""),
        )
        if latest.get(symbol) == state:
            continue
        digest = _digest({"symbol": symbol, "valid_from": target_date, "state": state})
        inserts.append((target_date, symbol, *state, source_name, source_version, digest, fetched_at))
        latest[symbol] = state
    connection.executemany(
        """
        INSERT INTO security_master_history (
            valid_from, symbol, code, name, industry, board, list_date, delist_date,
            provider_list_status, listing_status_as_of, is_st, st_status, st_type,
            is_suspended, suspend_reason, trading_status, provider, source_version,
            source_digest, fetched_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        inserts,
    )


def _write_indices(
    connection: sqlite3.Connection,
    *,
    target_date: str,
    run_id: str,
    batch_id: str,
    fetched_at: str,
    source_name: str,
    source_version: str,
    indices: Mapping[str, Mapping[str, Any]],
) -> None:
    connection.executemany(
        """
        INSERT INTO market_index_daily_bars (
            batch_id, run_id, market, index_symbol, trade_date,
            open, high, low, close, volume, amount, provider,
            source_version, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                f"index-{batch_id}",
                run_id,
                MARKET,
                symbol,
                target_date,
                _number(row.get("open")),
                _number(row.get("high")),
                _number(row.get("low")),
                _number(row.get("close")),
                _number(row.get("vol") if row.get("vol") is not None else row.get("volume")),
                _number(row.get("amount")),
                source_name,
                source_version,
                fetched_at,
            )
            for symbol, row in sorted(indices.items())
        ],
    )


def _existing_success(path: Path, target_date: str) -> dict[str, Any] | None:
    with sqlite3.connect(path) as connection:
        table = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='watchlist_market_collection_receipt'
            """
        ).fetchone()
        if not table:
            return None
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT * FROM watchlist_market_collection_receipt
            WHERE trade_date=? AND status='SUCCESS' LIMIT 1
            """,
            (target_date,),
        ).fetchone()
    return _receipt_dict(row) if row else None


def _receipt_dict(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    value["quality"] = json.loads(str(value.pop("quality_json") or "{}"))
    return value


def _assert_required_tables(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    missing = REQUIRED_TABLES - {str(row[0]) for row in rows}
    if missing:
        raise ValueError("行情库缺少必要数据表: " + ", ".join(sorted(missing)))


def _latest_qfq_date(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            """
            SELECT MAX(trade_date) FROM daily_bars
            WHERE market=? AND adj_type='qfq'
            """,
            (MARKET,),
        ).fetchone()
    return str(row[0] or "") if row else ""


def _valid_daily_row(row: Mapping[str, Any], expected_date: str) -> bool:
    if str(row.get("trade_date") or "") != expected_date:
        return False
    values = [_number(row.get(key)) for key in ("open", "high", "low", "close")]
    if not all(value is not None and value > 0 for value in values):
        return False
    open_price, high, low, close = (float(value) for value in values if value is not None)
    return high >= max(open_price, close, low) and low <= min(open_price, close, high)


def _price_scale_id(symbol: str, latest_factor: float, source_version: str) -> str:
    return _digest(
        {
            "market": MARKET,
            "symbol": symbol,
            "latest_factor": latest_factor,
            "derivation_version": QFQ_DERIVATION_VERSION,
            "source_version": source_version,
        }
    )


def _digest(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _is_st_name(name: str) -> bool:
    normalized = name.strip().upper().replace(" ", "")
    return normalized.startswith(("ST", "*ST", "S*ST", "SST"))


def _normalized_optional_date(value: Any) -> str:
    text = str(value or "").strip().replace("-", "")
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return ""


def _validate_date(value: str) -> None:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid trade date: {value}") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"invalid trade date: {value}")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def configured_minimum_stock_count() -> int:
    try:
        return max(1, int(os.environ.get("MASTERSTOCK_MINIMUM_STOCK_COUNT", "4500")))
    except ValueError:
        return 4500
