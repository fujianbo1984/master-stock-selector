from __future__ import annotations

import pytest

from master_stock_selector.watchlist.charting import keltner_channels


def test_keltner_channels_matches_ema_and_wilder_atr_formula() -> None:
    bars = [
        {"trade_date": "2026-01-01", "open": 10.0, "high": 12.0, "low": 9.0, "close": 10.0},
        {"trade_date": "2026-01-02", "open": 11.0, "high": 14.0, "low": 10.0, "close": 13.0},
        {"trade_date": "2026-01-03", "open": 13.0, "high": 15.0, "low": 12.0, "close": 14.0},
    ]

    values = keltner_channels(bars, length=2, multiplier=2, atr_length=2)

    assert values[0] == {"date": "2026-01-01", "basis": 10.0, "upper": None, "lower": None}
    assert values[1] == {"date": "2026-01-02", "basis": 12.0, "upper": 19.0, "lower": 5.0}
    assert values[2]["basis"] == pytest.approx(13.333333)
    assert values[2]["upper"] == pytest.approx(19.833333)
    assert values[2]["lower"] == pytest.approx(6.833333)


def test_keltner_channels_supports_true_range_and_sma() -> None:
    bars = [
        {"trade_date": "2026-01-01", "open": 10.0, "high": 12.0, "low": 9.0, "close": 10.0},
        {"trade_date": "2026-01-02", "open": 11.0, "high": 14.0, "low": 10.0, "close": 13.0},
    ]

    values = keltner_channels(bars, length=2, multiplier=2, use_ema=False, band_style="tr")

    assert values[0]["basis"] is None
    assert values[1] == {"date": "2026-01-02", "basis": 11.5, "upper": 19.5, "lower": 3.5}


def test_keltner_channels_accepts_open_high_and_low_sources() -> None:
    bars = [
        {"trade_date": "2026-01-01", "open": 10.0, "high": 12.0, "low": 9.0, "close": 11.0},
        {"trade_date": "2026-01-02", "open": 14.0, "high": 16.0, "low": 13.0, "close": 15.0},
    ]

    values = keltner_channels(bars, length=1, multiplier=1, source="open", band_style="tr")

    assert values[-1]["basis"] == 14.0
