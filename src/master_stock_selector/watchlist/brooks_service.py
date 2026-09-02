from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .brooks import (
    BROOKS_POLICY_VERSION,
    SecurityState,
    build_brooks_daily_facts,
    build_brooks_impulses,
    impulse_row,
    normalized_brooks_bars,
)
from .repository import MarketDataReader, WatchlistRepository


@dataclass(frozen=True)
class BrooksRunConfig:
    market_database: Path
    watchlist_database: Path
    as_of_date: str = ""
    from_date: str = ""
    origin: str = "OBSERVED"


def run_brooks_watchlist(config: BrooksRunConfig) -> dict[str, Any]:
    origin = config.origin.upper()
    if origin not in {"RECONSTRUCTED", "OBSERVED"}:
        raise ValueError("origin must be RECONSTRUCTED or OBSERVED")
    reader = MarketDataReader(config.market_database)
    latest_market_date = reader.latest_market_date()
    as_of_date = config.as_of_date or latest_market_date
    if not as_of_date:
        raise ValueError("market database has no A-share daily bars")
    if as_of_date > latest_market_date:
        raise ValueError(
            f"as_of_date {as_of_date} is later than latest local market date {latest_market_date}"
        )
    if origin == "OBSERVED" and as_of_date != date.today().isoformat():
        raise ValueError(
            "OBSERVED is allowed only when as_of_date equals the actual run date; "
            "use RECONSTRUCTED for historical local data"
        )

    trading_dates = reader.trading_dates(as_of_date)
    if as_of_date not in trading_dates:
        raise ValueError(f"as_of_date is not present in local A-share bars: {as_of_date}")
    from_date = config.from_date or as_of_date
    if from_date > as_of_date:
        raise ValueError("from_date must not be later than as_of_date")
    evaluation_dates = [value for value in trading_dates if from_date <= value <= as_of_date]
    if not evaluation_dates:
        raise ValueError("the requested range contains no local trading dates")

    repository = WatchlistRepository(config.watchlist_database)
    if not repository.brooks_schema_ready():
        raise ValueError(
            "Brooks 表尚未初始化；请先运行 watchlist --brooks-schema --apply"
        )
    latest_run_date = repository.latest_brooks_run_date(BROOKS_POLICY_VERSION)
    if latest_run_date and evaluation_dates[0] <= latest_run_date:
        raise ValueError(
            "Brooks facts are append-only and already exist through "
            f"{latest_run_date}; append a later date or reconstruct into a new database"
        )

    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    source_summary = reader.source_summary(as_of_date)
    source_digest = _digest(source_summary)
    histories = reader.security_histories(as_of_date)
    event_rows: list[dict[str, Any]] = []
    fact_rows: list[dict[str, Any]] = []
    symbols_scanned = 0
    for symbol, raw_rows in reader.iter_brooks_bars(as_of_date):
        raw_history = histories.get(symbol)
        if not raw_history:
            continue
        symbols_scanned += 1
        bars = normalized_brooks_bars(raw_rows)
        history = [_security_state(row) for row in raw_history]
        events = build_brooks_impulses(symbol, bars, history)
        event_rows.extend(impulse_row(event, origin=origin) for event in events)
        for event in events:
            fact_rows.extend(
                build_brooks_daily_facts(
                    event,
                    bars,
                    history,
                    evaluation_dates,
                    origin=origin,
                )
            )

    state_counts: dict[str, int] = {}
    for row in fact_rows:
        state = str(row["state"])
        state_counts[state] = state_counts.get(state, 0) + 1
    counts = {
        "evaluation_dates": len(evaluation_dates),
        "symbols_scanned": symbols_scanned,
        "impulse_events_seen": len(event_rows),
        "daily_facts": len(fact_rows),
        "states": state_counts,
    }
    finished_at = datetime.now().astimezone().isoformat(timespec="seconds")
    receipt = {
        "run_id": f"brooks-{as_of_date}-{uuid4().hex[:12]}",
        "as_of_date": as_of_date,
        "from_date": evaluation_dates[0],
        "origin": origin,
        "policy_version": BROOKS_POLICY_VERSION,
        "market_database": str(config.market_database.resolve()),
        "source_digest": source_digest,
        "counts": counts,
        "status": "SUCCESS",
        "started_at": started_at,
        "finished_at": finished_at,
    }
    repository.persist_brooks_run(events=event_rows, facts=fact_rows, receipt=receipt)
    return {
        **receipt,
        "watchlist_database": str(config.watchlist_database.resolve()),
        "source_summary": source_summary,
    }


def _security_state(row: dict[str, Any]) -> SecurityState:
    return SecurityState(
        valid_from=str(row.get("valid_from") or row.get("list_date") or ""),
        name=str(row.get("name") or ""),
        industry=str(row.get("industry") or ""),
        list_date=str(row.get("list_date") or ""),
        listing_status=str(row.get("listing_status_as_of") or "listed"),
        is_st=bool(row.get("is_st")),
        is_suspended=bool(row.get("is_suspended")),
        trading_status=str(row.get("trading_status") or "trading"),
    )


def _digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
