from __future__ import annotations

import sqlite3

import pytest

from master_stock_selector.web.users import SESSION_COOKIE, ApiTokenPrincipal
from tests.test_watchlist_web import _client, _csrf


def _principal(client):
    users = client.app.state.user_repository
    user = users.session_user(client.cookies.get(SESSION_COOKIE))
    assert user is not None
    return users, user


def _token_headers(token: str, *, key: str = "") -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if key:
        headers["Idempotency-Key"] = key
    return headers


def test_user_creates_one_time_agent_token_and_can_revoke_it(tmp_path) -> None:
    client = _client(tmp_path)

    page = client.get("/account/tokens")
    assert page.status_code == 200
    assert "Agent Token" in page.text
    assert "三步连接 Agent" in page.text
    assert "trades validate trades.json" in page.text
    assert 'aria-current="page"' in page.text
    created = client.post(
        "/account/tokens",
        data={
            "csrf_token": _csrf(client),
            "name": "Codex Agent",
            "expires_days": "90",
            "trades:read": "on",
            "trades:write": "on",
        },
    )

    assert created.status_code == 200
    assert "复制并妥善保存新 Token" in created.text
    assert "复制 Token" in created.text
    users, user = _principal(client)
    tokens = users.list_api_tokens(user.user_id)
    assert len(tokens) == 1
    assert tokens[0]["active"] is True
    with users.connect() as connection:
        stored = connection.execute(
            "SELECT token_hash FROM user_api_token WHERE token_id=?",
            (tokens[0]["token_id"],),
        ).fetchone()[0]
    assert "mst_" not in stored

    revoked = client.post(
        f"/account/tokens/{tokens[0]['token_id']}/revoke",
        data={"csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert revoked.status_code == 303
    assert users.list_api_tokens(user.user_id)[0]["active"] is False


def test_agent_api_preflight_atomic_commit_idempotency_and_stop_update(tmp_path) -> None:
    client = _client(tmp_path)
    users, user = _principal(client)
    token, _ = users.create_api_token(user.user_id, "integration")
    headers = _token_headers(token)
    payload = {
        "trades": [
            {
                "client_id": "sell",
                "symbol": "000001.SZ",
                "traded_on": "2026-08-02",
                "traded_at": "14:00:00",
                "side": "SELL",
                "quantity": 40,
                "price": 12.0,
            },
            {
                "client_id": "buy",
                "symbol": "000001.SZ",
                "traded_on": "2026-08-01",
                "traded_at": "09:30:00",
                "side": "BUY",
                "quantity": 100,
                "price": 10.0,
            },
        ]
    }

    me = client.get("/api/v1/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["username"] == "tester"
    checked = client.post("/api/v1/trades/validate", headers=headers, json=payload)
    assert checked.status_code == 200
    assert checked.json()["status"] == "VALID"
    assert checked.json()["ready"] == 2

    committed = client.post(
        "/api/v1/trades/batch",
        headers=_token_headers(token, key="screenshot-1"),
        json=payload,
    )
    assert committed.status_code == 200
    receipt = committed.json()
    assert receipt["created"] == 2
    assert receipt["replayed"] is False
    replayed = client.post(
        "/api/v1/trades/batch",
        headers=_token_headers(token, key="screenshot-1"),
        json=payload,
    )
    assert replayed.status_code == 200
    assert replayed.json()["batch_id"] == receipt["batch_id"]
    assert replayed.json()["replayed"] is True

    listed = client.get("/api/v1/trades", headers=headers)
    assert listed.status_code == 200
    assert listed.json()["count"] == 2
    buy = next(item for item in listed.json()["trades"] if item["side"] == "BUY")
    sell = next(item for item in listed.json()["trades"] if item["side"] == "SELL")
    changed = client.patch(
        f"/api/v1/trades/{buy['execution_id']}/stop",
        headers=headers,
        json={"stop_price": 9.0, "expected_revision": buy["revision"]},
    )
    assert changed.status_code == 200
    assert changed.json()["trade"]["stop_price"] == 9.0
    assert changed.json()["trade"]["quantity"] == 100
    assert changed.json()["revision"] == buy["revision"] + 1
    stale = client.patch(
        f"/api/v1/trades/{buy['execution_id']}/stop",
        headers=headers,
        json={"stop_price": 8.5, "expected_revision": buy["revision"]},
    )
    assert stale.status_code == 409
    assert client.get(
        f"/api/v1/trades/{buy['execution_id']}", headers=headers
    ).json()["trade"]["stop_price"] == 9.0
    rejected_sell = client.patch(
        f"/api/v1/trades/{sell['execution_id']}/stop",
        headers=headers,
        json={"stop_price": 11.0, "expected_revision": sell["revision"]},
    )
    assert rejected_sell.status_code == 422
    assert "既有 BUY" in rejected_sell.json()["detail"]

    duplicate = client.post("/api/v1/trades/validate", headers=headers, json=payload)
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicates"] == 2
    assert duplicate.json()["ready"] == 0
    same_trade_different_time = client.post(
        "/api/v1/trades/validate",
        headers=headers,
        json={
            "trades": [
                {
                    "symbol": "000001.SZ",
                    "traded_on": "2026-08-01",
                    "traded_at": "10:00:00",
                    "side": "BUY",
                    "quantity": 100,
                    "price": 10.0,
                }
            ]
        },
    )
    assert same_trade_different_time.json()["duplicates"] == 0
    assert same_trade_different_time.json()["ready"] == 1
    committed_later = client.post(
        "/api/v1/trades/batch",
        headers=_token_headers(token, key="same-price-later-time"),
        json={
            "trades": [{
                "symbol": "000001.SZ",
                "traded_on": "2026-08-01",
                "traded_at": "10:00:00",
                "side": "BUY",
                "quantity": 100,
                "price": 10.0,
            }]
        },
    )
    assert committed_later.status_code == 200
    assert client.get("/api/v1/trades", headers=headers).json()["count"] == 3


def test_agent_api_rejects_oversell_atomically_and_enforces_scopes(tmp_path) -> None:
    client = _client(tmp_path)
    users, user = _principal(client)
    write_token, write_metadata = users.create_api_token(
        user.user_id, "writer", scopes=("trades:write",)
    )
    read_token, _ = users.create_api_token(
        user.user_id, "reader", scopes=("trades:read",)
    )
    payload = {
        "trades": [
            {
                "symbol": "000001.SZ",
                "traded_on": "2026-08-01",
                "side": "BUY",
                "quantity": 100,
                "price": 10,
            },
            {
                "symbol": "000001.SZ",
                "traded_on": "2026-08-02",
                "side": "SELL",
                "quantity": 101,
                "price": 11,
            },
        ]
    }

    denied = client.post(
        "/api/v1/trades/validate", headers=_token_headers(read_token), json=payload
    )
    assert denied.status_code == 403
    rejected = client.post(
        "/api/v1/trades/batch",
        headers=_token_headers(write_token, key="oversell-1"),
        json=payload,
    )
    assert rejected.status_code == 422
    assert rejected.json()["rejected"] == 1
    assert client.get(
        "/api/v1/trades", headers=_token_headers(write_token)
    ).status_code == 403
    with users.connect(read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM user_trade_execution").fetchone()[0] == 0

    assert users.revoke_api_token(user.user_id, str(write_metadata["token_id"]))
    invalid = client.get("/api/v1/me", headers=_token_headers(write_token))
    assert invalid.status_code == 401

    with users.connect() as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT COUNT(*) FROM user_api_audit").fetchone()[0] >= 1


@pytest.mark.parametrize("field", ["price", "fee", "stop_price"])
@pytest.mark.parametrize("invalid", ["NaN", "Infinity", "-Infinity", "1e309"])
def test_agent_api_rejects_non_finite_trade_numbers_atomically(
    tmp_path, field: str, invalid: str
) -> None:
    client = _client(tmp_path)
    users, user = _principal(client)
    token, _ = users.create_api_token(user.user_id, "finite-values")
    values = {
        "price": "10.0",
        "fee": "1.0",
        "stop_price": "9.0",
    }
    values[field] = invalid
    body_template = (
        '{"trades":[{"symbol":"000001.SZ","traded_on":"2026-08-01",'
        '"traded_at":"09:30:00","side":"BUY","quantity":100,'
        '"price":__PRICE__,"fee":__FEE__,"stop_price":__STOP__}]}'
    )
    body = (
        body_template.replace("__PRICE__", values["price"])
        .replace("__FEE__", values["fee"])
        .replace("__STOP__", values["stop_price"])
    )

    response = client.post(
        "/api/v1/trades/batch",
        headers={
            **_token_headers(token, key=f"finite-{field}-{invalid}"),
            "Content-Type": "application/json",
        },
        content=body,
    )

    assert response.status_code == 422
    with users.connect(read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM user_trade_execution").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM user_trade_batch").fetchone()[0] == 0


def test_source_ref_deduplicates_exact_execution_and_rejects_conflicting_reuse(tmp_path) -> None:
    client = _client(tmp_path)
    users, user = _principal(client)
    token, _ = users.create_api_token(user.user_id, "source-ref")
    trade = {
        "source_ref": "broker-order-001",
        "symbol": "000001.SZ",
        "traded_on": "2026-08-01",
        "traded_at": "09:30:00",
        "side": "BUY",
        "quantity": 100,
        "price": 10.0,
    }
    first = client.post(
        "/api/v1/trades/batch",
        headers=_token_headers(token, key="source-first"),
        json={"trades": [trade]},
    )
    assert first.status_code == 200
    duplicate = client.post(
        "/api/v1/trades/batch",
        headers=_token_headers(token, key="source-retry"),
        json={"trades": [trade]},
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicates"] == 1
    conflicting = client.post(
        "/api/v1/trades/batch",
        headers=_token_headers(token, key="source-conflict"),
        json={
            "trades": [
                {
                    "source_ref": "broker-order-002",
                    "symbol": "000001.SZ",
                    "traded_on": "2026-08-01",
                    "traded_at": "10:00:00",
                    "side": "BUY",
                    "quantity": 100,
                    "price": 10.0,
                },
                {**trade, "traded_at": "14:00:00"},
            ]
        },
    )
    assert conflicting.status_code == 422
    with users.connect(read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM user_trade_execution").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM user_trade_batch").fetchone()[0] == 2


def test_write_only_token_receives_minimal_receipts_and_audit_failure_rolls_back(tmp_path) -> None:
    client = _client(tmp_path)
    users, user = _principal(client)
    token, metadata = users.create_api_token(
        user.user_id, "writer-only", scopes=("trades:write",)
    )
    payload = {
        "trades": [
            {
                "symbol": "000001.SZ",
                "traded_on": "2026-08-01",
                "traded_at": "09:30:00",
                "side": "BUY",
                "quantity": 100,
                "price": 10.0,
            }
        ]
    }
    validated = client.post(
        "/api/v1/trades/validate", headers=_token_headers(token), json=payload
    )
    assert validated.status_code == 200
    assert "symbol" not in validated.json()["results"][0]
    assert "price" not in validated.json()["results"][0]
    committed = client.post(
        "/api/v1/trades/batch",
        headers=_token_headers(token, key="write-only"),
        json=payload,
    )
    assert committed.status_code == 200
    assert "symbol" not in committed.json()["results"][0]
    assert "price" not in committed.json()["results"][0]
    assert committed.json()["results"][0]["revision"] == 1
    execution_id = committed.json()["results"][0]["execution_id"]
    stop = client.patch(
        f"/api/v1/trades/{execution_id}/stop",
        headers=_token_headers(token),
        json={"stop_price": 9.0, "expected_revision": 1},
    )
    assert stop.status_code == 200
    assert stop.json()["revision"] == 2
    assert "trade" not in stop.json()
    assert client.get("/api/v1/trades", headers=_token_headers(token)).status_code == 403

    invalid_principal = ApiTokenPrincipal(
        user_id=user.user_id,
        username=user.username,
        display_name=user.display_name,
        token_id="missing-token",
        scopes=("trades:write",),
    )
    with pytest.raises(sqlite3.IntegrityError):
        users.record_trade_batch(
            user.user_id,
            [{**payload["trades"][0], "traded_at": "10:00:00"}],
            idempotency_key="audit-must-commit",
            principal=invalid_principal,
            request_id="audit-failure",
        )
    with pytest.raises(sqlite3.IntegrityError):
        users.update_trade_stop(
            user.user_id,
            execution_id,
            8.5,
            expected_revision=2,
            principal=invalid_principal,
            request_id="stop-audit-failure",
        )
    with users.connect(read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM user_trade_execution").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM user_trade_batch").fetchone()[0] == 1
        stored_stop = connection.execute(
            "SELECT stop_price, revision FROM user_trade_execution WHERE execution_id=?",
            (execution_id,),
        ).fetchone()
        assert tuple(stored_stop) == (9.0, 2)
        assert connection.execute(
            "SELECT COUNT(*) FROM user_api_audit WHERE token_id=?",
            (str(metadata["token_id"]),),
        ).fetchone()[0] == 3
        assert connection.execute(
            "SELECT COUNT(*) FROM user_api_audit WHERE request_id LIKE '%audit-failure%'"
        ).fetchone()[0] == 0


@pytest.mark.parametrize("invalid_quantity", ["NaN", "Infinity", "1.5", "0", "-1", "true"])
def test_agent_api_rejects_non_integer_or_non_positive_quantity(
    tmp_path, invalid_quantity: str
) -> None:
    client = _client(tmp_path)
    users, user = _principal(client)
    token, _ = users.create_api_token(user.user_id, "quantity-validation")
    body = (
        '{"trades":[{"symbol":"000001.SZ","traded_on":"2026-08-01",'
        '"traded_at":"09:30:00","side":"BUY",'
        f'"quantity":{invalid_quantity},"price":10.0}}]}}'
    )

    response = client.post(
        "/api/v1/trades/batch",
        headers={
            **_token_headers(token, key=f"quantity-{invalid_quantity}"),
            "Content-Type": "application/json",
        },
        content=body,
    )

    assert response.status_code == 422
    with users.connect(read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM user_trade_execution").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM user_trade_batch").fetchone()[0] == 0
