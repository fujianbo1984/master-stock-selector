"""Materialize source-backed reference intervals into compact temporal history."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .repository import MarketDataReader, WatchlistRepository


@dataclass(frozen=True)
class ReferenceMaterializationConfig:
    market_database: Path
    watchlist_database: Path
    from_date: str
    to_date: str


def materialize_reference_history(config: ReferenceMaterializationConfig) -> dict[str, Any]:
    """Append reconstructed identity changes and SW intervals to a candidate database."""
    reader = MarketDataReader(config.market_database)
    dates = [date for date in reader.trading_dates(config.to_date) if config.from_date <= date <= config.to_date]
    if not dates:
        raise ValueError("no local trading dates in reference materialization range")
    members = reader.security_members(config.to_date)
    if not members:
        raise ValueError("no security master snapshot available for reference materialization")
    names, st_by_date, industries = _load_reference(config.market_database)
    repository = WatchlistRepository(config.watchlist_database)
    total_memberships = 0
    total_observations = 0
    total_identity_changes = 0
    for as_of_date in dates:
        identities = _identities(as_of_date, members, names, st_by_date)
        dimensions, memberships = _memberships(as_of_date, industries)
        payload = repository.import_reference_history_date({
            "snapshot_date": as_of_date,
            "identities": identities,
            "dimensions": dimensions,
            "memberships": memberships,
        })
        total_memberships += int(payload["membership_count"])
        total_observations += int(payload["observation_count"])
        total_identity_changes += int(payload["identity_change_count"])
    return {
        "evaluated_date_count": len(dates),
        "identity_evaluation_count": len(dates) * len(members),
        "identity_change_count": total_identity_changes,
        "membership_evaluation_count": total_memberships,
        "observation_count": total_observations,
    }


def materialize_reference_snapshots(config: ReferenceMaterializationConfig) -> dict[str, Any]:
    """Compatibility alias for the former daily-snapshot command implementation."""
    return materialize_reference_history(config)


def _load_reference(path: Path) -> tuple[dict[str, list[dict[str, str]]], dict[str, set[str]], dict[str, list[dict[str, str]]]]:
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as connection:
        tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"security_name_change_fact", "security_st_daily_fact", "sw2021_l3_membership_fact"}
        if not required.issubset(tables):
            raise ValueError("reference facts missing; run reference-backfill first")
        name_rows = [dict(zip(("symbol", "name", "valid_from", "valid_to"), row)) for row in connection.execute("SELECT symbol,name,valid_from,valid_to FROM security_name_change_fact ORDER BY symbol,valid_from")]
        st_rows = connection.execute("SELECT trade_date,symbol FROM security_st_daily_fact").fetchall()
        industry_rows = [dict(zip(("symbol", "industry_code", "industry_name", "parent_industry_code", "valid_from", "valid_to", "source_digest"), row)) for row in connection.execute("SELECT symbol,industry_code,industry_name,parent_industry_code,valid_from,valid_to,source_digest FROM sw2021_l3_membership_fact ORDER BY symbol,valid_from")]
    names: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in name_rows:
        names[str(row["symbol"])].append(row)
    st_by_date: dict[str, set[str]] = defaultdict(set)
    for trade_date, symbol in st_rows:
        st_by_date[str(trade_date)].add(str(symbol))
    industries: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in industry_rows:
        industries[str(row["symbol"])].append(row)
    return names, st_by_date, industries


def _identities(as_of_date: str, members: dict[str, dict[str, Any]], names: dict[str, list[dict[str, str]]], st_by_date: dict[str, set[str]]) -> list[dict[str, Any]]:
    st_symbols = st_by_date.get(as_of_date, set())
    result: list[dict[str, Any]] = []
    for symbol, member in sorted(members.items()):
        intervals = [row for row in names.get(symbol, []) if row["valid_from"] <= as_of_date and (not row["valid_to"] or as_of_date <= row["valid_to"])]
        chosen = max(intervals, key=lambda row: row["valid_from"], default={})
        list_date = str(member.get("list_date") or "")
        listed = bool(list_date and list_date <= as_of_date)
        result.append({
            "snapshot_date": as_of_date, "symbol": symbol,
            "name": str(chosen.get("name") or ""), "industry": "",
            "list_date": list_date, "is_st": symbol in st_symbols,
            "is_suspended": False, "listing_status": "listed" if listed else "not_listed",
            "trading_status": "historical_reference", "origin": "RECONSTRUCTED",
        })
    return result


def _memberships(as_of_date: str, industries: dict[str, list[dict[str, str]]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    active: list[dict[str, str]] = []
    for symbol, rows in industries.items():
        candidates = [row for row in rows if row["valid_from"] <= as_of_date and (not row["valid_to"] or as_of_date <= row["valid_to"])]
        if len({str(row["industry_code"]) for row in candidates}) == 1 and candidates:
            active.append(candidates[0])
    codes = {row["industry_code"]: row for row in active}
    dimensions = [{"snapshot_date": as_of_date, "taxonomy": "SW2021", "industry_level": "L3", "industry_code": code, "industry_name": row["industry_name"], "parent_industry_code": row["parent_industry_code"], "source": "tushare:index_member_all", "source_version": "tushare-pro-api-v1", "source_digest": _digest(row), "origin": "RECONSTRUCTED"} for code, row in sorted(codes.items())]
    memberships = [{"snapshot_date": as_of_date, "symbol": row["symbol"], "taxonomy": "SW2021", "industry_level": "L3", "industry_code": row["industry_code"], "industry_name": row["industry_name"], "valid_from": row["valid_from"], "valid_to": row["valid_to"], "assignment_state": "VERIFIED", "source": "tushare:index_member_all", "source_digest": row["source_digest"], "origin": "RECONSTRUCTED"} for row in active]
    return dimensions, memberships


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
