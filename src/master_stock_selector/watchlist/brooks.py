from __future__ import annotations

import hashlib
import json
import math
from bisect import bisect_right
from dataclasses import dataclass
from statistics import fmean, median
from typing import Any, Iterable, Mapping, Sequence

BROOKS_POLICY_VERSION = "brooks-second-leg-daily-v1"

STATE_S0 = "S0_IMPULSE"
STATE_S1 = "S1_TOUCH25"
STATE_S2 = "S2_H1"
STATE_S2_H2 = "S2_H1_H2_TAG"
STATE_COMPLETED = "COMPLETED"
STATE_INVALIDATED = "INVALIDATED"
STATE_STALE = "STALE"
STATE_PAUSED = "PAUSED"

ACTIVE_STATES = {STATE_S0, STATE_S1, STATE_S2, STATE_S2_H2, STATE_PAUSED}
STATE_RANKS = {
    STATE_S2: 1,
    STATE_S2_H2: 1,
    STATE_S1: 2,
    STATE_S0: 3,
    STATE_PAUSED: 4,
    STATE_COMPLETED: 8,
    STATE_INVALIDATED: 9,
    STATE_STALE: 10,
}


@dataclass(frozen=True)
class BrooksBar:
    trade_date: str
    open: float
    high: float
    low: float
    close: float
    amount: float | None
    raw_open: float
    raw_high: float
    raw_low: float
    raw_close: float
    pre_close: float | None = None
    source_token: str = ""


@dataclass(frozen=True)
class SecurityState:
    valid_from: str
    name: str = ""
    industry: str = ""
    list_date: str = ""
    listing_status: str = "listed"
    is_st: bool = False
    is_suspended: bool = False
    trading_status: str = "trading"


@dataclass(frozen=True)
class BrooksImpulse:
    event_id: str
    symbol: str
    signal_pos: int
    start_pos: int
    signal_date: str
    start_date: str
    duration_bars: int
    impulse_start: float
    signal_close: float
    signal_high: float
    impulse_height: float
    atr20: float
    move_atr: float
    path_efficiency: float
    bullish_body_count: int
    bullish_body_share: float
    mean_close_location: float
    mean_overlap: float
    speed_atr_per_session: float
    median_amount20: float
    source_digest: str


def normalized_brooks_bars(rows: Iterable[Mapping[str, Any]]) -> list[BrooksBar]:
    by_date: dict[str, BrooksBar] = {}
    for row in rows:
        trade_date = str(row.get("trade_date") or row.get("date") or "")
        try:
            values = {
                key: float(row[key])
                for key in ("open", "high", "low", "close")
                if row.get(key) is not None
            }
            if len(values) != 4 or not trade_date or min(values.values()) <= 0:
                continue
            if values["high"] < max(values["open"], values["low"], values["close"]):
                continue
            if values["low"] > min(values["open"], values["high"], values["close"]):
                continue
            raw_values = {
                key: float(row.get(f"raw_{key}") or values[key])
                for key in ("open", "high", "low", "close")
            }
            by_date[trade_date] = BrooksBar(
                trade_date=trade_date,
                open=values["open"],
                high=values["high"],
                low=values["low"],
                close=values["close"],
                amount=_optional_float(row.get("amount")),
                raw_open=raw_values["open"],
                raw_high=raw_values["high"],
                raw_low=raw_values["low"],
                raw_close=raw_values["close"],
                pre_close=_optional_float(row.get("pre_close")),
                source_token=str(row.get("source_token") or row.get("input_hash") or ""),
            )
        except (KeyError, TypeError, ValueError):
            continue
    return [by_date[value] for value in sorted(by_date)]


def build_brooks_impulses(
    symbol: str,
    bars: Sequence[BrooksBar],
    security_history: Sequence[SecurityState],
    *,
    move_atr_min: float = 3.0,
) -> list[BrooksImpulse]:
    if len(bars) < 134:
        return []
    atr = _prior_atr20(bars)
    states = _states_for_bars(bars, security_history)
    result: list[BrooksImpulse] = []
    last_signal = -(10**9)
    for index in range(133, len(bars)):
        if index - last_signal < 40:
            continue
        state = states[index]
        if not _signal_eligible(symbol, bars[index], state):
            continue
        amounts = [bar.amount for bar in bars[index - 20 : index]]
        if len(amounts) != 20 or any(value is None for value in amounts):
            continue
        median_amount20 = median(float(value) for value in amounts if value is not None)
        atr_value = atr[index]
        if median_amount20 < 20_000.0 or atr_value is None or atr_value <= 0:
            continue
        qualifying: list[BrooksImpulse] = []
        for duration in (3, 5, 8):
            candidate = _impulse_candidate(
                symbol,
                bars,
                index,
                duration,
                atr_value,
                median_amount20,
                move_atr_min,
            )
            if candidate is not None:
                qualifying.append(candidate)
        if not qualifying:
            continue
        selected = max(qualifying, key=lambda item: item.duration_bars)
        result.append(selected)
        last_signal = index
    return result


