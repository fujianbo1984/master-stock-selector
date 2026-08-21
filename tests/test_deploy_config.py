from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_ecs_web_service_is_loopback_only() -> None:
    service = (PROJECT_ROOT / "deploy/systemd/masterstock-web.service").read_text()

    assert "web --host 127.0.0.1 --port 8000" in service
    assert "web --host 0.0.0.0" not in service


def test_public_nginx_entry_is_read_only_and_blocks_private_routes() -> None:
    config = (
        PROJECT_ROOT / "deploy/nginx/masterstock-public-readonly.conf.example"
    ).read_text()

    assert "listen 8888 default_server;" in config
    assert "proxy_pass http://127.0.0.1:8000;" in config
    assert "limit_except GET HEAD" in config
    assert 'proxy_set_header Cookie "";' in config
    assert 'proxy_set_header Authorization "";' in config
    assert "location / {\n        return 404;\n    }" in config
    assert "api/a/" in config
    assert "stocks/[^/]+(?:/(?:chart|realtime))?" in config
    assert "api/v1" not in config
    assert "api/me" not in config


def test_nginx_entries_compress_text_and_serve_static_files_directly() -> None:
    configs = [
        (PROJECT_ROOT / "deploy/nginx/masterstock.conf.example").read_text(),
        (
            PROJECT_ROOT / "deploy/nginx/masterstock-public-readonly.conf.example"
        ).read_text(),
    ]

    for config in configs:
        assert "gzip on;" in config
        assert "gzip_comp_level 5;" in config
        assert "application/json" in config
        static_location = config.split("location ^~ /static/ {", 1)[1].split(
            "\n    location ", 1
        )[0]
        assert "root /opt/masterstock/src/master_stock_selector/web;" in static_location
        assert "try_files $uri =404;" in static_location
        assert "proxy_pass" not in static_location
        assert 'Cache-Control "public, max-age=31536000, immutable"' in static_location


def test_public_nginx_entry_uses_scoped_cache_policies() -> None:
    config = (
        PROJECT_ROOT / "deploy/nginx/masterstock-public-readonly.conf.example"
    ).read_text()

    server_prefix = config.split("location = / {", 1)[0]
    assert 'add_header Cache-Control "no-store" always;' not in server_prefix
    assert 'Cache-Control "public, max-age=30, stale-while-revalidate=60"' in config
    assert 'Cache-Control "public, max-age=300, stale-while-revalidate=60"' in config
    assert config.count("proxy_hide_header Cache-Control;") == 2
    health_location = config.split("location = /healthz {", 1)[1].split(
        "\n    location ", 1
    )[0]
    assert 'Cache-Control "no-store"' in health_location
