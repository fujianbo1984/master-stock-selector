from __future__ import annotations

from datetime import date, timedelta

from master_stock_selector.watchlist.methods import (
    RESULT_FAIL,
    RESULT_PASS,
    STAGE_2,
    STAGE_3,
    DailyBar,
    WeeklyBar,
    completed_week_end_map,
    finish_minervini_profile,
    minervini_base_profiles,
    percentile_ranks,
    weinstein_stage_series,
)


def _trading_dates(count: int, start: date = date(2024, 1, 1)) -> list[str]:
    values: list[str] = []
    current = start
    while len(values) < count:
        if current.weekday() < 5:
            values.append(current.isoformat())
        current += timedelta(days=1)
    return values


def test_minervini_uses_daily_150_and_200_day_rules_plus_cross_sectional_rs():
    bars = [
        DailyBar(value, close, close, close, close)
        for value, close in zip(_trading_dates(252), [100 + index for index in range(252)])
    ]
    latest = bars[-1].trade_date
    base = minervini_base_profiles(bars, {latest})[latest]

    passed = finish_minervini_profile(base, 80.0)
    failed = finish_minervini_profile(base, 69.99)

    assert passed["result"] == RESULT_PASS
    assert passed["metrics"]["sma150"] != passed["metrics"]["sma200"]
    assert passed["checks"]["rs_252d_percentile_at_least_70"] is True
    assert failed["result"] == RESULT_FAIL


def test_weinstein_uses_weekly_30_week_average_and_path_to_distinguish_stage_three():
    start = date(2025, 1, 3)
    rising = [100 + index * 3 for index in range(38)]
    flat = [rising[-1]] * 35
    weeks = [
        WeeklyBar(
            effective_date=(start + timedelta(days=index * 7)).isoformat(),
            open=close,
            high=close,
            low=close,
            close=close,
            volume=1000,
        )
        for index, close in enumerate(rising + flat)
    ]

    series = weinstein_stage_series(weeks)

    assert STAGE_2 in [fact.stage for fact in series]
    assert series[-1].stage == STAGE_3
    assert series[-1].evidence["ma30"] != ""
    assert "ma30_slope_4w_pct" in series[-1].evidence


def test_latest_incomplete_week_is_not_treated_as_completed():
    dates = ["2026-07-27", "2026-07-28", "2026-07-29", "2026-07-30"]

    assert completed_week_end_map(dates) == {}
    assert completed_week_end_map(dates + ["2026-07-31"])


def test_percentile_ranks_are_cross_sectional_and_ties_are_equal():
    ranks = percentile_ranks([("A", 0.1), ("B", 0.2), ("C", 0.2), ("D", 0.4)])

    assert ranks["A"] == 0.0
    assert ranks["B"] == ranks["C"] == 50.0
    assert ranks["D"] == 100.0
