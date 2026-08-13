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
    weinstein_provisional_profiles,
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


def test_weinstein_retention_baseline_preserves_stage_start_and_duration():
    start = date(2025, 1, 3)
    weeks = [
        WeeklyBar(
            effective_date=(start + timedelta(days=index * 7)).isoformat(),
            open=100 + index * 3,
            high=100 + index * 3,
            low=100 + index * 3,
            close=100 + index * 3,
            volume=1000,
        )
        for index in range(34)
    ]

    series = weinstein_stage_series(
        weeks,
        {
            "boundary_effective_date": weeks[32].effective_date,
            "previous_stage": "STAGE_2",
            "last_directional_stage": "STAGE_2",
            "stage_started_on": "2024-01-05",
            "duration_weeks": 100,
        },
    )

    assert series[-1].stage == STAGE_2
    assert series[-1].stage_started_on == "2024-01-05"
    assert series[-1].duration_weeks == 101


def test_latest_incomplete_week_is_not_treated_as_completed():
    dates = ["2026-07-27", "2026-07-28", "2026-07-29", "2026-07-30"]

    assert completed_week_end_map(dates) == {}
    assert completed_week_end_map(dates + ["2026-07-31"])


def test_authoritative_calendar_completes_a_holiday_shortened_week():
    dates = ["2026-09-28", "2026-09-29", "2026-09-30"]

    assert completed_week_end_map(dates) == {}
    assert completed_week_end_map(
        dates, authoritative_week_ends=["2026-09-30"]
    ) == {(2026, 40): "2026-09-30"}


def test_weinstein_intraweek_projection_uses_only_prices_available_each_day():
    start = date(2025, 1, 3)
    completed = [
        DailyBar(
            (start + timedelta(days=index * 7)).isoformat(),
            100 + index * 3,
            100 + index * 3,
            100 + index * 3,
            100 + index * 3,
            1000,
        )
        for index in range(34)
    ]
    monday = date.fromisoformat(completed[-1].trade_date) + timedelta(days=3)
    tuesday = monday + timedelta(days=1)
    bars = [
        *completed,
        DailyBar(monday.isoformat(), 203, 203, 203, 203, 1000),
        DailyBar(tuesday.isoformat(), 20, 20, 20, 20, 1000),
    ]
    trading_dates = [bar.trade_date for bar in bars]

    profiles = weinstein_provisional_profiles(
        bars,
        [monday.isoformat(), tuesday.isoformat()],
        trading_dates,
    )

    assert profiles[monday.isoformat()]["result"] == RESULT_PASS
    assert profiles[monday.isoformat()]["evidence"]["close"] == 203.0
    assert profiles[tuesday.isoformat()]["result"] != RESULT_PASS
    assert profiles[tuesday.isoformat()]["evidence"]["close"] == 20.0
    assert profiles[monday.isoformat()]["sessions_elapsed"] == 1
    assert profiles[monday.isoformat()]["is_final_session"] is False


def test_final_session_projection_equals_the_formal_completed_week_stage():
    start = date(2025, 1, 3)
    bars = [
        DailyBar(
            (start + timedelta(days=index * 7)).isoformat(),
            100 + index * 3,
            100 + index * 3,
            100 + index * 3,
            100 + index * 3,
            1000,
        )
        for index in range(40)
    ]
    trading_dates = [bar.trade_date for bar in bars]
    final_date = trading_dates[-1]
    formal = weinstein_stage_series(
        [
            WeeklyBar(
                effective_date=bar.trade_date,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
            )
            for bar in bars
        ]
    )[-1]

    projection = weinstein_provisional_profiles(
        bars, [final_date], trading_dates
    )[final_date]

    assert projection["is_final_session"] is True
    assert projection["projected_stage"] == formal.stage
    assert projection["formal_stage"] == formal.stage
    assert projection["formal_effective_week_end"] == final_date


def test_percentile_ranks_are_cross_sectional_and_ties_are_equal():
    ranks = percentile_ranks([("A", 0.1), ("B", 0.2), ("C", 0.2), ("D", 0.4)])

    assert ranks["A"] == 0.0
    assert ranks["B"] == ranks["C"] == 50.0
    assert ranks["D"] == 100.0
