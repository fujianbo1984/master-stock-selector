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
