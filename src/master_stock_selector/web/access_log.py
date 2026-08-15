from __future__ import annotations

import ipaddress
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import Request

LOGGER = logging.getLogger(__name__)
DEFAULT_TRUSTED_PROXIES = "127.0.0.1/32,::1/128"
EXCLUDED_ACCESS_PATHS = frozenset({"/healthz", "/favicon.ico", "/robots.txt"})


@dataclass(frozen=True)
class AccessLocation:
    country_code: str = ""
    country_name: str = ""
    region_name: str = ""
    city_name: str = ""
    source: str = "UNAVAILABLE"


class ClientIpResolver:
    """Resolve proxy headers only when the direct peer is explicitly trusted."""

    def __init__(self, trusted_proxies: str = DEFAULT_TRUSTED_PROXIES):
        networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        for value in trusted_proxies.split(","):
            candidate = value.strip()
            if candidate:
                networks.append(ipaddress.ip_network(candidate, strict=False))
        self._trusted_networks = tuple(networks)

    def resolve(self, request: Request) -> str:
        peer = request.client.host.strip() if request.client is not None else ""
        peer_ip = _parse_ip(peer)
        if peer_ip is None:
            return ""
        if not self._is_trusted(peer_ip):
            return peer_ip.compressed

        real_ip = _parse_ip(request.headers.get("x-real-ip", ""))
        if real_ip is not None:
            return real_ip.compressed

        forwarded = [
            parsed
            for value in request.headers.get("x-forwarded-for", "").split(",")
            if (parsed := _parse_ip(value.strip())) is not None
        ]
        for candidate in reversed(forwarded):
            if not self._is_trusted(candidate):
                return candidate.compressed
        return peer_ip.compressed

    def _is_trusted(self, address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        return any(address in network for network in self._trusted_networks)


class GeoIpResolver:
    """Offline IP lookup backed by an optional MaxMind GeoLite2 City database."""

    def __init__(self, database_path: str | Path | None):
        self.database_path = Path(database_path).expanduser() if database_path else None
        self._reader: Any = None
        if self.database_path is None:
            return
        if not self.database_path.is_file():
            LOGGER.warning("GeoIP database does not exist: %s", self.database_path)
            return
        try:
            import geoip2.database

            self._reader = geoip2.database.Reader(str(self.database_path))
        except Exception:
            LOGGER.exception("Could not open GeoIP database: %s", self.database_path)

    def lookup(self, client_ip: str) -> AccessLocation:
        address = _parse_ip(client_ip)
        if address is None:
            return AccessLocation(source="INVALID")
        if not address.is_global:
            return AccessLocation(source="LOCAL_OR_RESERVED")
        if self._reader is None:
            return AccessLocation(source="UNAVAILABLE")
        try:
            response = self._reader.city(address.compressed)
        except Exception as exc:
            if exc.__class__.__name__ != "AddressNotFoundError":
                LOGGER.warning("GeoIP lookup failed for a valid public address: %s", exc)
            return AccessLocation(source="NOT_FOUND")
        return AccessLocation(
            country_code=str(response.country.iso_code or ""),
            country_name=_localized_name(response.country),
            region_name=_localized_name(response.subdivisions.most_specific),
            city_name=_localized_name(response.city),
            source="GEOLITE2_CITY",
        )

    def close(self) -> None:
        if self._reader is not None:
            self._reader.close()
            self._reader = None


def should_record_access(path: str) -> bool:
    return not path.startswith("/static/") and path not in EXCLUDED_ACCESS_PATHS


def _parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _localized_name(record: Any) -> str:
    names = getattr(record, "names", {}) or {}
    return str(names.get("zh-CN") or names.get("en") or getattr(record, "name", "") or "")
