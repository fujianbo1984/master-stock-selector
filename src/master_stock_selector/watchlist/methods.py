from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Mapping

MINERVINI_POLICY_VERSION = "minervini-trend-template-v1"
MINERVINI_INDEX_STAGE2_POLICY_VERSION = "minervini-index-stage2-price-template-v1"
WEINSTEIN_POLICY_VERSION = "weinstein-stage-30w-v1"

RESULT_PASS = "PASS"
RESULT_FAIL = "FAIL"
RESULT_UNKNOWN = "UNKNOWN"
RESULT_TRANSITION = "TRANSITION"

STAGE_1 = "STAGE_1"
STAGE_2 = "STAGE_2"
STAGE_3 = "STAGE_3"
STAGE_4 = "STAGE_4"
STAGE_TRANSITION = "TRANSITION"
STAGE_UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class DailyBar:
    trade_date: str
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None


@dataclass(frozen=True)
class WeeklyBar:
    effective_date: str
    open: float
    high: float
    low: float
    close: float
    volume: float | None


@dataclass(frozen=True)
class WeinsteinStageFact:
    effective_date: str
    stage: str
    stage_started_on: str
    duration_weeks: int
    evidence: dict[str, Any]


def normalized_daily_bars(rows: Iterable[Mapping[str, Any]]) -> list[DailyBar]:
    by_date: dict[str, DailyBar] = {}
    for row in rows:
        trade_date = str(row.get("trade_date") or row.get("date") or "")
        close_value = row.get("close")
        if close_value is None:
            continue
        try:
            close = float(close_value)
            if not trade_date or close <= 0:
                continue
            by_date[trade_date] = DailyBar(
                trade_date=trade_date,
                open=float(row.get("open") or close),
                high=float(row.get("high") or close),
                low=float(row.get("low") or close),
                close=close,
                volume=(
                    float(row["volume"])
                    if row.get("volume") is not None
                    else None
                ),
            )
        except (TypeError, ValueError):
            continue
    return [by_date[key] for key in sorted(by_date)]


def minervini_base_profiles(
    bars: list[DailyBar],
    evaluation_dates: set[str],
) -> dict[str, dict[str, Any]]:
    """Return the price-template facts; cross-sectional RS is added later."""

    if not bars or not evaluation_dates:
        return {}
    closes = [bar.close for bar in bars]
    prefix = [0.0]
    for value in closes:
        prefix.append(prefix[-1] + value)

    rolling_high = _rolling_extreme([bar.high for bar in bars], 252, maximum=True)
    rolling_low = _rolling_extreme([bar.low for bar in bars], 252, maximum=False)
    result: dict[str, dict[str, Any]] = {}
    for index, bar in enumerate(bars):
        if bar.trade_date not in evaluation_dates:
            continue
        if index < 251:
            result[bar.trade_date] = {
                "result": RESULT_UNKNOWN,
                "reason": "INSUFFICIENT_DAILY_HISTORY",
                "bar_count": index + 1,
                "rs_return": None,
                "metrics": {"close": _rounded(bar.close)},
                "checks": {},
            }
            continue
        sma50 = _window_average(prefix, index, 50)
        sma150 = _window_average(prefix, index, 150)
        sma200 = _window_average(prefix, index, 200)
        sma200_prior = _window_average(prefix, index - 20, 200)
        high_52w = rolling_high[index]
        low_52w = rolling_low[index]
        checks = OrderedDict(
            [
                ("close_above_sma50", bar.close > sma50),
                ("close_above_sma150", bar.close > sma150),
                ("close_above_sma200", bar.close > sma200),
                ("sma50_above_sma150", sma50 > sma150),
                ("sma50_above_sma200", sma50 > sma200),
                ("sma150_above_sma200", sma150 > sma200),
                ("sma200_rising_20d", sma200 > sma200_prior),
                ("close_30pct_above_52w_low", bar.close >= low_52w * 1.30),
                ("close_within_25pct_52w_high", bar.close >= high_52w * 0.75),
            ]
        )
        result[bar.trade_date] = {
            "result": RESULT_PASS if all(checks.values()) else RESULT_FAIL,
            "reason": "PRICE_TEMPLATE_COMPLETE",
            "bar_count": index + 1,
            "rs_return": bar.close / closes[index - 251] - 1,
            "metrics": {
                "close": _rounded(bar.close),
                "sma50": _rounded(sma50),
                "sma150": _rounded(sma150),
                "sma200": _rounded(sma200),
                "sma200_20d_ago": _rounded(sma200_prior),
                "high_52w": _rounded(high_52w),
                "low_52w": _rounded(low_52w),
            },
            "checks": checks,
        }
    return result


