from __future__ import annotations

import copy
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from master_stock_selector.watchlist.charting import default_keltner_preset, keltner_channels
from master_stock_selector.watchlist.climax import POLICY_VERSION, _track, build_climax
from master_stock_selector.watchlist.climax_service import (
    _pack,
    _verify_prefix,
    chart_climax,
    migrate,
)


def fixture(count: int = 165) -> tuple[list[str], dict[str, Any], dict[str, Any]]:
    dates = [(date(2025, 1, 1) + timedelta(days=i)).isoformat() for i in range(count)]
    bars, meta = {}, {}
    for i, d in enumerate(dates):
        close = 10 * 1.005**i if i < 140 else 10 * 1.005**139 * 1.035 ** (i - 139)
        bars[d] = dict(
            trade_date=d,
            open=close * 0.996,
            high=close * 1.003,
            low=close * 0.994,
            close=close,
            amount=30000,
        )
        meta[d] = dict(active=True, st_known=True, is_st=False, suspended=False, total_mv=1e6)
    return dates, bars, meta


def test_onset_and_full_prefix_replay_and_system_kc() -> None:
    dates, bars, meta = fixture()
    full = build_climax("600000.SH", dates, bars, meta)
    assert full["events"]
    _, preset = default_keltner_preset("day")
    kc = keltner_channels(
        list(bars.values()),
        **{
            k: preset[k]
            for k in ("length", "atr_length", "multiplier", "source", "use_ema", "band_style")
        },
    )
    for i in range(139, len(dates)):
        prefix = build_climax("600000.SH", dates[: i + 1], bars, meta)
        _verify_prefix(prefix, full)
        m = full["daily"][i]["metrics"]
        assert m["free_bars"] == sum(
            bars[dates[j]]["low"] > kc[j]["upper"] for j in range(i - 4, i + 1)
        )
    # Persistent true condition never creates a second onset at cooldown expiry.
    assert len(full["events"]) == 1
    t = dates.index(full["events"][0]["date"])

    def tr(j: int) -> float:
        return max(
            bars[dates[j]]["high"] - bars[dates[j]]["low"],
            abs(bars[dates[j]]["high"] - bars[dates[j - 1]]["close"]),
            abs(bars[dates[j]]["low"] - bars[dates[j - 1]]["close"]),
        )

    assert full["events"][0]["evidence"]["atr"] == pytest.approx(
        sum(tr(j) for j in range(t - 20, t)) / 20
    )
    changed = copy.deepcopy(full)
    changed["daily"][t]["metrics"]["close"] += 1
    with pytest.raises(ValueError, match="historical inputs changed"):
        _verify_prefix(full, changed)


def test_missing_qualification_and_suspension_do_not_create_candidates() -> None:
    dates, bars, meta = fixture()
    for m in meta.values():
        m["st_known"] = False
    result = build_climax("600000.SH", dates, bars, meta)
    assert not result["events"]
    assert result["daily"][-1]["potential"] is None
    dates, bars, meta = fixture()
    del bars[dates[135]]
    meta[dates[135]]["suspended"] = True
    result = build_climax("600000.SH", dates, bars, meta)
    assert not result["events"]
    assert result["daily"][135]["status"] == "SUSPENDED"


def track_fixture() -> tuple[dict[str, Any], list[str], list[Any], list[str]]:
    dates = [f"2025-06-{i:02}" for i in range(1, 13)]
    rows = [dict(open=10, high=10.1, low=9.9, close=10) for _ in dates]
    event = dict(evidence={"atr": 1}, timeline=[])
    return event, dates, rows, ["VALID"] * len(dates)


def test_confirmation_requires_next_market_day_and_keeps_both_types() -> None:
    event, dates, rows, statuses = track_fixture()
    rows[1] = dict(open=10, high=10.1, low=8.8, close=9)
    rows[2] = dict(open=9, high=9.1, low=8.6, close=8.7)
    _track(event, 0, dates, rows, statuses)
    confirms = [p for p in event["timeline"] if p["kind"].endswith("_CONFIRMED")]
    assert {p["kind"] for p in confirms} == {"FAILURE_CONFIRMED", "MOMENTUM_CONFIRMED"}
    assert all(p["date"] == dates[2] for p in confirms)
    # A suspension cannot be skipped to obtain next-day confirmation.
    event["timeline"] = []
    statuses[2], rows[2] = "SUSPENDED", None
    _track(event, 0, dates, rows, statuses)
    assert not any(p["kind"].endswith("_CONFIRMED") for p in event["timeline"])
    event["timeline"] = []
    statuses[2] = "UNKNOWN"
    _track(event, 0, dates, rows, statuses)
    assert event["timeline"][-1]["kind"] == "UNKNOWN"