def build_brooks_daily_facts(
    event: BrooksImpulse,
    bars: Sequence[BrooksBar],
    security_history: Sequence[SecurityState],
    evaluation_dates: Sequence[str],
    *,
    origin: str,
) -> list[dict[str, Any]]:
    dates = [bar.trade_date for bar in bars]
    states = _states_for_bars(bars, security_history)
    result: list[dict[str, Any]] = []
    for as_of_date in sorted(set(evaluation_dates)):
        if as_of_date < event.signal_date:
            continue
        exact = bisect_right(dates, as_of_date) - 1
        if exact < event.signal_pos:
            continue
        snapshot = _event_snapshot(event, bars, states, exact)
        exact_bar = bars[exact].trade_date == as_of_date
        if not exact_bar and snapshot["state"] in ACTIVE_STATES:
            snapshot = {
                **snapshot,
                "state": STATE_PAUSED,
                "state_reason": "MISSING_DECISION_DATE_BAR",
                "state_rank": STATE_RANKS[STATE_PAUSED],
                "next_open_status": (
                    "REJECTED_MISSING"
                    if snapshot.get("evidence", {}).get("decision_date")
                    else snapshot["next_open_status"]
                ),
            }
        if snapshot["state"] not in ACTIVE_STATES and snapshot["state_changed_on"] != as_of_date:
            continue
        result.append(
            {
                "as_of_date": as_of_date,
                "event_id": event.event_id,
                "symbol": event.symbol,
                "policy_version": BROOKS_POLICY_VERSION,
                "origin": origin,
                "source_digest": _fact_digest(event, as_of_date, snapshot),
                **snapshot,
            }
        )
    return result


def impulse_row(event: BrooksImpulse, *, origin: str) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "symbol": event.symbol,
        "signal_date": event.signal_date,
        "start_date": event.start_date,
        "duration_bars": event.duration_bars,
        "impulse_start": event.impulse_start,
        "signal_close": event.signal_close,
        "signal_high": event.signal_high,
        "impulse_height": event.impulse_height,
        "atr20": event.atr20,
        "move_atr": event.move_atr,
        "path_efficiency": event.path_efficiency,
        "bullish_body_count": event.bullish_body_count,
        "bullish_body_share": event.bullish_body_share,
        "mean_close_location": event.mean_close_location,
        "mean_overlap": event.mean_overlap,
        "speed_atr_per_session": event.speed_atr_per_session,
        "median_amount20": event.median_amount20,
        "signal_tradability": "TRADABLE",
        "source_digest": event.source_digest,
        "origin": origin,
        "policy_version": BROOKS_POLICY_VERSION,
    }


def _impulse_candidate(
    symbol: str,
    bars: Sequence[BrooksBar],
    index: int,
    duration: int,
    atr20: float,
    median_amount20: float,
    move_atr_min: float,
) -> BrooksImpulse | None:
    start = index - duration
    impulse_height = bars[index].close - bars[start].close
    changes = [
        abs(bars[position].close - bars[position - 1].close)
        for position in range(start + 1, index + 1)
    ]
    path = sum(changes)
    if impulse_height <= 0 or path <= 0:
        return None
    move_atr = impulse_height / atr20
    path_efficiency = impulse_height / path
    quality_bars = bars[start + 1 : index + 1]
    bullish_body_count = sum(bar.close > bar.open for bar in quality_bars)
    bullish_body_share = bullish_body_count / duration
    mean_close_location = fmean(_close_location(bar) for bar in quality_bars)
    mean_overlap = fmean(
        _pair_overlap(bars[position - 1], bars[position])
        for position in range(start + 1, index + 1)
    )
    if not (
        move_atr >= move_atr_min
        and path_efficiency >= 0.75
        and bullish_body_count >= math.ceil(0.60 * duration)
        and bullish_body_count >= 2
        and mean_close_location >= 0.65
        and mean_overlap <= 0.35
    ):
        return None
    signal_date = bars[index].trade_date
    event_id = f"brooks|{symbol}|{signal_date}|{duration}"
    digest = hashlib.sha256(
        "|".join(
            [event_id, *(bar.source_token or bar.trade_date for bar in bars[start : index + 1])]
        ).encode("utf-8")
    ).hexdigest()[:20]
    return BrooksImpulse(
        event_id=event_id,
        symbol=symbol,
        signal_pos=index,
        start_pos=start,
        signal_date=signal_date,
        start_date=bars[start].trade_date,
        duration_bars=duration,
        impulse_start=bars[start].close,
        signal_close=bars[index].close,
        signal_high=bars[index].high,
        impulse_height=impulse_height,
        atr20=atr20,
        move_atr=move_atr,
        path_efficiency=path_efficiency,
        bullish_body_count=bullish_body_count,
        bullish_body_share=bullish_body_share,
        mean_close_location=mean_close_location,
        mean_overlap=mean_overlap,
        speed_atr_per_session=move_atr / duration,
        median_amount20=median_amount20,
        source_digest=digest,
    )


