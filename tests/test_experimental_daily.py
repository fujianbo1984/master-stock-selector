from __future__ import annotations

import math
import sqlite3
from datetime import date, timedelta

import pytest

from master_stock_selector.watchlist.experimental import read_snapshot
from master_stock_selector.watchlist.experimental_daily import (
    calculate_features,
    initialize,
    run_experiment,
)


def sample():
    bars = []
    for i in range(61):
        close = 10 + i * 0.01 + (0.05 if i % 2 else 0)
        bars.append(
            dict(
                open=close,
                high=close * 1.16,
                low=close * 0.86,
                close=close,
                af=1.0,
                volume=100.0,
                amount=60000.0,
                turnover=5.0,
                up=20.0,
                down=5.0,
            )
        )
    bars[10]["up"] = bars[10]["close"]
    return bars, [10.0] * 61


def test_feature_windows_and_factor_scale():
    bars, index = sample()
    feature, reason = calculate_features(bars, index, mature=True, is_st=False)
    assert reason == "eligible"
    assert feature["limit60"] == 1
    assert feature["volume_dry"] == 1
    assert feature["rs20"] == pytest.approx(bars[-1]["close"] / bars[-21]["close"] - 1)
    scaled = [dict(b, af=2.0) for b in bars]
    assert calculate_features(scaled, index, mature=True, is_st=False)[0] == feature
    bars[-1]["up"] = bars[-1]["close"]
    assert calculate_features(bars, index, mature=True, is_st=False)[1] == "recent_acceleration"


@pytest.mark.parametrize(
    "change,reason",
    [("missing", "history"), ("suspended", "history"), ("limit", "limits"), ("index", "benchmark")],
)
def test_missing_data_never_becomes_signal(change, reason):
    bars, index = sample()
    if change == "missing":
        bars[-2] = {}
    if change == "suspended":
        bars[-2]["suspended"] = True
    if change == "limit":
        bars[0]["up"] = None
    if change == "index":
        index[-1] = math.nan
    assert calculate_features(bars, index, mature=True, is_st=False)[1] == reason


def seed(tmp_path):
    market = tmp_path / "market.sqlite3"
    days = [(date(2025, 1, 1) + timedelta(days=i)).isoformat() for i in range(125)]
    with sqlite3.connect(market) as c:
        c.executescript("""
        CREATE TABLE daily_bars(market,symbol,trade_date,adj_type,open,high,low,close,volume,amount);
        CREATE TABLE daily_adjustment_factors(market,symbol,trade_date,adj_factor);
        CREATE TABLE daily_metrics(market,symbol,trade_date,turnover_rate);
        CREATE TABLE security_master_history(symbol,valid_from,name,list_date);
        CREATE TABLE market_index_daily_bars(index_symbol,trade_date,close,fetched_at,batch_id);
        """)
        for i, d in enumerate(days):
            close = 10 + i * 0.01 + (0.05 if i % 2 else 0)
            c.execute(
                "INSERT INTO daily_bars VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    "ashare",
                    "000001.SZ",
                    d,
                    "raw",
                    close,
                    close * 1.16,
                    close * 0.86,
                    close,
                    100.0,
                    60000.0,
                ),
            )
            c.execute(
                "INSERT INTO daily_adjustment_factors VALUES (?,?,?,?)",
                ("ashare", "000001.SZ", d, 1.0),
            )
            c.execute("INSERT INTO daily_metrics VALUES (?,?,?,?)", ("ashare", "000001.SZ", d, 5.0))
            c.execute(
                "INSERT INTO market_index_daily_bars VALUES (?,?,?,?,?)",
                ("000985.CSI", d, 10.0, d, "test"),
            )
        c.execute(
            "INSERT INTO security_master_history VALUES (?,?,?,?)",
            ("000001.SZ", days[0], "测试股票", "2020-01-01"),
        )
    return market, days


class Provider:
    def __init__(self, days):
        self.days = days
        self.calls = 0
        self.empty_st = False

    def assert_ready(self):
        pass

    def trade_calendar(self, start_date, end_date):
        return [d for d in self.days if start_date <= d <= end_date]

    def stock_limits(self, day):
        self.calls += 1
        return {"000001.SZ": dict(trade_date=day, up_limit=20.0, down_limit=5.0)}

    def stock_suspensions(self, day):
        return []

    def stock_st(self, day):
        return [] if self.empty_st else [dict(trade_date=day, ts_code="000002.SZ")]


