from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from datetime import date
from pathlib import Path
from time import perf_counter
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..watchlist.repository import MarketDataReader, WatchlistRepository
from .routers.agent_api import build_agent_api_router
from .routers.auth import build_auth_router, current_user
from .routers.content import build_content_router
from .routers.watchlist import build_watchlist_router
from .users import SESSION_COOKIE, UserRepository

TEMPLATE_DIR = Path(__file__).with_name("templates")
STATIC_DIR = Path(__file__).with_name("static")
PROJECT_ROOT = Path(__file__).resolve().parents[3]


def create_app(
    *,
    market_database: str | Path | None = None,
    watchlist_database: str | Path | None = None,
    user_database: str | Path | None = None,
    secure_cookies: bool | None = None,
) -> FastAPI:
    """Create the user-facing two-master watchlist website."""

    market_path = _market_database_path(market_database)
    watchlist_path = _watchlist_database_path(watchlist_database)
    user_path = _user_database_path(user_database, watchlist_path, watchlist_database is not None)
    cookies_are_secure = (
        _env_bool("MASTERSTOCK_SECURE_COOKIES", True)
        if secure_cookies is None
        else secure_cookies
    )

    app = FastAPI(
        title="大师选股",
        description="Weinstein 指数与个股阶段、Minervini 个股趋势模板观察池",
    )
    app.state.market_database = str(market_path)
    app.state.watchlist_database = str(watchlist_path)
    app.state.user_database = str(user_path)
    repository = WatchlistRepository(watchlist_path)
    repository.initialize()
    market_reader = MarketDataReader(market_path)
    users = UserRepository(user_path)
    users.require_schema()
    app.state.watchlist_repository = repository
    app.state.market_reader = market_reader
    app.state.user_repository = users

    @app.middleware("http")
    async def add_performance_headers(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        started = perf_counter()
        request.state.user = users.session_user(request.cookies.get(SESSION_COOKIE))
        response = await call_next(request)
        duration_ms = (perf_counter() - started) * 1000
        response.headers["Server-Timing"] = f"app;dur={duration_ms:.1f}"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if cookies_are_secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        if request.url.path.startswith("/static/") and request.url.path.endswith((".css", ".js")):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif current_user(request) is not None or request.url.path.startswith(
            ("/login", "/a/focus", "/a/review", "/api/me/", "/api/v1/")
        ):
            response.headers["Cache-Control"] = "private, no-store"
            response.headers["Vary"] = (
                "Authorization" if request.url.path.startswith("/api/v1/") else "Cookie"
            )
        return response

    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    def render(
        request: Request,
        template_name: str,
        context: dict[str, Any],
    ) -> HTMLResponse:
        user = current_user(request)
        public_items = [
            ("大师观察池", "/a/daily"),
            ("四指数判断", "/a/indices"),
            ("行业观察", "/a/industries"),
        ]
        private_items = [
            ("市场广度", "/a/breadth"),
            ("我的观察", "/a/observations"),
            ("交易复盘", "/a/review"),
        ] if user is not None else []
        nav_groups = [{"label": "公开研究", "items": public_items}]
        if private_items:
            nav_groups.append({"label": "个人工作区", "items": private_items})
        base_context = {
            "request": request,
            "today": date.today().isoformat(),
            "a_nav_groups": nav_groups,
            "a_nav": [item for group in nav_groups for item in group["items"]],
            "current_user": user,
            "csrf_token": user.csrf_token if user is not None else "",
            "research_date": (
                context.get("query_date")
                or context.get("latest_date")
                or request.query_params.get("date")
                or ""
            ),
        }
        base_context.update(context)
        return templates.TemplateResponse(request, template_name, base_context)

    app.include_router(
        build_auth_router(
            render=render,
            users=users,
            secure_cookies=cookies_are_secure,
        )
    )
    app.include_router(
        build_watchlist_router(
            render=render,
            repository=repository,
            market_reader=market_reader,
            users=users,
        )
    )
    app.include_router(build_content_router(render=render))
    app.include_router(build_agent_api_router(repository=repository, users=users))

    return app


def _market_database_path(
    explicit: str | Path | None,
) -> Path:
    if explicit is not None:
        return _resolve_database_path(explicit)
    configured = os.environ.get("MASTERSTOCK_MARKET_DATABASE", "").strip()
    if configured:
        return _resolve_database_path(configured)
    return PROJECT_ROOT / "data" / "market.sqlite3"


def _watchlist_database_path(
    explicit: str | Path | None,
) -> Path:
    if explicit is not None:
        return _resolve_database_path(explicit)
    configured = os.environ.get("MASTERSTOCK_WATCHLIST_DATABASE", "").strip()
    if configured:
        return _resolve_database_path(configured)
    return PROJECT_ROOT / "data" / "master_watchlist.sqlite3"


def _user_database_path(
    explicit: str | Path | None,
    watchlist_path: Path,
    watchlist_was_explicit: bool,
) -> Path:
    if explicit is not None:
        return _resolve_database_path(explicit)
    configured = os.environ.get("MASTERSTOCK_USER_DATABASE", "").strip()
    if configured:
        return _resolve_database_path(configured)
    if watchlist_was_explicit:
        return watchlist_path.with_name("users.sqlite3")
    return PROJECT_ROOT / "data" / "users.sqlite3"


def _resolve_database_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}
