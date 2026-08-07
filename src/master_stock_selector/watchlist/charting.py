from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def keltner_channels(
    bars: Sequence[dict[str, Any]],
    *,
    length: int = 20,
    multiplier: float = 2.0,
    source: str = "close",
    use_ema: bool = True,
    band_style: str = "atr",
    atr_length: int = 10,
) -> list[dict[str, float | str | None]]:
    """Match the TradingView KC built-in indicator's documented Pine formula."""

    if length < 1 or atr_length < 1:
        raise ValueError("lengths must be positive")
    if source not in {"close", "open", "high", "low"}:
        raise ValueError("unsupported source")
    if band_style not in {"atr", "tr", "range"}:
        raise ValueError("unsupported band style")
    closes = [float(row["close"]) for row in bars]
    source_values = [float(row[source]) for row in bars]
    true_ranges: list[float] = []
    ranges: list[float] = []
    for index, row in enumerate(bars):
        high, low = float(row["high"]), float(row["low"])
        prior_close = closes[index - 1] if index else None
        true_ranges.append(
            high - low
            if prior_close is None
            else max(high - low, abs(high - prior_close), abs(low - prior_close))
        )
        ranges.append(high - low)
    basis = _ema(source_values, length) if use_ema else _sma(source_values, length)
    band_range = (
        _rma(true_ranges, atr_length)
        if band_style == "atr"
        else [value for value in true_ranges]
        if band_style == "tr"
        else _rma(ranges, length)
    )
    result: list[dict[str, float | str | None]] = []
    for index, row in enumerate(bars):
        center, value = basis[index], band_range[index]
        result.append(
            {
                "date": str(row["trade_date"]),
                "basis": round(center, 6) if center is not None else None,
                "upper": round(center + value * multiplier, 6)
                if center is not None and value is not None
                else None,
                "lower": round(center - value * multiplier, 6)
                if center is not None and value is not None
                else None,
            }
        )
    return result


def _sma(values: Sequence[float], length: int) -> list[float | None]:
    result: list[float | None] = []
    running = 0.0
    for index, value in enumerate(values):
        running += value
        if index >= length:
            running -= values[index - length]
        result.append(running / length if index >= length - 1 else None)
    return result


def _ema(values: Sequence[float], length: int) -> list[float | None]:
    result: list[float | None] = []
    previous: float | None = None
    alpha = 2.0 / (length + 1.0)
    for value in values:
        if previous is None:
            previous = value
        elif previous is not None:
            previous = value * alpha + previous * (1.0 - alpha)
        result.append(previous)
    return result


def _rma(values: Sequence[float], length: int) -> list[float | None]:
    result: list[float | None] = []
    seed = _sma(values, length)
    previous: float | None = None
    for index, value in enumerate(values):
        if previous is None:
            previous = seed[index]
        elif previous is not None:
            previous = (previous * (length - 1) + value) / length
        result.append(previous)
    return result
