"""Point-in-time reference facts used by reconstructed watchlists.

Price data alone cannot safely answer a historical page's company name, ST
state, or SW2021 industry.  This module keeps those facts in the market vault
with their provider-effective dates and never substitutes current metadata.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping
from uuid import uuid4

from .market_provider import TushareMarketProvider

REFERENCE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS security_name_change_fact (
    symbol TEXT NOT NULL, name TEXT NOT NULL, valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL, announced_on TEXT NOT NULL, change_reason TEXT NOT NULL,
    provider TEXT NOT NULL, source_version TEXT NOT NULL, source_digest TEXT NOT NULL,
    origin TEXT NOT NULL CHECK (origin = 'RECONSTRUCTED'),
    PRIMARY KEY (symbol, name, valid_from, valid_to, source_digest)
);
CREATE TABLE IF NOT EXISTS security_st_daily_fact (
    trade_date TEXT NOT NULL, symbol TEXT NOT NULL, name TEXT NOT NULL,
    st_type TEXT NOT NULL, st_type_name TEXT NOT NULL, provider TEXT NOT NULL,
    source_version TEXT NOT NULL, source_digest TEXT NOT NULL,
    origin TEXT NOT NULL CHECK (origin = 'RECONSTRUCTED'),
    PRIMARY KEY (trade_date, symbol, source_digest)
);
CREATE TABLE IF NOT EXISTS sw2021_l3_membership_fact (
    symbol TEXT NOT NULL, industry_code TEXT NOT NULL, industry_name TEXT NOT NULL,
    parent_industry_code TEXT NOT NULL, valid_from TEXT NOT NULL, valid_to TEXT NOT NULL,
    provider TEXT NOT NULL, source_version TEXT NOT NULL, source_digest TEXT NOT NULL,
    origin TEXT NOT NULL CHECK (origin = 'RECONSTRUCTED'),
    PRIMARY KEY (symbol, industry_code, valid_from, valid_to, source_digest)
);
CREATE TABLE IF NOT EXISTS reference_data_run_receipt (
    run_id TEXT PRIMARY KEY, from_date TEXT NOT NULL, to_date TEXT NOT NULL,
    provider TEXT NOT NULL, source_version TEXT NOT NULL, name_change_count INTEGER NOT NULL,
    st_fact_count INTEGER NOT NULL, membership_count INTEGER NOT NULL,
    request_count INTEGER NOT NULL, content_hash TEXT NOT NULL, status TEXT NOT NULL,
    started_at TEXT NOT NULL, finished_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reference_name_asof
ON security_name_change_fact(symbol, valid_from, valid_to);
CREATE INDEX IF NOT EXISTS idx_reference_st_asof
ON security_st_daily_fact(trade_date, symbol);
CREATE INDEX IF NOT EXISTS idx_reference_sw_asof
ON sw2021_l3_membership_fact(symbol, valid_from, valid_to);
"""


@dataclass(frozen=True)
class ReferenceBackfillConfig:
    market_database: Path
    from_date: str
    to_date: str
    include_names_st: bool = True


