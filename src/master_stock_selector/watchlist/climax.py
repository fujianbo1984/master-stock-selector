"""Causal daily expansion facts. KC is the existing system preset; no trade signals."""

from __future__ import annotations

import math
from statistics import median
from typing import Any

from .charting import ASHARE_DAILY_KELTNER_PRESET, keltner_channels, keltner_preset

POLICY_VERSION = "climax-system-kc-v1"


def ordinary_ashare(symbol: str) -> bool:
    return (symbol.endswith(".SH") and symbol.startswith(("600", "601", "603", "605", "688"))) or (
        symbol.endswith(".SZ") and symbol.startswith(("000", "001", "002", "003", "300", "301"))
    )


def valid_bar(row: dict[str, Any] | None) -> bool:
    if row is None:
        return False
    try:
        o, h, low, c = (float(row[k]) for k in ("open", "high", "low", "close"))
        return (
            all(math.isfinite(v) and v > 0 for v in (o, h, low, c))
            and low <= min(o, c) <= max(o, c) <= h
        )
    except (KeyError, TypeError, ValueError):
        return False


def seeded_ema(
    rows: list[dict[str, Any] | None], statuses: list[str], period: int
) -> tuple[list[float | None], list[int]]:
    seed: list[float] = []
    value = None
    result: list[float | None] = []
    counts: list[int] = []
    count = 0
    for row, status in zip(rows, statuses):
        if status == "UNKNOWN":
            seed, value, count = [], None, 0
        if row is not None and status == "VALID":
            count += 1
            close = float(row["close"])
            if len(seed) < period:
                seed.append(close)
                if len(seed) == period:
                    value = sum(seed) / period
            else:
                assert value is not None
                value += 2 / (period + 1) * (close - value)
        result.append(value)
        counts.append(count)
    return result, counts


