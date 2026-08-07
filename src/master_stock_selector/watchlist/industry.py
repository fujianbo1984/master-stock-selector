from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

INDUSTRY_POLICY_VERSION = "industry-observation-v2-non-st"
INDUSTRY_TAXONOMY = "SW2021"
INDUSTRY_LEVEL = "L3"
INDUSTRY_SOURCE = "tushare:index_member_all"
MINIMUM_RATE_SAMPLE = 5
MINIMUM_MEMBERSHIP_COVERAGE_PCT = 95.0

TRANSITION_STATES = ("ENTERED", "REENTERED", "CONTINUING", "EXITED", "DATA_GAP")


def build_industry_observations(
    *,
    as_of_date: str,
    eligible_symbols: Sequence[str],
    memberships: Sequence[Mapping[str, Any]],
    method_facts: Sequence[Mapping[str, Any]],
    membership_snapshot_date: str,
    membership_source_digest: str,
    minervini_policy_version: str,
    weinstein_policy_version: str,
    origin: str,
) -> list[dict[str, Any]]:
    """Aggregate method facts by industry without changing any stock-level fact."""

    eligible = {str(symbol).upper() for symbol in eligible_symbols if symbol}
    memberships_by_symbol: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    industry_names: dict[str, str] = {}
    for row in memberships:
        symbol = str(row.get("symbol") or "").upper()
        code = str(row.get("industry_code") or "")
        if not symbol or not code:
            continue
        memberships_by_symbol[symbol].append(row)
        industry_names[code] = str(row.get("industry_name") or code)

    ambiguous_symbols = {
        symbol for symbol, rows in memberships_by_symbol.items()
        if len({str(row.get("industry_code") or "") for row in rows}) != 1
    }
    unique_membership = {
        symbol: rows[0]
        for symbol, rows in memberships_by_symbol.items()
        if symbol not in ambiguous_symbols
    }
    mapped_eligible = eligible & unique_membership.keys()
    coverage_pct = round(100.0 * len(mapped_eligible) / len(eligible), 4) if eligible else 0.0
    global_unknown = (
        bool(ambiguous_symbols & eligible)
        or coverage_pct < MINIMUM_MEMBERSHIP_COVERAGE_PCT
    )

    facts_by_industry: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    fact_digest_rows: list[tuple[str, str, str, str, str]] = []
    for fact in method_facts:
        symbol = str(fact.get("symbol") or "").upper()
        membership = unique_membership.get(symbol)
        if symbol not in eligible or membership is None:
            continue
        code = str(membership.get("industry_code") or "")
        facts_by_industry[code].append(fact)
        fact_digest_rows.append(
            (
                symbol,
                str(fact.get("method") or ""),
                str(fact.get("result") or ""),
                str(fact.get("state") or ""),
                str(fact.get("source_digest") or ""),
            )
        )

    members_by_industry: dict[str, set[str]] = defaultdict(set)
    for symbol in mapped_eligible:
        code = str(unique_membership[symbol].get("industry_code") or "")
        members_by_industry[code].add(symbol)

    observation_digest = _digest(
        {
            "as_of_date": as_of_date,
            "membership_source_digest": membership_source_digest,
            "eligible_symbols": sorted(eligible),
            "facts": sorted(fact_digest_rows),
            "policy_version": INDUSTRY_POLICY_VERSION,
        }
    )
    observations: list[dict[str, Any]] = []
    for code in sorted(members_by_industry):
        rows = facts_by_industry.get(code, [])
        by_method = {
            method: [row for row in rows if str(row.get("method") or "") == method]
            for method in ("weinstein", "minervini")
        }
        w_rows = by_method["weinstein"]
        m_rows = by_method["minervini"]
        w_pass = {str(row.get("symbol") or "") for row in w_rows if row.get("result") == "PASS"}
        m_pass = {str(row.get("symbol") or "") for row in m_rows if row.get("result") == "PASS"}
        w_evaluable = sum(1 for row in w_rows if row.get("result") != "UNKNOWN")
        m_evaluable = sum(1 for row in m_rows if row.get("result") != "UNKNOWN")
        member_count = len(members_by_industry[code])
        quality_state = (
            "UNKNOWN" if global_unknown
            else "SMALL_SAMPLE" if member_count < MINIMUM_RATE_SAMPLE
            else "COMPLETE"
        )
        item: dict[str, Any] = {
            "as_of_date": as_of_date,
            "taxonomy": INDUSTRY_TAXONOMY,
            "industry_level": INDUSTRY_LEVEL,
            "industry_code": code,
            "industry_name": industry_names.get(code, code),
            "eligible_member_count": member_count,
            "mapped_member_count": member_count,
            "weinstein_evaluable_count": w_evaluable,
            "weinstein_pass_count": len(w_pass),
            "weinstein_pass_rate": round(len(w_pass) / w_evaluable, 6) if w_evaluable else None,
            "minervini_evaluable_count": m_evaluable,
            "minervini_pass_count": len(m_pass),
            "minervini_pass_rate": round(len(m_pass) / m_evaluable, 6) if m_evaluable else None,
            "both_pass_count": len(w_pass & m_pass),
            "union_pass_count": len(w_pass | m_pass),
            "membership_coverage_pct": coverage_pct,
            "membership_snapshot_date": membership_snapshot_date,
            "weinstein_policy_version": weinstein_policy_version,
            "minervini_policy_version": minervini_policy_version,
            "policy_version": INDUSTRY_POLICY_VERSION,
            "quality_state": quality_state,
            "source_digest": observation_digest,
            "origin": origin,
        }
        for method, method_rows in (("weinstein", w_rows), ("minervini", m_rows)):
            prefix = "w" if method == "weinstein" else "m"
            for state in TRANSITION_STATES:
                item[f"{prefix}_{state.lower()}_count"] = sum(
                    1 for row in method_rows if str(row.get("state") or "") == state
                )
        observations.append(item)
    return observations


def _digest(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