def test_tenth_day_attempt_cannot_confirm_on_eleventh() -> None:
    event, dates, rows, statuses = track_fixture()
    rows[10] = dict(open=10, high=10.1, low=8.8, close=9)
    rows[11] = dict(open=9, high=9.1, low=8.5, close=8.6)
    _track(event, 0, dates, rows, statuses)
    assert any(p["kind"].endswith("_ATTEMPT") for p in event["timeline"])
    assert not any(p["kind"].endswith("_CONFIRMED") for p in event["timeline"])
    assert event["timeline"][-1]["kind"] == "WINDOW_ENDED"


def test_chart_asof_scale_and_missing_schema(tmp_path: Path) -> None:
    path = tmp_path / "climax.sqlite3"
    assert chart_climax(path, "600000.SH", "2025-06-01", [])["status"] == "NOT_COMPUTED"
    migrate(path)
    dates, bars, meta = fixture()
    document = build_climax("600000.SH", dates, bars, meta)
    with sqlite3.connect(path) as c:
        c.execute(
            "INSERT INTO climax_stock_document VALUES (?,?,?,?)",
            (POLICY_VERSION, "600000.SH", dates[-1], _pack(document)),
        )
    cutoff = document["events"][0]["date"]
    scaled = [{**b, "close": b["close"] / 7} for d, b in bars.items() if d <= cutoff]
    layer = chart_climax(path, "600000.SH", cutoff, scaled)
    assert layer["current"]["date"] == cutoff
    assert layer["events"][0]["evidence"]["close"] == pytest.approx(bars[cutoff]["close"] / 7)
    assert all(p["date"] <= cutoff for e in layer["events"] for p in e["timeline"])
    assert layer["events"][0]["timeline"] == [
        dict(date=cutoff, kind="ONSET", close=pytest.approx(bars[cutoff]["close"] / 7))
    ]


def test_batch_append_reuse_and_atomic_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from master_stock_selector.watchlist import climax_service as service

    market, output = tmp_path / "market.sqlite3", tmp_path / "facts.sqlite3"
    dates, bars, _ = fixture()
    with sqlite3.connect(market) as c:
        c.executescript("""
        CREATE TABLE daily_bars(market TEXT, symbol TEXT, trade_date TEXT, open REAL, high REAL, low REAL, close REAL, amount REAL, adj_type TEXT);
        CREATE TABLE daily_adjustment_factors(market TEXT,symbol TEXT,trade_date TEXT,adj_factor REAL);
        CREATE TABLE daily_metrics(market TEXT,symbol TEXT,trade_date TEXT,total_mv REAL);
        CREATE TABLE security_master_history(symbol TEXT,valid_from TEXT,listing_status_as_of TEXT,list_date TEXT,delist_date TEXT,is_suspended INT,is_st INT);
        CREATE TABLE security_st_daily_fact(trade_date TEXT,symbol TEXT);
        """)
        for symbol in ("600000.SH", "600001.SH"):
            c.execute(
                "INSERT INTO security_master_history VALUES (?,?,'listed',?,'',0,0)",
                (symbol, dates[0], dates[0]),
            )
            for d, b in bars.items():
                for adj in ("raw", "qfq"):
                    c.execute(
                        "INSERT INTO daily_bars VALUES ('ashare',?,?,?,?,?,?,?,?)",
                        (symbol, d, b["open"], b["high"], b["low"], b["close"], b["amount"], adj),
                    )
                c.execute(
                    "INSERT INTO daily_adjustment_factors VALUES ('ashare',?,?,1)", (symbol, d)
                )
                c.execute("INSERT INTO daily_metrics VALUES ('ashare',?,?,1000000)", (symbol, d))
        c.executemany(
            "INSERT INTO security_st_daily_fact VALUES (?,'600099.SH')", [(d,) for d in dates]
        )
    migrate(output)
    first = service.run_climax(market, output, dates[145])
    assert first["counts"]["symbols"] == 2
    assert service.run_climax(market, output, dates[145])["status"] == "REUSED"
    original = service.build_climax

    def fail_second(symbol: str, *args: Any) -> dict[str, Any]:
        if symbol == "600001.SH":
            raise RuntimeError("injected batch failure")
        return original(symbol, *args)

    monkeypatch.setattr(service, "build_climax", fail_second)
    with pytest.raises(RuntimeError, match="injected"):
        service.run_climax(market, output, dates[150])
    with sqlite3.connect(output) as c:
        assert c.execute("SELECT DISTINCT as_of_date FROM climax_stock_document").fetchall() == [
            (dates[145],)
        ]
        assert c.execute("SELECT count(*) FROM climax_run").fetchone()[0] == 1
    monkeypatch.setattr(service, "build_climax", original)
    assert service.run_climax(market, output, dates[-1])["status"] == "SUCCESS"
    assert (
        chart_climax(output, "600000.SH", dates[145], list(bars.values())[:146])["current"]["date"]
        == dates[145]
    )