def test_daily_incremental_retry_gap_and_immutable_market(tmp_path):
    market, days = seed(tmp_path)
    original = market.read_bytes()
    output = tmp_path / "experiment.sqlite3"
    initialize(output)
    provider = Provider(days)
    result = run_experiment(market, output, days[-3], provider=provider, minimum=1)
    assert result["counts"] == {"selected": 0, "reference": 1}
    assert provider.calls == 61
    assert (
        run_experiment(market, output, days[-3], provider=provider, minimum=1)["state"] == "REUSED"
    )
    assert provider.calls == 61
    provider.empty_st = True
    with pytest.raises(ValueError, match="ST"):
        run_experiment(market, output, days[-2], provider=provider, minimum=1)
    snap = read_snapshot(output, None, "reference")
    assert snap["latest_date"] == days[-3] and snap["failed_date"] == days[-2]
    provider.empty_st = False
    run_experiment(market, output, days[-1], provider=provider, minimum=1)
    assert provider.calls == 63
    snap = read_snapshot(output, None, "reference")
    assert snap["daily_mode"] and not snap["failed_date"]
    assert snap["rows"][0]["first_seen"] == days[-3]
    assert read_snapshot(output, days[-2], "reference")["available"]
    assert market.read_bytes() == original


def test_short_market_history_does_not_publish_empty_success(tmp_path):
    market, days = seed(tmp_path)
    output = tmp_path / "experiment.sqlite3"
    initialize(output)
    with sqlite3.connect(market) as con:
        con.execute("DELETE FROM daily_bars WHERE trade_date=?", (days[-10],))
    with pytest.raises(ValueError, match="原价历史不足"):
        run_experiment(market, output, days[-1], provider=Provider(days), minimum=1)
    assert not read_snapshot(output, None, "selected")["available"]


def test_closed_loop_runs_experiment_even_when_formal_facts_reused(tmp_path, monkeypatch):
    from master_stock_selector.watchlist import daily
    from master_stock_selector.watchlist.daily import DailyRunConfig

    class Repo:
        def __init__(self, path):
            pass

        def brooks_schema_ready(self):
            return True

        def latest_fact_date(self):
            return date.today().isoformat()

        def latest_brooks_run_date(self, policy):
            return date.today().isoformat()

    monkeypatch.setattr(daily, "WatchlistRepository", Repo)
    monkeypatch.setattr(daily, "climax_ready", lambda path: True)
    monkeypatch.setattr(daily, "collect_market_data", lambda *a, **kw: {"status": "SUCCESS"})
    monkeypatch.setattr(daily, "run_climax", lambda *a: {"status": "SUCCESS"})
    called = []
    monkeypatch.setattr(
        daily, "run_experiment", lambda *a, **kw: called.append((a, kw)) or {"status": "SUCCESS"}
    )
    result = daily.run_daily_closed_loop(
        DailyRunConfig(tmp_path / "market", tmp_path / "watchlist")
    )
    assert result["experimental"]["status"] == "SUCCESS"
    assert result["selection"]["state"] == "REUSED"
    assert len(called) == 1
    monkeypatch.setattr(daily, "collect_market_data", lambda *a, **kw: {"status": "SKIPPED"})
    assert (
        daily.run_daily_closed_loop(DailyRunConfig(tmp_path / "market", tmp_path / "watchlist"))[
            "status"
        ]
        == "SKIPPED"
    )
    assert len(called) == 1


def test_daily_web_origin_and_failed_day(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from tests.test_watchlist_web import create_app

    market, days = seed(tmp_path)
    output = tmp_path / "experiment.sqlite3"
    initialize(output)
    provider = Provider(days)
    run_experiment(market, output, days[-2], provider=provider, minimum=1)
    monkeypatch.setenv("MASTERSTOCK_EXPERIMENT_DATABASE", str(output))
    app = create_app(
        market_database=market,
        watchlist_database=tmp_path / "watchlist.sqlite3",
        access_logging=False,
    )
    with TestClient(app) as client:
        text = client.get("/a/experimental?model=reference").text
        assert "服务器行情 · 每日收盘计算" in text and "测试股票" in text
        provider.empty_st = True
        with pytest.raises(ValueError):
            run_experiment(market, output, days[-1], provider=provider, minimum=1)
        text = client.get("/a/experimental?model=reference").text
        assert f"{days[-1]} 实验计算未完成" in text and "测试股票" in text