def build_climax(
    symbol: str,
    dates: list[str],
    bars: dict[str, dict[str, Any]],
    metadata: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Inputs are aligned to market sessions. Missing dates must not be compressed away."""
    rows = [bars.get(d) if valid_bar(bars.get(d)) else None for d in dates]
    statuses = [
        "INACTIVE"
        if metadata.get(d, {}).get("active") is False
        else "SUSPENDED"
        if metadata.get(d, {}).get("suspended") and d not in bars
        else "VALID"
        if row is not None and metadata.get(d, {}).get("active") is True
        else "UNKNOWN"
        for d, row in zip(dates, rows)
    ]
    ema20, counts = seeded_ema(rows, statuses, 20)
    ema60, _ = seeded_ema(rows, statuses, 60)
    # Identical function/initialization to the existing chart. Never create another KC series.
    preset = keltner_preset(ASHARE_DAILY_KELTNER_PRESET)
    kc_rows = [bars[d] for d in dates if valid_bar(bars.get(d))]
    kc = {
        str(p["date"]): p["upper"]
        for p in keltner_channels(
            kc_rows,
            **{
                k: preset[k]
                for k in ("length", "atr_length", "multiplier", "source", "use_ema", "band_style")
            },
        )
    }
    tr: list[float | None] = [None] * len(dates)
    for i in range(1, len(dates)):
        row, prior = rows[i], rows[i - 1]
        if (
            statuses[i] == statuses[i - 1] == "VALID"
            and row
            and prior
            and not metadata.get(dates[i], {}).get("suspended")
            and not metadata.get(dates[i - 1], {}).get("suspended")
        ):
            tr[i] = max(
                row["high"] - row["low"],
                abs(row["high"] - prior["close"]),
                abs(row["low"] - prior["close"]),
            )
    daily: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    last_onset = -10000
    previous: bool | None = None
    for i, d in enumerate(dates):
        row, meta = rows[i], metadata.get(d, {})
        fact: dict[str, Any] = {
            "date": d,
            "status": statuses[i],
            "potential": None,
            "reason": "DATA_GAP",
            "metrics": {},
        }
        qualified = (
            i >= 69
            and all(s == "VALID" for s in statuses[i - 69 : i + 1])
            and not any(metadata.get(day, {}).get("suspended") for day in dates[i - 69 : i + 1])
        )
        if row and qualified and ema20[i] is not None and ema60[i] is not None:
            prior_tr = tr[i - 20 : i]
            if all(v is not None for v in prior_tr):
                a = sum(float(v) for v in prior_tr if v is not None) / 20
                if a > 0:
                    close = row["close"]

                    def c(j: int) -> float:
                        return float(bars[dates[j]]["close"])

                    r5, p15 = close / c(i - 5) - 1, c(i - 5) / c(i - 20) - 1
                    p60, p20 = c(i - 5) / c(i - 65) - 1, c(i - 5) / c(i - 25) - 1
                    acceleration = 3 * math.log1p(r5) / math.log1p(p15) if p15 > 0 else None
                    window = [bars[x] for x in dates[i - 4 : i + 1]]
                    free = sum(
                        kc.get(x) is not None and bars[x]["low"] > float(kc[x] or 0)
                        for x in dates[i - 4 : i + 1]
                    )
                    max_tr = max(float(x) for x in tr[i - 4 : i + 1] if x is not None) / a
                    gap = (
                        max(
                            max(0, bars[dates[j]]["low"] - bars[dates[j - 1]]["high"])
                            for j in range(i - 4, i + 1)
                        )
                        / a
                    )
                    clv = (
                        (close - row["low"]) / (row["high"] - row["low"])
                        if row["high"] > row["low"]
                        else 0.5
                    )
                    distance = (close - float(ema20[i] or 0)) / a
                    near = (max(x["high"] for x in window) - close) / a
                    amounts = [bars[x].get("amount") for x in dates[i - 20 : i]]
                    amount = (
                        median(float(x) for x in amounts if x is not None)
                        if all(x is not None and math.isfinite(float(x)) for x in amounts)
                        else None
                    )
                    background = c(i - 5) > float(ema60[i - 5] or math.inf) and float(
                        ema20[i - 5] or -math.inf
                    ) > float(ema20[i - 10] or math.inf)
                    category = (
                        "MATURE"
                        if background and p60 >= 0.25
                        else "LOCAL"
                        if background and p20 >= 0.1
                        else "OTHER"
                    )
                    metrics = dict(
                        return5=r5,
                        pre15=p15,
                        pre60=p60,
                        pre20=p20,
                        acceleration=acceleration,
                        up_fraction10=sum(c(j) > c(j - 1) for j in range(i - 9, i + 1)) / 10,
                        distance=distance,
                        near_high=near,
                        free_bars=free,
                        max_tr=max_tr,
                        gap=gap,
                        atr=a,
                        close=close,
                        clv=clv,
                        amount20=amount,
                    )
                    checks = dict(
                        history=counts[i] >= 120,
                        st=meta.get("st_known") is True and not meta.get("is_st"),
                        liquidity=amount is not None and amount >= 20000,
                        market_value=meta.get("total_mv") is not None
                        and math.isfinite(float(meta["total_mv"]))
                        and float(meta["total_mv"]) > 0,
                        price=close > 2 * a,
                        trend=close > float(ema60[i] or 0)
                        and float(ema20[i] or 0) > float(ema20[i - 5] or math.inf),
                        background=category != "OTHER" and p15 > 0,
                        speed=r5 >= 0.08,
                        acceleration=acceleration is not None and acceleration >= 1.5,
                        distance=distance >= 3,
                        near_high=near <= 0.5,
                        witness=free >= 2 or (max_tr >= 2 and clv >= 0.75) or gap >= 0.5,
                    )
                    known = (
                        meta.get("st_known") is True
                        and meta.get("total_mv") is not None
                        and amount is not None
                    )
                    potential = all(checks.values()) if known else None
                    fact.update(
                        metrics=metrics,
                        checks=checks,
                        category=category,
                        potential=potential,
                        reason="QUALIFIED"
                        if potential
                        else "CONDITIONS_NOT_MET"
                        if known
                        else "QUALIFICATION_UNKNOWN",
                    )
                    if potential and previous is False and i - last_onset >= 20:
                        event = dict(
                            id=f"{symbol}:{d}",
                            date=d,
                            category=category,
                            evidence=metrics,
                            checks=checks,
                            timeline=[dict(date=d, kind="ONSET", close=close)],
                            end_date=dates[min(i + 10, len(dates) - 1)],
                            window_complete=i + 10 < len(dates),
                        )
                        _track(event, i, dates, rows, statuses)
                        events.append(event)
                        last_onset = i
                    fact["cooldown_remaining"] = max(0, 20 - (i - last_onset))
        daily.append(fact)
        previous = fact["potential"]
    return dict(version=POLICY_VERSION, symbol=symbol, daily=daily, events=events)


def _track(
    event: dict[str, Any],
    start: int,
    dates: list[str],
    rows: list[dict[str, Any] | None],
    statuses: list[str],
) -> None:
    a = event["evidence"]["atr"]
    initial = rows[start]
    assert initial is not None
    high = initial["high"]
    pending: dict[str, dict[str, Any]] = {}
    confirmed: set[str] = set()
    for j in range(start + 1, min(start + 11, len(dates))):
        row = rows[j]
        if statuses[j] != "VALID" or row is None:
            pending = {}
            kind = "PAUSED" if statuses[j] == "SUSPENDED" else "UNKNOWN"
            event["timeline"].append(dict(date=dates[j], kind=kind))
            if kind == "UNKNOWN":
                return
            continue
        close = row["close"]
        for kind, attempt in pending.items():
            if close < attempt["close"]:
                event["timeline"].append(
                    dict(
                        date=dates[j],
                        kind=kind + "_CONFIRMED",
                        close=close,
                        attempt_date=attempt["date"],
                        reference=attempt.get("reference"),
                    )
                )
                confirmed.add(kind)
        pending = {}
        clv = (close - row["low"]) / (row["high"] - row["low"]) if row["high"] > row["low"] else 0.5
        prior = rows[j - 1] if statuses[j - 1] == "VALID" else None
        kinds = []
        if (
            "FAILURE" not in confirmed
            and row["high"] >= high - 0.25 * a
            and close <= high - 0.5 * a
            and clv <= 0.35
        ):
            kinds.append("FAILURE")
        if "MOMENTUM" not in confirmed and prior and close - prior["close"] <= -a and clv <= 0.25:
            kinds.append("MOMENTUM")
        for kind in kinds:
            attempt = dict(date=dates[j], kind=kind + "_ATTEMPT", close=close, reference=high)
            event["timeline"].append(attempt)
            if j < start + 10:
                pending[kind] = attempt
        high = max(high, row["high"])
    if start + 10 < len(dates):
        event["timeline"].append(dict(date=dates[start + 10], kind="WINDOW_ENDED"))
