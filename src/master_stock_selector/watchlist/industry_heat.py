from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from math import ceil
from statistics import fmean, median
from typing import Any

MINIMUM_HEAT_MEMBERS = 5
MINIMUM_PRICE_COVERAGE_PCT = 95.0
HOT_RETURN_TOP_FRACTION = 0.10
HOT_BREADTH_PCT = 70.0
BROOKS_RESONANCE_MIN_COUNT = 2
BROOKS_RESONANCE_MIN_ENRICHMENT = 2.0

ACTIVE_BROOKS_PHASES = {"h1", "cycle_h1", "new"}


def build_industry_heat_rows(
    *,
    observations: Sequence[Mapping[str, Any]],
    memberships: Sequence[Mapping[str, Any]],
    stock_returns: Mapping[str, Mapping[str, float | None]],
    brooks_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Add short-horizon price breadth and independent Brooks facts to industries.

    Hot-candidate eligibility is intentionally price-only: recent equal-weight
    return, breadth, sample size and coverage. Method facts are annotations and
    never contribute to a combined score.
    """

    members_by_industry: dict[str, list[str]] = defaultdict(list)
    industry_by_symbol: dict[str, str] = {}
    for membership in memberships:
        code = str(membership.get("industry_code") or "").upper()
        symbol = str(membership.get("symbol") or "").upper()
        if not code or not symbol:
            continue
        members_by_industry[code].append(symbol)
        industry_by_symbol[symbol] = code

    brooks_by_industry: dict[str, dict[str, Any]] = defaultdict(_empty_brooks)
    mapped_active_symbols: set[str] = set()
    mapped_symbols = set(industry_by_symbol)
    for raw in brooks_rows:
        symbol = str(raw.get("symbol") or "").upper()
        code = industry_by_symbol.get(symbol, "")
        if not code:
            continue
        phase = str(raw.get("brooks_phase") or "")
        bucket = brooks_by_industry[code]
        bucket["current_symbols"].add(symbol)
        bucket["members"].append({"symbol": symbol, "phase": phase})
        if phase in ACTIVE_BROOKS_PHASES:
            bucket["active_symbols"].add(symbol)
            mapped_active_symbols.add(symbol)
        if phase == "new":
            bucket["new_symbols"].add(symbol)
        elif phase == "h1":
            bucket["h1_symbols"].add(symbol)
        elif phase == "cycle_h1":
            bucket["cycle_h1_symbols"].add(symbol)

    total_eligible_members = sum(int(row.get("eligible_member_count") or 0) for row in observations)
    global_active_rate = (
        len(mapped_active_symbols & mapped_symbols) / total_eligible_members
        if total_eligible_members
        else 0.0
    )

    result: list[dict[str, Any]] = []
    for observation in observations:
        row = dict(observation)
        code = str(row.get("industry_code") or "").upper()
        members = sorted(set(members_by_industry.get(code, [])))
        eligible_count = int(row.get("eligible_member_count") or len(members))
        for horizon in (1, 5, 20):
            values = [
                float(value)
                for symbol in members
                if (value := stock_returns.get(symbol, {}).get(f"return_{horizon}d_pct"))
                is not None
            ]
            valid_count = len(values)
            row[f"return_{horizon}d_pct"] = round(fmean(values), 2) if values else None
            row[f"median_return_{horizon}d_pct"] = (
                round(float(median(values)), 2) if values else None
            )
            row[f"up_{horizon}d_count"] = sum(value > 0 for value in values)
            row[f"priced_{horizon}d_count"] = valid_count
            row[f"breadth_{horizon}d_pct"] = (
                round(100.0 * row[f"up_{horizon}d_count"] / valid_count, 2)
                if valid_count
                else None
            )
            row[f"price_coverage_{horizon}d_pct"] = (
                round(100.0 * valid_count / eligible_count, 2) if eligible_count else None
            )

        brooks = brooks_by_industry.get(code, _empty_brooks())
        current_count = len(brooks["current_symbols"])
        active_count = len(brooks["active_symbols"])
        active_rate = active_count / eligible_count if eligible_count else 0.0
        enrichment = active_rate / global_active_rate if global_active_rate else None
        row.update(
            {
                "brooks_current_count": current_count,
                "brooks_active_count": active_count,
                "brooks_new_count": len(brooks["new_symbols"]),
                "brooks_h1_count": len(brooks["h1_symbols"]),
                "brooks_cycle_h1_count": len(brooks["cycle_h1_symbols"]),
                "brooks_active_rate_pct": round(100.0 * active_rate, 2),
                "brooks_active_enrichment": round(enrichment, 2) if enrichment is not None else None,
                "brooks_members": sorted(
                    brooks["members"], key=lambda item: (item["phase"], item["symbol"])
                ),
            }
        )
        row["brooks_resonance"] = bool(
            active_count >= BROOKS_RESONANCE_MIN_COUNT
            and enrichment is not None
            and enrichment >= BROOKS_RESONANCE_MIN_ENRICHMENT
        )
        row["heat_quality_state"] = _heat_quality_state(row, eligible_count)
        result.append(row)

    ranked = [
        row
        for row in result
        if row["heat_quality_state"] == "COMPLETE" and row.get("return_5d_pct") is not None
    ]
    ranked.sort(key=lambda row: (-float(row["return_5d_pct"]), str(row["industry_code"])))
    hot_limit = max(1, ceil(len(ranked) * HOT_RETURN_TOP_FRACTION)) if ranked else 0
    rank_by_code = {
        str(row["industry_code"]): rank for rank, row in enumerate(ranked, start=1)
    }
    for row in result:
        rank = rank_by_code.get(str(row.get("industry_code") or ""))
        row["return_5d_rank"] = rank
        row["ranked_industry_count"] = len(ranked)
        row["return_5d_percentile"] = (
            round(100.0 * (len(ranked) - rank + 1) / len(ranked), 2)
            if rank is not None and ranked
            else None
        )
        row["price_strong"] = bool(rank is not None and rank <= hot_limit)
        row["breadth_strong"] = bool(
            row.get("breadth_5d_pct") is not None
            and float(row["breadth_5d_pct"]) >= HOT_BREADTH_PCT
        )
        row["is_hot"] = bool(
            row["heat_quality_state"] == "COMPLETE"
            and row["price_strong"]
            and row["breadth_strong"]
        )
        row["heat_reasons"] = [
            label
            for matched, label in (
                (row["price_strong"], "价格强"),
                (row["breadth_strong"], "广度强"),
                (row["brooks_resonance"], "Leg1共振"),
                (
                    int(row.get("w_entered_count") or 0)
                    + int(row.get("w_reentered_count") or 0)
                    > 0,
                    "W扩散",
                ),
                (
                    int(row.get("m_entered_count") or 0)
                    + int(row.get("m_reentered_count") or 0)
                    > 0,
                    "M扩散",
                ),
            )
            if matched
        ]
    return result


def _empty_brooks() -> dict[str, Any]:
    return {
        "current_symbols": set(),
        "active_symbols": set(),
        "new_symbols": set(),
        "h1_symbols": set(),
        "cycle_h1_symbols": set(),
        "members": [],
    }


def _heat_quality_state(row: Mapping[str, Any], eligible_count: int) -> str:
    if str(row.get("quality_state") or "") == "UNKNOWN":
        return "UNKNOWN"
    if eligible_count < MINIMUM_HEAT_MEMBERS:
        return "SMALL_SAMPLE"
    coverage = row.get("price_coverage_5d_pct")
    if coverage is None or float(coverage) < MINIMUM_PRICE_COVERAGE_PCT:
        return "PRICE_INSUFFICIENT"
    return "COMPLETE"
