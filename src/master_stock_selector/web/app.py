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
from .routers.watchlist import build_watchlist_router

TEMPLATE_DIR = Path(__file__).with_name("templates")
STATIC_DIR = Path(__file__).with_name("static")
PROJECT_ROOT = Path(__file__).resolve().parents[3]


def create_app(
    *,
    market_database: str | Path | None = None,
    watchlist_database: str | Path | None = None,
) -> FastAPI:
    """Create the user-facing two-master watchlist website."""

    market_path = _market_database_path(market_database)
    watchlist_path = _watchlist_database_path(watchlist_database)

    app = FastAPI(
        title="大师选股",
        description="Weinstein 指数与个股阶段、Minervini 个股趋势模板观察池",
    )
    app.state.market_database = str(market_path)
    app.state.watchlist_database = str(watchlist_path)
    repository = WatchlistRepository(watchlist_path)
    repository.initialize()
    market_reader = MarketDataReader(market_path)
    app.state.watchlist_repository = repository
    app.state.market_reader = market_reader

    @app.middleware("http")
    async def add_performance_headers(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        started = perf_counter()
        response = await call_next(request)
        duration_ms = (perf_counter() - started) * 1000
        response.headers["Server-Timing"] = f"app;dur={duration_ms:.1f}"
        if request.url.path.startswith("/static/") and request.url.path.endswith((".css", ".js")):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    def render(
        request: Request,
        template_name: str,
        context: dict[str, Any],
    ) -> HTMLResponse:
        nav_groups = [
            {
                "label": "大师选股",
                "items": [
                    ("大师观察池", "/a/daily"),
                    ("四指数判断", "/a/indices"),
                    ("行业观察", "/a/industries"),
                    ("我的重点", "/a/focus"),
                    ("交易复盘", "/a/review"),
                ],
            }
        ]
        base_context = {
            "request": request,
            "today": date.today().isoformat(),
            "a_nav_groups": nav_groups,
            "a_nav": [item for group in nav_groups for item in group["items"]],
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
        build_watchlist_router(
            render=render,
            repository=repository,
            market_reader=market_reader,
        )
    )

    return app


def _market_database_path(
    explicit: str | Path | None,
) -> Path:
    if explicit is not None:
        return Path(explicit)
    configured = os.environ.get("MASTERSTOCK_MARKET_DATABASE", "").strip()
    if configured:
        return Path(configured)
    return PROJECT_ROOT / "data" / "market.sqlite3"


def _watchlist_database_path(
    explicit: str | Path | None,
) -> Path:
    if explicit is not None:
        return Path(explicit)
    configured = os.environ.get("MASTERSTOCK_WATCHLIST_DATABASE", "").strip()
    if configured:
        return Path(configured)
    return PROJECT_ROOT / "data" / "master_watchlist.sqlite3"