def finish_minervini_profile(
    base: Mapping[str, Any] | None,
    rs_percentile: float | None,
) -> dict[str, Any]:
    if not base:
        return {
            "result": RESULT_UNKNOWN,
            "reason": "MISSING_BAR_FOR_AS_OF_DATE",
            "metrics": {},
            "checks": {},
        }
    item = dict(base)
    base_result = str(item.get("result") or RESULT_UNKNOWN)
    metrics = dict(item.get("metrics") or {})
    checks = OrderedDict(item.get("checks") or {})
    metrics["rs_252d_percentile"] = _rounded(rs_percentile)
    if base_result == RESULT_UNKNOWN or rs_percentile is None:
        item["result"] = RESULT_UNKNOWN
        item["reason"] = (
            str(item.get("reason") or "")
            if base_result == RESULT_UNKNOWN
            else "MISSING_CROSS_SECTIONAL_RS"
        )
    else:
        checks["rs_252d_percentile_at_least_70"] = rs_percentile >= 70.0
        item["result"] = RESULT_PASS if all(checks.values()) else RESULT_FAIL
        item["reason"] = "ALL_RULES_PASS" if item["result"] == RESULT_PASS else "RULES_FAILED"
    item["metrics"] = metrics
    item["checks"] = checks
    return item


def aggregate_completed_weeks(
    bars: list[DailyBar],
    completed_week_ends: Mapping[tuple[int, int], str],
) -> list[WeeklyBar]:
    buckets: OrderedDict[tuple[int, int], list[DailyBar]] = OrderedDict()
    for bar in bars:
        parsed = date.fromisoformat(bar.trade_date)
        iso = parsed.isocalendar()
        buckets.setdefault((iso.year, iso.week), []).append(bar)
    weeks: list[WeeklyBar] = []
    for week_key, items in buckets.items():
        effective_date = completed_week_ends.get(week_key)
        if not effective_date:
            continue
        volume_values = [item.volume for item in items if item.volume is not None]
        weeks.append(
            WeeklyBar(
                effective_date=effective_date,
                open=items[0].open,
                high=max(item.high for item in items),
                low=min(item.low for item in items),
                close=items[-1].close,
                volume=sum(volume_values) if volume_values else None,
            )
        )
    return weeks


def weinstein_stage_series(weeks: list[WeeklyBar]) -> list[WeinsteinStageFact]:
    closes = [week.close for week in weeks]
    prefix = [0.0]
    for value in closes:
        prefix.append(prefix[-1] + value)
    raw_stages: list[str] = []
    stage_started_on = ""
    duration = 0
    previous_stage = ""
    last_directional_stage = ""
    facts: list[WeinsteinStageFact] = []
    for index, week in enumerate(weeks):
        if index < 33:
            stage = STAGE_UNKNOWN
            evidence = {
                "reason": "INSUFFICIENT_WEEKLY_HISTORY",
                "week_count": index + 1,
                "close": _rounded(week.close),
            }
        else:
            ma30 = _window_average(prefix, index, 30)
            ma30_prior = _window_average(prefix, index - 4, 30)
            slope_pct = (ma30 / ma30_prior - 1) * 100 if ma30_prior > 0 else 0.0
            distance_from_ma30_pct = (week.close / ma30 - 1) * 100 if ma30 > 0 else 0.0
            return_13w = (
                (week.close / closes[index - 13] - 1) * 100
                if closes[index - 13] > 0
                else 0.0
            )
            if week.close > ma30 and slope_pct > 1.0 and return_13w > 0:
                stage = STAGE_2
            elif week.close < ma30 and slope_pct < -1.0 and return_13w < 0:
                stage = STAGE_4
            elif abs(slope_pct) <= 1.0:
                if last_directional_stage == STAGE_2 and abs(distance_from_ma30_pct) <= 5.0:
                    stage = STAGE_3
                elif last_directional_stage == STAGE_4 and abs(distance_from_ma30_pct) <= 5.0:
                    stage = STAGE_1
                else:
                    stage = STAGE_TRANSITION
            else:
                stage = STAGE_TRANSITION
            evidence = {
                "reason": "STAGE_RULES_APPLIED",
                "week_count": index + 1,
                "close": _rounded(week.close),
                "ma30": _rounded(ma30),
                "ma30_4w_ago": _rounded(ma30_prior),
                "ma30_slope_4w_pct": _rounded(slope_pct),
                "distance_from_ma30_pct": _rounded(distance_from_ma30_pct),
                "return_13w_pct": _rounded(return_13w),
                "close_above_ma30": week.close > ma30,
                "prior_directional_stage": last_directional_stage or None,
            }
        raw_stages.append(stage)
        if stage != previous_stage:
            stage_started_on = week.effective_date
            duration = 1
        else:
            duration += 1
        if stage in {STAGE_2, STAGE_4}:
            last_directional_stage = stage
        facts.append(
            WeinsteinStageFact(
                effective_date=week.effective_date,
                stage=stage,
                stage_started_on=stage_started_on,
                duration_weeks=duration,
                evidence=evidence,
            )
        )
        previous_stage = stage
    return facts


