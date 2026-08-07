from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from .methods import (
    MINERVINI_INDEX_STAGE2_POLICY_VERSION,
    MINERVINI_POLICY_VERSION,
    RESULT_PASS,
    RESULT_TRANSITION,
    RESULT_UNKNOWN,
    WEINSTEIN_POLICY_VERSION,
    aggregate_completed_weeks,
    completed_week_end_map,
    finish_minervini_profile,
    minervini_base_profiles,
    normalized_daily_bars,
    percentile_ranks,
    weinstein_profiles_for_dates,
    weinstein_stage_series,
)
from .repository import MarketDataReader, WatchlistRepository

INDEX_UNIVERSE = (
    ("000300.SH", "沪深300"),
    ("000852.SH", "中证1000"),
    ("399006.SZ", "创业板指"),
    ("000688.SH", "科创50"),
)


@dataclass(frozen=True)
class WatchlistRunConfig:
    market_database: Path
    watchlist_database: Path
    as_of_date: str = ""
    from_date: str = ""
    origin: str = "OBSERVED"


def run_watchlist(config: WatchlistRunConfig) -> dict[str, Any]:
    origin = config.origin.upper()
    if origin not in {"RECONSTRUCTED", "OBSERVED"}:
        raise ValueError("origin must be RECONSTRUCTED or OBSERVED")
    reader = MarketDataReader(config.market_database)
    latest_date = reader.latest_market_date()
    as_of_date = config.as_of_date or latest_date
    if not as_of_date:
        raise ValueError("market database has no qfq A-share daily bars")
    if as_of_date > latest_date:
        raise ValueError(
            f"as_of_date {as_of_date} is later than latest local market date {latest_date}"
        )
    if origin == "OBSERVED" and as_of_date != date.today().isoformat():
        raise ValueError(
            "OBSERVED is allowed only when as_of_date equals the actual run date; "
            "use RECONSTRUCTED for historical local data"
        )

    trading_dates = reader.trading_dates(as_of_date)
    if as_of_date not in trading_dates:
        raise ValueError(f"as_of_date is not present in the local A-share bars: {as_of_date}")
    from_date = config.from_date or as_of_date
    if from_date > as_of_date:
        raise ValueError("from_date must not be later than as_of_date")
    evaluation_dates = [value for value in trading_dates if from_date <= value <= as_of_date]
    if not evaluation_dates:
        raise ValueError("the requested range contains no local trading dates")

    repository = WatchlistRepository(config.watchlist_database)
    existing_latest = repository.latest_fact_date()
    if existing_latest and evaluation_dates[0] <= existing_latest:
        raise ValueError(
            "watchlist facts are append-only and already exist through "
            f"{existing_latest}; append a later date or reconstruct into a new database"
        )

    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    source_summary = reader.source_summary(as_of_date)
    source_digest = _digest(source_summary)
    members = _eligible_members(reader.security_members(as_of_date))
    week_ends = completed_week_end_map(trading_dates)

    trading_position = {value: index for index, value in enumerate(trading_dates)}
    minervini_dates = [
        value for value in evaluation_dates if trading_position.get(value, -1) >= 251
    ]
    rs_values: dict[str, list[tuple[str, float]]] = {value: [] for value in minervini_dates}
    minervini_set = set(minervini_dates)
    for symbol, raw_rows in reader.iter_stock_bars(as_of_date):
        if symbol not in members:
            continue
        profiles = minervini_base_profiles(normalized_daily_bars(raw_rows), minervini_set)
        for value in minervini_dates:
            profile = profiles.get(value)
            rs_return = profile.get("rs_return") if profile else None
            if isinstance(rs_return, (int, float)):
                rs_values[value].append((symbol, float(rs_return)))
    rs_percentiles = {
        value: percentile_ranks(rows) for value, rows in rs_values.items()
    }

    counts: dict[str, Any] = {
        "evaluation_dates": len(evaluation_dates),
        "minervini_evaluation_dates": len(minervini_dates),
        "eligible_symbols": len(members),
        "stock_facts": 0,
        "minervini_pass": 0,
        "weinstein_pass": 0,
        "unknown": 0,
        "transition": 0,
        "index_facts": 0,
        "index_minervini_facts": 0,
        "index_minervini_stage2": 0,
    }
    stock_facts = _stock_fact_stream(
        reader=reader,
        members=members,
        evaluation_dates=evaluation_dates,
        minervini_set=minervini_set,
        week_ends=week_ends,
        rs_percentiles=rs_percentiles,
        as_of_date=as_of_date,
        origin=origin,
        counts=counts,
    )
    index_facts = _index_facts(
        reader=reader,
        as_of_date=as_of_date,
        week_ends=week_ends,
        origin=origin,
    )
    index_minervini_facts = build_index_minervini_facts(
        reader=reader,
        as_of_date=as_of_date,
        origin=origin,
    )
    counts["index_facts"] = len(index_facts)
    counts["index_minervini_facts"] = len(index_minervini_facts)
    counts["index_minervini_stage2"] = sum(
        1 for row in index_minervini_facts if row["result"] == RESULT_PASS
    )
    finished_at = datetime.now().astimezone().isoformat(timespec="seconds")
    run_id = f"watchlist-{as_of_date}-{uuid4().hex[:12]}"
    receipt = {
        "run_id": run_id,
        "as_of_date": as_of_date,
        "from_date": evaluation_dates[0],
        "origin": origin,
        "minervini_policy_version": MINERVINI_POLICY_VERSION,
        "weinstein_policy_version": WEINSTEIN_POLICY_VERSION,
        "market_database": str(config.market_database.resolve()),
        "source_digest": source_digest,
        "counts": counts,
        "status": "SUCCESS",
        "error_message": "",
        "started_at": started_at,
        "finished_at": finished_at,
    }
    repository.persist_run(
        stock_facts=stock_facts,
        index_facts=index_facts,
        index_minervini_facts=index_minervini_facts,
        receipt=receipt,
        identities=_identity_rows(members, as_of_date, origin),
    )
    # The fact generator is consumed inside the transaction, so refresh the
    # completion timestamp and return the actual counts after persistence.
    receipt["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    return {
        **receipt,
        "watchlist_database": str(config.watchlist_database.resolve()),
        "source_summary": source_summary,
    }