def _event_snapshot(
    event: BrooksImpulse,
    bars: Sequence[BrooksBar],
    security_states: Sequence[SecurityState],
    as_of_pos: int,
) -> dict[str, Any]:
    signal = event.signal_pos
    touch_pos: int | None = None
    h1_pos: int | None = None
    h1_failure_pos: int | None = None
    h2_pos: int | None = None
    late_h1_pos: int | None = None
    state = STATE_S0
    reason = "IMPULSE_QUALIFIED"
    changed_pos = signal
    terminal = False

    last_touch_scan = min(as_of_pos, signal + 15)
    for pos in range(signal + 1, last_touch_scan + 1):
        if not _bar_structurally_valid(event, bars, pos):
            state = STATE_INVALIDATED
            reason = "STRUCTURAL_FAILURE_BEFORE_TOUCH25"
            changed_pos = pos
            terminal = True
            break
        current = _retracement(event, bars[pos].close)
        maximum = _maximum_retracement(event, bars, pos)
        if touch_pos is None and 0.25 <= current < 1.0 and maximum < 1.0:
            touch_pos = pos
            state = STATE_S1
            reason = "TOUCH25_CONFIRMED"
            changed_pos = pos
        if touch_pos is not None and h1_pos is None and _is_h1_bar(bars, pos):
            h1_pos = pos
            state = STATE_S2
            reason = "H1_CONFIRMED"
            changed_pos = pos
            break

    if not terminal and touch_pos is None and as_of_pos > signal + 15:
        state = STATE_STALE
        reason = "STALE_NO_TOUCH25"
        changed_pos = signal + 16
        terminal = True

    if not terminal and touch_pos is not None and h1_pos is None:
        structural_end = min(as_of_pos, touch_pos + 20)
        for pos in range(last_touch_scan + 1, structural_end + 1):
            if not _bar_structurally_valid(event, bars, pos):
                state = STATE_INVALIDATED
                reason = "STRUCTURAL_FAILURE_AFTER_TOUCH25"
                changed_pos = pos
                terminal = True
                break
            if late_h1_pos is None and _is_h1_bar(bars, pos):
                late_h1_pos = pos
        if not terminal and as_of_pos > touch_pos + 20:
            state = STATE_STALE
            reason = "STALE_NO_H1"
            changed_pos = touch_pos + 21
            terminal = True

    if not terminal and h1_pos is not None:
        h1_anchor = min(bar.low for bar in bars[signal + 1 : h1_pos + 1])
        for pos in range(h1_pos + 1, min(as_of_pos, signal + 30) + 1):
            if bars[pos].low <= event.impulse_start:
                state = STATE_INVALIDATED
                reason = "STRUCTURAL_FAILURE_AFTER_H1"
                changed_pos = pos
                terminal = True
                break
            broke_anchor = bars[pos].low < h1_anchor
            touched_high = bars[pos].high >= event.signal_high
            if h1_failure_pos is None and broke_anchor and touched_high:
                reason = "AMBIGUOUS_H1_FAILURE_AND_PRIOR_HIGH"
                break
            if h1_failure_pos is None and touched_high:
                break
            if h1_failure_pos is None and broke_anchor:
                h1_failure_pos = pos
            if h1_failure_pos is not None and _is_h1_bar(bars, pos):
                h2_pos = pos
                state = STATE_S2_H2
                reason = "H2_RESCUE_TAGGED"
                changed_pos = pos
                break

    decision_pos = h2_pos if h2_pos is not None else h1_pos if h1_pos is not None else touch_pos
    anchor_end = decision_pos if decision_pos is not None else as_of_pos
    anchor = (
        min(bar.low for bar in bars[signal + 1 : anchor_end + 1])
        if anchor_end > signal
        else event.signal_close
    )
    prior_high_touched = False
    prior_high_closed = False
    mm08_completed = False
    mm10_completed = False
    ambiguous_same_bar = False
    mm08 = anchor + 0.8 * event.impulse_height
    mm10 = anchor + event.impulse_height
    if decision_pos is not None:
        path_end = changed_pos if terminal else as_of_pos
        for pos in range(decision_pos + 1, path_end + 1):
            target_hit = bars[pos].high >= event.signal_high or bars[pos].high >= mm08
            failure_hit = bars[pos].low <= event.impulse_start
            if target_hit and failure_hit:
                ambiguous_same_bar = True
            prior_high_touched = prior_high_touched or bars[pos].high >= event.signal_high
            prior_high_closed = prior_high_closed or bars[pos].close >= event.signal_high
            mm08_completed = mm08_completed or bars[pos].high >= mm08
            mm10_completed = mm10_completed or bars[pos].high >= mm10
            if not terminal and failure_hit:
                state = STATE_INVALIDATED
                reason = (
                    "AMBIGUOUS_TARGET_AND_STRUCTURAL_FAILURE"
                    if target_hit
                    else "STRUCTURAL_FAILURE_AFTER_DECISION"
                )
                changed_pos = pos
                terminal = True
                break
            if not terminal and prior_high_touched and mm10_completed:
                state = STATE_COMPLETED
                reason = "PRIMARY_PATHS_COMPLETED"
                changed_pos = pos
                terminal = True
                break
        if not terminal and as_of_pos > decision_pos + 60:
            state = STATE_STALE
            reason = "STALE_PATH_CENSORED_60D"
            changed_pos = decision_pos + 61
            terminal = True

    current_state = security_states[as_of_pos]
    if not terminal and not _security_eligible(current_state):
        state = STATE_PAUSED
        reason = _security_reason(current_state)
        changed_pos = as_of_pos

    current_bar = bars[as_of_pos]
    next_open_status = _next_open_status(
        event.symbol, decision_pos, as_of_pos, bars, security_states
    )
    return {
        "state": state,
        "state_reason": reason,
        "state_rank": STATE_RANKS[state],
        "state_changed_on": bars[changed_pos].trade_date,
        "touch25_date": bars[touch_pos].trade_date if touch_pos is not None else "",
        "h1_date": bars[h1_pos].trade_date if h1_pos is not None else "",
        "h1_failure_date": (bars[h1_failure_pos].trade_date if h1_failure_pos is not None else ""),
        "h2_date": bars[h2_pos].trade_date if h2_pos is not None else "",
        "bars_after_signal": max(0, as_of_pos - signal),
        "bars_after_touch25": max(0, as_of_pos - touch_pos) if touch_pos is not None else 0,
        "current_retracement": _retracement(event, current_bar.close),
        "maximum_low_retracement": _maximum_retracement(event, bars, as_of_pos),
        "pullback_atr": max(0.0, event.signal_close - anchor) / event.atr20,
        "pullback_low_anchor": anchor,
        "prior_high": event.signal_high,
        "mm_0_8": mm08,
        "mm_1_0": mm10,
        "tight_invalidation": anchor,
        "structural_invalidation": event.impulse_start,
        "prior_high_touched": prior_high_touched,
        "prior_high_closed": prior_high_closed,
        "mm_0_8_completed": mm08_completed,
        "mm_1_0_completed": mm10_completed,
        "next_open_status": next_open_status,
        "tradability_reason": reason if state == STATE_PAUSED else "",
        "evidence": {
            "ambiguous_same_bar": ambiguous_same_bar,
            "decision_date": bars[decision_pos].trade_date if decision_pos is not None else "",
            "late_h1_diagnostic_date": (
                bars[late_h1_pos].trade_date if late_h1_pos is not None else ""
            ),
            "current_close": current_bar.close,
        },
    }


