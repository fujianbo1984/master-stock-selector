from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_ecs_web_service_is_loopback_only() -> None:
    service = (PROJECT_ROOT / "deploy/systemd/masterstock-web.service").read_text()

    assert "web --host 127.0.0.1 --port 8000" in service
    assert "web --host 0.0.0.0" not in service


def test_https_nginx_compresses_text_and_serves_static_files_directly() -> None:
    config = (PROJECT_ROOT / "deploy/nginx/masterstock.conf.example").read_text()

    assert "listen 80;" in config
    assert "listen 443 ssl http2;" in config
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