def _stock_fact_stream(
    *,
    reader: MarketDataReader,
    members: dict[str, dict[str, Any]],
    evaluation_dates: list[str],
    minervini_set: set[str],
    week_ends: dict[tuple[int, int], str],
    rs_percentiles: dict[str, dict[str, float]],
    as_of_date: str,
    origin: str,
    counts: dict[str, Any],
) -> Iterator[dict[str, Any]]:
    seen: set[str] = set()
    for symbol, raw_rows in reader.iter_stock_bars(as_of_date):
        member = members.get(symbol)
        if member is None:
            continue
        seen.add(symbol)
        bars = normalized_daily_bars(raw_rows)
        minervini = minervini_base_profiles(bars, minervini_set)
        weekly = aggregate_completed_weeks(bars, week_ends)
        stage_series = weinstein_stage_series(weekly)
        weinstein = weinstein_profiles_for_dates(stage_series, evaluation_dates)
        digest = _bars_digest(symbol, raw_rows)
        eligibility = _eligibility_evidence(member)
        for evaluation_date in evaluation_dates:
            if evaluation_date in minervini_set:
                minervini_profile = finish_minervini_profile(
                    minervini.get(evaluation_date),
                    rs_percentiles.get(evaluation_date, {}).get(symbol),
                )
                yield _stock_fact(
                    evaluation_date,
                    symbol,
                    "minervini",
                    MINERVINI_POLICY_VERSION,
                    minervini_profile,
                    eligibility,
                    digest,
                    origin,
                    counts,
                )
            weinstein_profile = weinstein.get(evaluation_date) or {
                "result": RESULT_UNKNOWN,
                "stage": "UNKNOWN",
                "reason": "NO_WEINSTEIN_PROFILE",
                "evidence": {},
            }
            yield _stock_fact(
                evaluation_date,
                symbol,
                "weinstein",
                WEINSTEIN_POLICY_VERSION,
                weinstein_profile,
                eligibility,
                digest,
                origin,
                counts,
            )

    # A security in the authoritative universe without any qfq bars is an
    # explicit unknown, never an implicit failure or a silently missing row.
    for symbol in sorted(set(members) - seen):
        eligibility = _eligibility_evidence(members[symbol])
        for evaluation_date in evaluation_dates:
            method_versions = [("weinstein", WEINSTEIN_POLICY_VERSION)]
            if evaluation_date in minervini_set:
                method_versions.insert(0, ("minervini", MINERVINI_POLICY_VERSION))
            for method, policy_version in method_versions:
                yield _stock_fact(
                    evaluation_date,
                    symbol,
                    method,
                    policy_version,
                    {
                        "result": RESULT_UNKNOWN,
                        "reason": "NO_LOCAL_QFQ_BARS",
                        "metrics": {},
                        "checks": {},
                    },
                    eligibility,
                    "missing",
                    origin,
                    counts,
                )