def _states_for_bars(
    bars: Sequence[BrooksBar], history: Sequence[SecurityState]
) -> list[SecurityState]:
    if not history:
        return [SecurityState(valid_from="") for _ in bars]
    ordered = sorted(history, key=lambda item: item.valid_from)
    starts = [item.valid_from for item in ordered]
    result: list[SecurityState] = []
    for bar in bars:
        position = bisect_right(starts, bar.trade_date) - 1
        result.append(ordered[position] if position >= 0 else SecurityState(valid_from=""))
    return result


def _signal_eligible(symbol: str, bar: BrooksBar, state: SecurityState) -> bool:
    code = symbol.split(".", 1)[0]
    return (
        symbol.endswith((".SH", ".SZ"))
        and not code.startswith(("4", "8"))
        and _security_eligible(state)
        and not _one_price_limit_up(symbol, bar)
    )


def _security_eligible(state: SecurityState) -> bool:
    return (
        state.listing_status in {"", "listed"}
        and not state.is_st
        and not state.is_suspended
        and state.trading_status.lower() not in {"suspended", "halted"}
    )


def _security_reason(state: SecurityState) -> str:
    if state.is_st:
        return "PAUSED_ST"
    if state.is_suspended or state.trading_status.lower() in {"suspended", "halted"}:
        return "PAUSED_SUSPENDED"
    if state.listing_status not in {"", "listed"}:
        return "PAUSED_NOT_LISTED"
    return "PAUSED_DATA_INSUFFICIENT"