def collect_reference_data(
    config: ReferenceBackfillConfig,
    provider: TushareMarketProvider | None = None,
) -> dict[str, Any]:
    """Collect all source-backed identity, ST and SW intervals for a range."""
    _validate_date(config.from_date)
    _validate_date(config.to_date)
    if config.from_date > config.to_date:
        raise ValueError("from_date must not be later than to_date")
    if not config.market_database.is_file():
        raise FileNotFoundError(f"market database does not exist: {config.market_database}")
    trading_dates = _trading_dates(config.market_database, config.from_date, config.to_date)
    if not trading_dates:
        raise ValueError("market database has no trading dates in the requested range")
    active = provider or TushareMarketProvider()
    active.assert_ready()
    started_at = _now()
    names_raw = active.stock_name_changes() if config.include_names_st else []
    classifications = active.sw_l3_classifications()
    memberships_raw: list[dict[str, Any]] = []
    for row in classifications:
        code = str(row.get("index_code") or "").strip().upper()
        if code:
            memberships_raw.extend(active.sw_l3_members(code))
    st_raw: list[tuple[str, dict[str, Any]]] = []
    if config.include_names_st:
        for trade_date in trading_dates:
            for row in active.stock_st(trade_date):
                st_raw.append((trade_date, row))

    names = _name_rows(names_raw, active)
    st_rows = _st_rows(st_raw, active)
    memberships = _membership_rows(memberships_raw, active)
    content_hash = _digest({"names": names, "st": st_rows, "memberships": memberships})
    run_id = f"reference-{config.to_date}-{uuid4().hex[:12]}"
    with sqlite3.connect(config.market_database) as connection:
        connection.executescript(REFERENCE_SCHEMA_SQL)
        connection.execute("BEGIN IMMEDIATE")
        connection.executemany(
            "INSERT OR IGNORE INTO security_name_change_fact VALUES (?,?,?,?,?,?,?,?,?,?)",
            [tuple(row[key] for key in ("symbol", "name", "valid_from", "valid_to", "announced_on", "change_reason", "provider", "source_version", "source_digest", "origin")) for row in names],
        )
        connection.executemany(
            "INSERT OR IGNORE INTO security_st_daily_fact VALUES (?,?,?,?,?,?,?,?,?)",
            [tuple(row[key] for key in ("trade_date", "symbol", "name", "st_type", "st_type_name", "provider", "source_version", "source_digest", "origin")) for row in st_rows],
        )
        connection.executemany(
            "INSERT OR IGNORE INTO sw2021_l3_membership_fact VALUES (?,?,?,?,?,?,?,?,?,?)",
            [tuple(row[key] for key in ("symbol", "industry_code", "industry_name", "parent_industry_code", "valid_from", "valid_to", "provider", "source_version", "source_digest", "origin")) for row in memberships],
        )
        connection.execute(
            "INSERT INTO reference_data_run_receipt VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, config.from_date, config.to_date, active.source_name, active.source_version,
             len(names), len(st_rows), len(memberships), active.request_count, content_hash,
             "SUCCESS", started_at, _now()),
        )
    return {"run_id": run_id, "trading_days": len(trading_dates), "name_change_count": len(names), "st_fact_count": len(st_rows), "membership_count": len(memberships), "request_count": active.request_count, "content_hash": content_hash}


def _name_rows(rows: Iterable[Mapping[str, Any]], provider: TushareMarketProvider) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for raw in rows:
        symbol = str(raw.get("ts_code") or "").upper()
        name = str(raw.get("name") or "").strip()
        start = _date(raw.get("start_date"))
        if not symbol.endswith((".SH", ".SZ")) or not name or not start:
            continue
        item = {"symbol": symbol, "name": name, "valid_from": start, "valid_to": _date(raw.get("end_date")), "announced_on": _date(raw.get("ann_date")), "change_reason": str(raw.get("change_reason") or ""), "provider": provider.source_name, "source_version": provider.source_version, "origin": "RECONSTRUCTED"}
        item["source_digest"] = _digest(item)
        result.append(item)
    return result


def _st_rows(rows: Iterable[tuple[str, Mapping[str, Any]]], provider: TushareMarketProvider) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for requested_date, raw in rows:
        symbol = str(raw.get("ts_code") or "").upper()
        trade_date = _date(raw.get("trade_date"))
        if trade_date != requested_date or not symbol.endswith((".SH", ".SZ")):
            continue
        item = {"trade_date": trade_date, "symbol": symbol, "name": str(raw.get("name") or ""), "st_type": str(raw.get("type") or ""), "st_type_name": str(raw.get("type_name") or ""), "provider": provider.source_name, "source_version": provider.source_version, "origin": "RECONSTRUCTED"}
        item["source_digest"] = _digest(item)
        result.append(item)
    return result


def _membership_rows(rows: Iterable[Mapping[str, Any]], provider: TushareMarketProvider) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for raw in rows:
        symbol = str(raw.get("ts_code") or "").upper()
        code = str(raw.get("l3_code") or "").upper()
        start = _date(raw.get("in_date"))
        if not symbol.endswith((".SH", ".SZ")) or not code or not start:
            continue
        item = {"symbol": symbol, "industry_code": code, "industry_name": str(raw.get("l3_name") or ""), "parent_industry_code": str(raw.get("l2_code") or ""), "valid_from": start, "valid_to": _date(raw.get("out_date")), "provider": provider.source_name, "source_version": provider.source_version, "origin": "RECONSTRUCTED"}
        item["source_digest"] = _digest(item)
        result.append(item)
    return result


def _trading_dates(path: Path, start: str, end: str) -> list[str]:
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as connection:
        rows = connection.execute("SELECT DISTINCT trade_date FROM daily_bars WHERE market='ashare' AND adj_type='qfq' AND trade_date BETWEEN ? AND ? ORDER BY trade_date", (start, end)).fetchall()
    return [str(row[0]) for row in rows]


def _date(value: Any) -> str:
    compact = str(value or "").strip().replace("-", "")
    return f"{compact[:4]}-{compact[4:6]}-{compact[6:]}" if len(compact) == 8 and compact.isdigit() else ""


def _validate_date(value: str) -> None:
    datetime.strptime(value, "%Y-%m-%d")


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