def _stock_fact(
    as_of_date: str,
    symbol: str,
    method: str,
    policy_version: str,
    profile: dict[str, Any],
    eligibility: dict[str, Any],
    source_digest: str,
    origin: str,
    counts: dict[str, Any],
) -> dict[str, Any]:
    result = str(profile.get("result") or RESULT_UNKNOWN)
    counts["stock_facts"] += 1
    if result == RESULT_UNKNOWN:
        counts["unknown"] += 1
    elif result == RESULT_TRANSITION:
        counts["transition"] += 1
    elif result == RESULT_PASS:
        counts[f"{method}_pass"] += 1
    evidence = {
        "eligibility": eligibility,
        "profile": _compact_profile(method, profile),
    }
    return {
        "as_of_date": as_of_date,
        "symbol": symbol,
        "method": method,
        "result": result,
        "policy_version": policy_version,
        "evidence": evidence,
        "source_digest": source_digest,
        "origin": origin,
    }


def _compact_profile(method: str, profile: dict[str, Any]) -> dict[str, Any]:
    if method == "minervini":
        checks = dict(profile.get("checks") or {})
        result = str(profile.get("result") or RESULT_UNKNOWN)
        metrics = dict(profile.get("metrics") or {})
        if result == "FAIL":
            metrics = {}
        elif result == RESULT_UNKNOWN:
            metrics = {"close": metrics.get("close")}
        return {
            "result": result,
            "reason": str(profile.get("reason") or ""),
            "bar_count": int(profile.get("bar_count") or 0),
            "metrics": metrics,
            "failed_checks": [key for key, passed in checks.items() if not passed],
            "evaluated_check_count": len(checks),
        }
    evidence = dict(profile.get("evidence") or {})
    compact_metrics = {
        key: evidence.get(key)
        for key in (
            "week_count",
            "close",
            "ma30",
            "ma30_slope_4w_pct",
            "distance_from_ma30_pct",
            "return_13w_pct",
            "prior_directional_stage",
        )
        if key in evidence
    }
    return {
        "result": str(profile.get("result") or RESULT_UNKNOWN),
        "stage": str(profile.get("stage") or "UNKNOWN"),
        "stage_started_on": str(profile.get("stage_started_on") or ""),
        "duration_weeks": int(profile.get("duration_weeks") or 0),
        "effective_week_end": str(profile.get("effective_week_end") or ""),
        "reason": str(profile.get("reason") or ""),
        "metrics": compact_metrics,
    }