def weinstein_profiles_for_dates(
    stage_series: list[WeinsteinStageFact],
    evaluation_dates: list[str],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    position = -1
    for evaluation_date in sorted(evaluation_dates):
        while (
            position + 1 < len(stage_series)
            and stage_series[position + 1].effective_date <= evaluation_date
        ):
            position += 1
        if position < 0:
            result[evaluation_date] = {
                "result": RESULT_UNKNOWN,
                "stage": STAGE_UNKNOWN,
                "reason": "NO_COMPLETED_WEEK_WITH_ENOUGH_HISTORY",
                "evidence": {},
            }
            continue
        fact = stage_series[position]
        stage = fact.stage
        method_result = {
            STAGE_2: RESULT_PASS,
            STAGE_TRANSITION: RESULT_TRANSITION,
            STAGE_UNKNOWN: RESULT_UNKNOWN,
        }.get(stage, RESULT_FAIL)
        result[evaluation_date] = {
            "result": method_result,
            "stage": stage,
            "stage_started_on": fact.stage_started_on,
            "duration_weeks": fact.duration_weeks,
            "effective_week_end": fact.effective_date,
            "reason": str(fact.evidence.get("reason") or ""),
            "evidence": fact.evidence,
        }
    return result


def completed_week_end_map(trading_dates: list[str]) -> dict[tuple[int, int], str]:
    """A week is complete only when a later known trading date is in another week.

    The latest observed week is accepted only when its final date is Friday. A
    holiday-shortened latest week must be run with an authoritative future
    calendar check by the caller rather than guessed here.
    """

    by_week: OrderedDict[tuple[int, int], list[str]] = OrderedDict()
    for value in sorted(set(trading_dates)):
        parsed = date.fromisoformat(value)
        iso = parsed.isocalendar()
        by_week.setdefault((iso.year, iso.week), []).append(value)
    keys = list(by_week)
    result: dict[tuple[int, int], str] = {}
    for index, key in enumerate(keys):
        end_date = by_week[key][-1]
        if index < len(keys) - 1 or date.fromisoformat(end_date).weekday() == 4:
            result[key] = end_date
    return result


def percentile_ranks(values: list[tuple[str, float]]) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values, key=lambda item: (item[1], item[0]))
    if len(ordered) == 1:
        return {ordered[0][0]: 100.0}
    result: dict[str, float] = {}
    index = 0
    denominator = len(ordered) - 1
    while index < len(ordered):
        end = index
        while end + 1 < len(ordered) and ordered[end + 1][1] == ordered[index][1]:
            end += 1
        average_rank = (index + end) / 2
        percentile = average_rank / denominator * 100
        for position in range(index, end + 1):
            result[ordered[position][0]] = round(percentile, 4)
        index = end + 1
    return result


def _rolling_extreme(values: list[float], window: int, *, maximum: bool) -> list[float]:
    queue: deque[int] = deque()
    result: list[float] = []
    for index, value in enumerate(values):
        while queue and queue[0] <= index - window:
            queue.popleft()
        while queue and (
            values[queue[-1]] <= value if maximum else values[queue[-1]] >= value
        ):
            queue.pop()
        queue.append(index)
        result.append(values[queue[0]])
    return result


def _window_average(prefix: list[float], end_index: int, window: int) -> float:
    start = end_index + 1 - window
    return (prefix[end_index + 1] - prefix[start]) / window


def _rounded(value: float | None) -> float | None:
    return round(value, 4) if value is not None else None
