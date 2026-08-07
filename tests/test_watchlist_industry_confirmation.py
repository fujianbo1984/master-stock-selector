from __future__ import annotations

from datetime import date, timedelta

from master_stock_selector.watchlist.industry_confirmation import (
    build_industry_weinstein_confirmation,
)


def _observation(**overrides):
    value = {
        "quality_state": "COMPLETE",
        "mapped_member_count": 10,
        "weinstein_pass_count": 4,
        "weinstein_evaluable_count": 8,
        "w_entered_count": 1,
        "w_reentered_count": 1,
        "w_continuing_count": 3,
        "w_exited_count": 0,
        "w_data_gap_count": 0,
    }
    value.update(overrides)
    return value


def _weekly_proxy_bars(count: int, member_count: int = 10):
    start = date(2025, 1, 3)
    rows = []
    for index in range(count):
        close = 1000 + index * 20
        rows.append(
            {
                "trade_date": (start + timedelta(days=index * 7)).isoformat(),
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "member_count": member_count,
            }
        )
    return rows


def test_industry_confirmation_reuses_weinstein_weekly_stage_and_width():
    result = build_industry_weinstein_confirmation(
        _weekly_proxy_bars(40), _observation()
    )

    assert result["stage"] == "STAGE_2"
    assert result["completed_week_count"] == 40
    assert result["metrics"]["ma30"] is not None
    assert result["weinstein_entered_count"] == 2
    assert "内部扩散均偏强" in result["summary"]


def test_industry_confirmation_fails_closed_for_short_history_or_low_coverage():
    short_history = build_industry_weinstein_confirmation(
        _weekly_proxy_bars(33), _observation()
    )
    low_coverage = build_industry_weinstein_confirmation(
        _weekly_proxy_bars(40, member_count=9), _observation()
    )

    assert short_history["stage"] == "UNKNOWN"
    assert short_history["reason"] == "INSUFFICIENT_COMPLETED_WEEKLY_HISTORY"
    assert low_coverage["stage"] == "UNKNOWN"
    assert low_coverage["reason"] == "PRICE_COVERAGE_INSUFFICIENT"
