from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .methods import (
    STAGE_2,
    STAGE_3,
    STAGE_4,
    STAGE_UNKNOWN,
    aggregate_completed_weeks,
    completed_week_end_map,
    normalized_daily_bars,
    weinstein_stage_series,
)

INDUSTRY_CONFIRMATION_POLICY_VERSION = "industry-weinstein-confirmation-v1"
MINIMUM_COMPLETED_WEEKS = 34
MINIMUM_PRICE_COVERAGE_PCT = 95.0


def build_industry_weinstein_confirmation(
    proxy_bars: Sequence[Mapping[str, Any]],
    observation: Mapping[str, Any],
) -> dict[str, Any]:
    """Describe an industry proxy's Weinstein context without qualifying stocks.

    The result is intentionally request-derived.  It is an aid for the user's
    industry review, not a new method fact and never changes any stock result.
    """

    mapped_members = int(observation.get("mapped_member_count") or 0)
    latest_proxy = dict(proxy_bars[-1]) if proxy_bars else {}
    priced_members = int(latest_proxy.get("member_count") or 0)
    price_coverage_pct = (
        round(100.0 * priced_members / mapped_members, 2) if mapped_members else None
    )
    result = _base_result(
        observation,
        proxy_bar_count=len(proxy_bars),
        priced_members=priced_members,
        price_coverage_pct=price_coverage_pct,
    )

    if str(observation.get("quality_state") or "") == "UNKNOWN":
        return _unknown(result, "MEMBERSHIP_MAPPING_INSUFFICIENT")
    if price_coverage_pct is None or price_coverage_pct < MINIMUM_PRICE_COVERAGE_PCT:
        return _unknown(result, "PRICE_COVERAGE_INSUFFICIENT")

    daily = normalized_daily_bars(proxy_bars)
    week_ends = completed_week_end_map([bar.trade_date for bar in daily])
    weeks = aggregate_completed_weeks(daily, week_ends)
    result["completed_week_count"] = len(weeks)
    if len(weeks) < MINIMUM_COMPLETED_WEEKS:
        return _unknown(result, "INSUFFICIENT_COMPLETED_WEEKLY_HISTORY")

    fact = weinstein_stage_series(weeks)[-1]
    evidence = dict(fact.evidence)
    result.update(
        {
            "stage": fact.stage,
            "effective_week_end": fact.effective_date,
            "stage_started_on": fact.stage_started_on,
            "duration_weeks": fact.duration_weeks,
            "metrics": {
                key: evidence.get(key)
                for key in (
                    "close",
                    "ma30",
                    "ma30_slope_4w_pct",
                    "distance_from_ma30_pct",
                    "return_13w_pct",
                    "close_above_ma30",
                )
            },
            "reason": "STAGE_RULES_APPLIED",
        }
    )
    result["summary"] = _summary(result)
    return result


def _base_result(
    observation: Mapping[str, Any],
    *,
    proxy_bar_count: int,
    priced_members: int,
    price_coverage_pct: float | None,
) -> dict[str, Any]:
    return {
        "policy_version": INDUSTRY_CONFIRMATION_POLICY_VERSION,
        "stage": STAGE_UNKNOWN,
        "effective_week_end": "",
        "stage_started_on": "",
        "duration_weeks": 0,
        "completed_week_count": 0,
        "proxy_bar_count": proxy_bar_count,
        "mapped_member_count": int(observation.get("mapped_member_count") or 0),
        "priced_member_count": priced_members,
        "price_coverage_pct": price_coverage_pct,
        "weinstein_pass_count": int(observation.get("weinstein_pass_count") or 0),
        "weinstein_evaluable_count": int(observation.get("weinstein_evaluable_count") or 0),
        "weinstein_entered_count": int(observation.get("w_entered_count") or 0)
        + int(observation.get("w_reentered_count") or 0),
        "weinstein_continuing_count": int(observation.get("w_continuing_count") or 0),
        "weinstein_exited_count": int(observation.get("w_exited_count") or 0)
        + int(observation.get("w_data_gap_count") or 0),
        "metrics": {},
        "reason": "",
        "summary": "行业确认数据不足，不作趋势判断。",
    }


def _unknown(result: dict[str, Any], reason: str) -> dict[str, Any]:
    result["reason"] = reason
    result["stage"] = STAGE_UNKNOWN
    result["summary"] = "行业确认数据不足，不作趋势判断。"
    return result


def _summary(result: Mapping[str, Any]) -> str:
    stage = str(result.get("stage") or STAGE_UNKNOWN)
    entered = int(result.get("weinstein_entered_count") or 0)
    continuing = int(result.get("weinstein_continuing_count") or 0)
    exited = int(result.get("weinstein_exited_count") or 0)
    pass_count = int(result.get("weinstein_pass_count") or 0)
    if stage == STAGE_2 and pass_count > 0 and (entered > 0 or continuing > 0):
        return "行业趋势与内部扩散均偏强，优先人工复核其中个股。"
    if stage in {STAGE_3, STAGE_4}:
        return "行业未提供上升阶段确认；个股方法结论保持独立。"
    if exited > 0:
        return "行业处于过渡阶段且已有退出，人工复核时应关注风险。"
    return "行业尚未提供明确的上升阶段确认。"
