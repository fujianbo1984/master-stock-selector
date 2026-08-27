from __future__ import annotations

import pytest

from master_stock_selector.watchlist.charting import (
    default_keltner_preset,
    keltner_channels,
    keltner_config_payload,
)


def test_keltner_presets_keep_daily_calibration_and_weekly_reference_separate() -> None:
    daily_name, daily = default_keltner_preset("day")
    weekly_name, weekly = default_keltner_preset("week")

    assert daily_name == "ashare_daily_calibrated_v1"
    assert (daily["length"], daily["atr_length"], daily["multiplier"]) == (
        20,
        20,
        2.35,
    )
    assert weekly_name == "adam_original_reference_v1"
    assert (weekly["length"], weekly["atr_length"], weekly["multiplier"]) == (
        20,
        20,
        2.25,
    )
    assert keltner_config_payload(
        interval="day",
        length=20,
        atr_length=20,
        multiplier=2.35,
        source="close",
        use_ema=True,
        band_style="atr",
    ) == {
        "length": 20,
        "atr_length": 20,
        "multiplier": 2.35,
        "source": "close",
        "basis": "ema",
        "band_style": "wilder_atr",
        "interval": "day",
        "preset": "ashare_daily_calibrated_v1",
    }


def test_keltner_channels_matches_ema_and_wilder_atr_formula() -> None:
    bars = [
        {"trade_date": "2026-01-01", "open": 10.0, "high": 12.0, "low": 9.0, "close": 10.0},
        {"trade_date": "2026-01-02", "open": 11.0, "high": 14.0, "low": 10.0, "close": 13.0},
        {"trade_date": "2026-01-03", "open": 13.0, "high": 15.0, "low": 12.0, "close": 14.0},
    ]

    values = keltner_channels(
        bars,
        length=2,
        multiplier=2,
        source="close",
        use_ema=True,
        band_style="atr",
        atr_length=2,
    )

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

    values = keltner_channels(
        bars,
        length=2,
        multiplier=2,
        source="close",
        use_ema=False,
        band_style="tr",
        atr_length=2,
    )

    assert values[0]["basis"] is None
    assert values[1] == {"date": "2026-01-02", "basis": 11.5, "upper": 19.5, "lower": 3.5}


def test_keltner_channels_accepts_open_high_and_low_sources() -> None:
    bars = [
        {"trade_date": "2026-01-01", "open": 10.0, "high": 12.0, "low": 9.0, "close": 11.0},
        {"trade_date": "2026-01-02", "open": 14.0, "high": 16.0, "low": 13.0, "close": 15.0},
    ]

    values = keltner_channels(
        bars,
        length=1,
        multiplier=1,
        source="open",
        use_ema=True,
        band_style="tr",
        atr_length=1,
    )

    assert values[-1]["basis"] == 14.0
