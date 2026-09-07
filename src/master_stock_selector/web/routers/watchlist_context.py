from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse

from ...watchlist.repository import MarketDataReader, WatchlistRepository
from ..users import AuthenticatedUser, UserRepository

Render = Callable[[Request, str, dict[str, Any]], HTMLResponse]


@dataclass(frozen=True)
class WatchlistRouteContext:
    render: Render
    repository: WatchlistRepository
    market_reader: MarketDataReader
    users: UserRepository
    site_owner_username: str
    cached_rows: Callable[..., list[dict[str, Any]]]
    cached_daily_auxiliary: Callable[..., tuple[list[dict[str, Any]], list[dict[str, Any]]]]
    cached_industry_heat: Callable[[str], list[dict[str, Any]]]
    cached_projection_rows: Callable[..., list[dict[str, Any]]]
    clear_row_caches: Callable[[], None]
    rows_for_user: Callable[..., list[dict[str, Any]]]
    projection_rows_for_user: Callable[..., list[dict[str, Any]]]
    stock_navigation_rows_for_user: Callable[..., list[dict[str, Any]]]
    personal_navigation_rows: Callable[[Request], list[dict[str, Any]]]
    open_position_navigation_rows: Callable[[Request], list[dict[str, Any]]]
    brooks_navigation_rows: Callable[..., list[dict[str, Any]]]
    require_csrf: Callable[[Request, AuthenticatedUser, str], None]