def _index_facts(
    *,
    reader: MarketDataReader,
    as_of_date: str,
    week_ends: dict[tuple[int, int], str],
    origin: str,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index_symbol, index_name in INDEX_UNIVERSE:
        raw_rows = reader.index_bars(index_symbol, as_of_date)
        bars = normalized_daily_bars(raw_rows)
        weeks = aggregate_completed_weeks(bars, week_ends)
        series = weinstein_stage_series(weeks)
        digest = _bars_digest(index_symbol, raw_rows)
        if not series:
            result.append(
                {
                    "effective_date": as_of_date,
                    "index_symbol": index_symbol,
                    "index_name": index_name,
                    "stage": "UNKNOWN",
                    "stage_started_on": as_of_date,
                    "duration_weeks": 0,
                    "policy_version": WEINSTEIN_POLICY_VERSION,
                    "evidence": {
                        "reason": "NO_COMPLETED_LOCAL_INDEX_WEEK",
                        "week_count": 0,
                        "close": None,
                    },
                    "source_digest": digest,
                    "origin": origin,
                }
            )
            continue
        for fact in series:
            result.append(
                {
                    "effective_date": fact.effective_date,
                    "index_symbol": index_symbol,
                    "index_name": index_name,
                    "stage": fact.stage,
                    "stage_started_on": fact.stage_started_on,
                    "duration_weeks": fact.duration_weeks,
                    "policy_version": WEINSTEIN_POLICY_VERSION,
                    "evidence": fact.evidence,
                    "source_digest": digest,
                    "origin": origin,
                }
            )
    return result


def build_index_minervini_facts(
    *,
    reader: MarketDataReader,
    as_of_date: str,
    origin: str,
) -> list[dict[str, Any]]:
    """Build a Stage 2 yes/no fact for each index from the daily price template.

    The stock-only cross-sectional relative-strength percentile is deliberately
    excluded: ranking four unlike indices would not reproduce Minervini's stock
    selection rule.
    """

    result: list[dict[str, Any]] = []
    for index_symbol, index_name in INDEX_UNIVERSE:
        raw_rows = reader.index_bars(index_symbol, as_of_date)
        bars = normalized_daily_bars(raw_rows)
        base = minervini_base_profiles(bars, {as_of_date}).get(as_of_date)
        if base is None:
            profile = {
                "result": RESULT_UNKNOWN,
                "reason": "MISSING_BAR_FOR_AS_OF_DATE",
                "bar_count": len(bars),
                "metrics": {},
                "checks": {},
            }
        else:
            profile = base
        evidence = _compact_profile("minervini", profile)
        evidence["relative_strength_rule"] = "NOT_APPLICABLE_TO_INDEX"
        result.append(
            {
                "as_of_date": as_of_date,
                "index_symbol": index_symbol,
                "index_name": index_name,
                "result": str(profile.get("result") or RESULT_UNKNOWN),
                "policy_version": MINERVINI_INDEX_STAGE2_POLICY_VERSION,
                "evidence": evidence,
                "source_digest": _bars_digest(index_symbol, raw_rows),
                "origin": origin,
            }
        )
    return result


def _eligible_members(members: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for symbol, member in members.items():
        if not symbol.endswith((".SH", ".SZ")):
            continue
        if str(member.get("listing_status_as_of") or "listed") != "listed":
            continue
        result[symbol] = member
    return result


def _eligibility_evidence(member: dict[str, Any]) -> dict[str, Any]:
    return {
        "is_st": bool(member.get("is_st")),
        "is_suspended": bool(member.get("is_suspended")),
    }


def _identity_rows(
    members: dict[str, dict[str, Any]],
    snapshot_date: str,
    origin: str,
) -> list[dict[str, Any]]:
    return [
        {
            "snapshot_date": snapshot_date,
            "symbol": symbol,
            "name": str(member.get("name") or ""),
            "industry": str(member.get("industry") or ""),
            "list_date": str(member.get("list_date") or ""),
            "is_st": bool(member.get("is_st")),
            "is_suspended": bool(member.get("is_suspended")),
            "listing_status": str(member.get("listing_status_as_of") or ""),
            "trading_status": str(member.get("trading_status") or ""),
            "origin": origin,
        }
        for symbol, member in sorted(members.items())
    ]


def _bars_digest(symbol: str, rows: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    digest.update(symbol.encode("utf-8"))
    for row in rows:
        digest.update(
            (
                f"{row.get('trade_date') or row.get('date')}|{row.get('close')}|"
                f"{row.get('input_hash') or ''}|{row.get('price_scale_id') or ''};"
            ).encode("utf-8")
        )
    return digest.hexdigest()[:20]


def _digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
