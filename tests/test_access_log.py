from __future__ import annotations

from pathlib import Path

from fastapi import Request
from fastapi.testclient import TestClient

from master_stock_selector.web.access_log import (
    AccessLocation,
    ClientIpResolver,
    GeoIpResolver,
    should_record_access,
)
from master_stock_selector.web.app import create_app
from master_stock_selector.web.users import SESSION_COOKIE, UserRepository


def _request(peer: str, headers: list[tuple[bytes, bytes]]) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "path": "/a/daily",
            "raw_path": b"/a/daily",
            "query_string": b"",
            "headers": headers,
            "client": (peer, 50000),
            "server": ("example.test", 443),
        }
    )


def test_client_ip_headers_are_used_only_for_trusted_proxy() -> None:
    resolver = ClientIpResolver("127.0.0.0/8")

    direct = _request("198.51.100.10", [(b"x-real-ip", b"8.8.8.8")])
    proxied = _request("127.0.0.1", [(b"x-real-ip", b"8.8.4.4")])
    forwarded = _request(
        "127.0.0.1",
        [(b"x-forwarded-for", b"1.1.1.1, 127.0.0.2")],
    )

    assert resolver.resolve(direct) == "198.51.100.10"
    assert resolver.resolve(proxied) == "8.8.4.4"
    assert resolver.resolve(forwarded) == "1.1.1.1"


def test_geoip_resolver_handles_local_and_missing_database_without_network(
    tmp_path: Path,
) -> None:
    resolver = GeoIpResolver(tmp_path / "missing.mmdb")

    assert resolver.lookup("127.0.0.1").source == "LOCAL_OR_RESERVED"
    assert resolver.lookup("8.8.8.8").source == "UNAVAILABLE"
    assert resolver.lookup("not-an-ip").source == "INVALID"


def test_access_path_filter_excludes_operational_and_static_requests() -> None:
    assert should_record_access("/a/daily")
    assert should_record_access("/api/v1/trades")
    assert not should_record_access("/healthz")
    assert not should_record_access("/static/app.css")
    assert not should_record_access("/favicon.ico")
    assert not should_record_access("/robots.txt")


def test_web_access_is_recorded_without_query_string_and_can_link_user(
    tmp_path: Path,
) -> None:
    user_path = tmp_path / "users.sqlite3"
    users = UserRepository(user_path)
    users.initialize()
    user_id = users.create_user("reader", "Reader-password-123")
    session, _ = users.create_session(user_id)
    app = create_app(
        market_database=tmp_path / "market.sqlite3",
        watchlist_database=tmp_path / "master_watchlist.sqlite3",
        user_database=user_path,
        secure_cookies=False,
        trusted_proxies="127.0.0.1/32",
    )
    app.state.geoip_resolver.lookup = lambda _ip: AccessLocation(
        country_code="CN",
        country_name="中国",
        region_name="北京市",
        city_name="北京市",
        source="TEST",
    )
    client = TestClient(app, client=("127.0.0.1", 50000))
    client.cookies.set(SESSION_COOKIE, session)

    response = client.get(
        "/a/daily?date=2026-08-15",
        headers={"X-Real-IP": "8.8.8.8"},
    )

    assert response.status_code == 200
    with users.connect(read_only=True) as connection:
        row = connection.execute(
            """SELECT user_id, client_ip, country_code, country_name,
                      region_name, city_name, location_source, method, path, status_code
               FROM user_access_log"""
        ).fetchone()
    assert tuple(row) == (
        user_id,
        "8.8.8.8",
        "CN",
        "中国",
        "北京市",
        "北京市",
        "TEST",
        "GET",
        "/a/daily",
        200,
    )


def test_static_and_health_requests_do_not_create_access_rows(tmp_path: Path) -> None:
    user_path = tmp_path / "users.sqlite3"
    UserRepository(user_path).initialize()
    app = create_app(
        market_database=tmp_path / "market.sqlite3",
        watchlist_database=tmp_path / "master_watchlist.sqlite3",
        user_database=user_path,
        secure_cookies=False,
        trusted_proxies="127.0.0.1/32",
    )
    client = TestClient(app, client=("127.0.0.1", 50000))

    assert client.get("/healthz").status_code in {200, 503}
    assert client.get("/static/app.css").status_code == 200

    with UserRepository(user_path).connect(read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM user_access_log").fetchone()[0] == 0
