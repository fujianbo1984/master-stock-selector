from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, timedelta

import numpy as np
import pytest
from fastapi.testclient import TestClient

from master_stock_selector.watchlist.experimental import KEYS, SELECTED, read_snapshot, select_rows
from master_stock_selector.watchlist.experimental_import import import_panel
from tests.test_watchlist_web import create_app


def row(symbol="000001.SZ", **changes):
    return dict(symbol=symbol, eligible=True, **(dict(zip(KEYS, SELECTED)) | changes))


def test_frozen_thresholds_score_and_reference():
    actual = select_rows([row()])
    assert actual[0]["score"] == pytest.approx(4.25)
    assert actual[0]["rank"] == 1
    weaker = row(volume_dry=0.5, rs20=-0.1, limit60=0, up_down_amount=0.5)
    assert select_rows([weaker]) == []
    assert len(select_rows([weaker], "reference")) == 1


@pytest.mark.parametrize("changes", [
    {"turnover": 3.99}, {"turnover": 20.01}, {"volume_dry": 1.299},
    {"up_down_amount": 0.999}, {"rs20": 0.0499}, {"limit60": 0},
    {"range20": 0.2499}, {"range20": 0.8001}, {"rs20": float("nan")},
    {"volume_dry": float("inf")}, {"turnover": None},
])
def test_reject_outside_bounds_or_unknown(changes):
    assert select_rows([row(**changes)]) == []


def test_no_implicit_eligibility_and_no_duplicates():
    candidate = row()
    candidate["eligible"] = "true"
    assert select_rows([candidate]) == []
    with pytest.raises(ValueError, match="Duplicate"):
        select_rows([row(), row()])
    with pytest.raises(ValueError):
        select_rows([row()], "optimized")


def test_cap_ties_and_score_clipping():
    rows = [row(f"{i:06d}.SZ", turnover=12, volume_dry=99, up_down_amount=99,
                rs20=1, limit60=20, range20=0.80) for i in range(25, 0, -1)]
    result = select_rows(rows)
    assert len(result) == 20
    assert result[0]["symbol"] == "000001.SZ"
    assert result[-1]["symbol"] == "000020.SZ"
    assert all(r["score"] == 12 for r in result)


def build_fixture(tmp_path, *, mutate=False):
    panel = tmp_path / "panel.npz"
    dates = np.array([(date(2026, 1, 1) + timedelta(days=i)).isoformat() for i in range(23)])
    # Market-session indices, intentionally independent of elapsed calendar days.
    np.savez(panel, F=np.tile(SELECTED, (22, 1)), si=np.zeros(22, dtype=int),
             ti=np.arange(22), dates=dates, symbols=np.array(["000001.SZ"]),
             names=np.array(["测试股票"]), label50=np.full(22, 999 if mutate else 0))
    digest = hashlib.sha256(panel.read_bytes()).hexdigest()
    lock = tmp_path / "lock.json"
    audit = tmp_path / "audit.json"
    lock.write_text(json.dumps(dict(selected_grid_id=190, parameters=dict(zip(KEYS, SELECTED)),
                                    source_panel_sha256=digest)))
    audit.write_text(json.dumps(dict(panel_sha256=digest, rows=22, calendar_end=str(dates[-1]))))
    output = tmp_path / "experimental_watchlist.sqlite3"
    return panel, lock, audit, output


def test_import_first_seen_cooldown_empty_and_exact_date(tmp_path):
    paths = build_fixture(tmp_path)
    import_panel(*paths)
    snap = read_snapshot(paths[-1], "2026-01-21", "selected")
    assert snap["rows"][0]["first_seen"] == "2026-01-01"
    assert snap["rows"][0]["episode_start"] == "2026-01-01"
    assert not snap["rows"][0]["new_episode"]
    assert read_snapshot(paths[-1], "2026-01-22", "selected")["rows"][0]["new_episode"]
    empty = read_snapshot(paths[-1], None, "selected")
    assert empty["available"] and empty["rows"] == []
    assert empty["query_date"] == "2026-01-23"
    assert not read_snapshot(paths[-1], "2026-02-01", "selected")["available"]
    assert not read_snapshot(tmp_path / "missing", None, "selected")["available"]
    with pytest.raises(FileExistsError):
        import_panel(*paths)


def test_import_rejects_wrong_hash_and_changed_lock(tmp_path):
    paths = build_fixture(tmp_path)
    lock = json.loads(paths[1].read_text())
    lock["source_panel_sha256"] = "wrong"
    paths[1].write_text(json.dumps(lock))
    with pytest.raises(ValueError, match="fingerprint"):
        import_panel(*paths)
    assert not paths[-1].exists()
    lock["parameters"]["rs20"] = 0
    paths[1].write_text(json.dumps(lock))
    with pytest.raises(ValueError, match="190"):
        import_panel(*paths)


def test_future_labels_cannot_change_selections(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    snapshots = []
    for folder, mutate in [(a, False), (b, True)]:
        paths = build_fixture(folder, mutate=mutate)
        import_panel(*paths)
        with sqlite3.connect(paths[-1]) as con:
            snapshots.append(con.execute("SELECT * FROM observations ORDER BY date,model,rank").fetchall())
    assert snapshots[0] == snapshots[1]


def test_website_versions_dates_navigation_and_missing_data(tmp_path, monkeypatch):
    paths = build_fixture(tmp_path)
    import_panel(*paths)
    monkeypatch.setenv("MASTERSTOCK_EXPERIMENT_DATABASE", str(paths[-1]))
    app = create_app(market_database=tmp_path / "market.sqlite3",
                     watchlist_database=tmp_path / "watchlist.sqlite3", access_logging=False)
    with TestClient(app) as client:
        for model in ("selected", "reference"):
            response = client.get(f"/a/experimental?date=2026-01-21&model={model}")
            assert response.status_code == 200
            assert "测试股票" in response.text
            assert "/a/stocks/000001.SZ/chart?date=2026-01-21" in response.text
            assert "2026-01-01" in response.text
            assert "历史回填，非实时行情" in response.text
        assert "所选日期没有实验数据" in client.get("/a/experimental?date=2026-02-01").text
        assert "没有股票满足" in client.get("/a/experimental").text
        assert client.get("/a/experimental?date=bad").status_code == 422
        assert client.get("/a/experimental?model=optimized").status_code == 422
        assert 'href="/a/experimental"' in client.get("/a/discover").text
        paths[-1].unlink()
        assert "实验数据尚未准备" in client.get("/a/experimental").text