def _one_price_limit_up(symbol: str, bar: BrooksBar) -> bool:
    if (
        max(bar.raw_open, bar.raw_high, bar.raw_low, bar.raw_close)
        - min(bar.raw_open, bar.raw_high, bar.raw_low, bar.raw_close)
        > 1e-8
    ):
        return False
    if not bar.pre_close or bar.pre_close <= 0:
        return False
    code = symbol.split(".", 1)[0]
    threshold = 0.195 if code.startswith(("300", "301", "688")) else 0.095
    return bar.raw_close / bar.pre_close - 1 >= threshold


def _next_open_status(
    symbol: str,
    decision_pos: int | None,
    as_of_pos: int,
    bars: Sequence[BrooksBar],
    states: Sequence[SecurityState],
) -> str:
    if decision_pos is None:
        return "NOT_APPLICABLE"
    if as_of_pos <= decision_pos:
        return "PENDING"
    next_pos = decision_pos + 1
    if next_pos >= len(bars):
        return "PENDING"
    state = states[next_pos]
    if state.is_st:
        return "REJECTED_ST"
    if state.is_suspended:
        return "REJECTED_SUSPENDED"
    if _one_price_limit_up(symbol, bars[next_pos]):
        return "REJECTED_LIMIT_UP"
    return "TRADABLE"


def _prior_atr20(bars: Sequence[BrooksBar]) -> list[float | None]:
    true_ranges: list[float | None] = [None]
    for index in range(1, len(bars)):
        true_ranges.append(
            max(
                bars[index].high - bars[index].low,
                abs(bars[index].high - bars[index - 1].close),
                abs(bars[index].low - bars[index - 1].close),
            )
        )
    result: list[float | None] = [None] * len(bars)
    for index in range(21, len(bars)):
        window = true_ranges[index - 20 : index]
        if all(value is not None for value in window):
            result[index] = fmean(float(value) for value in window if value is not None)
    return result


def _close_location(bar: BrooksBar) -> float:
    span = bar.high - bar.low
    return 0.5 if span <= 0 else (bar.close - bar.low) / span


def _pair_overlap(left: BrooksBar, right: BrooksBar) -> float:
    union = max(left.high, right.high) - min(left.low, right.low)
    if union <= 0:
        return 1.0
    intersection = max(0.0, min(left.high, right.high) - max(left.low, right.low))
    return intersection / union


def _is_h1_bar(bars: Sequence[BrooksBar], pos: int) -> bool:
    return (
        pos > 0
        and bars[pos].close > bars[pos].open
        and _close_location(bars[pos]) >= 0.65
        and bars[pos].high > bars[pos - 1].high
    )


def _bar_structurally_valid(event: BrooksImpulse, bars: Sequence[BrooksBar], pos: int) -> bool:
    return (
        bars[pos].close > event.impulse_start
        and min(bar.low for bar in bars[event.signal_pos + 1 : pos + 1]) > event.impulse_start
    )


def _retracement(event: BrooksImpulse, close: float) -> float:
    return max(0.0, event.signal_close - close) / event.impulse_height


def _maximum_retracement(event: BrooksImpulse, bars: Sequence[BrooksBar], pos: int) -> float:
    if pos <= event.signal_pos:
        return 0.0
    anchor = min(bar.low for bar in bars[event.signal_pos + 1 : pos + 1])
    return max(0.0, event.signal_close - anchor) / event.impulse_height


def _fact_digest(event: BrooksImpulse, as_of_date: str, snapshot: Mapping[str, Any]) -> str:
    payload = json.dumps(
        {"event": event.source_digest, "as_of_date": as_of_date, "snapshot": snapshot},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def _optional_float(value: Any) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None
