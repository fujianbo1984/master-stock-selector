from __future__ import annotations

from datetime import date as CalendarDate
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from ..display_time import sqlite_utc_to_shanghai
from .auth import current_user
from .watchlist import _selected_date
from .watchlist_context import WatchlistRouteContext


def build_core_router(context: WatchlistRouteContext) -> APIRouter:
    router = APIRouter()
    repository = context.repository
    market_reader = context.market_reader
    users = context.users

    @router.get("/", include_in_schema=False)
    def home() -> RedirectResponse:
        return RedirectResponse("/a/dashboard", status_code=307)

    @router.api_route(
        "/favicon.ico",
        methods=["GET", "HEAD"],
        include_in_schema=False,
    )
    def favicon() -> Response:
        svg = (
            "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
            "<rect width='64' height='64' rx='12' fill='#245df4'/>"
            "<path d='M14 43h36v6H14zM17 38l9-10 8 7 13-18 5 4-17 24-8-7-6 7z' fill='white'/>"
            "</svg>"
        )
        return Response(svg, media_type="image/svg+xml")

    @router.get("/a/dashboard", response_class=HTMLResponse)
    def dashboard(request: Request, date: CalendarDate | None = None) -> Response:
        query_date = date.isoformat() if date else _selected_date(repository, None, market_reader)
        user = current_user(request)
        reviews = users.reviews_for_user(user.user_id) if user else []
        active_reviews = [row for row in reviews if row["manual_state"] in {"WATCH", "FOCUS"}]
        active_reviews.sort(
            key=lambda row: (row["manual_state"] == "FOCUS", str(row.get("reviewed_at") or "")),
            reverse=True,
        )
        names = repository.stock_names({str(row["symbol"]) for row in active_reviews})
        recent = [
            {
                **row,
                "name": names.get(str(row["symbol"])) or row["symbol"],
                "updated_label": sqlite_utc_to_shanghai(row.get("reviewed_at")),
            }
            for row in active_reviews[:6]
        ]
        hot_industries = sorted(
            [row for row in context.cached_industry_heat(query_date) if bool(row.get("is_hot"))],
            key=lambda row: (
                int(row.get("return_5d_rank") or 1000000),
                str(row.get("industry_code") or ""),
            ),
        )[:5]
        return context.render(
            request,
            "ashare/dashboard.html",
            {
                "active": "工作台",
                "query_date": query_date,
                "recent_observations": recent,
                "observation_count": len(active_reviews),
                "hot_industries": hot_industries,
            },
        )

    @router.get("/a/discover", response_class=HTMLResponse)
    def discover(request: Request, date: CalendarDate | None = None) -> Response:
        return context.render(
            request,
            "ashare/discover.html",
            {
                "active": "发现股票",
                "query_date": date.isoformat()
                if date
                else _selected_date(repository, None, market_reader),
            },
        )

    @router.get("/a/focus", include_in_schema=False)
    def focus_redirect(request: Request) -> RedirectResponse:
        if current_user(request) is None:
            return RedirectResponse("/login?next=/a/focus", status_code=303)
        query = dict(request.query_params)
        query["state"] = "FOCUS"
        return RedirectResponse(f"/a/observations?{urlencode(query)}", status_code=307)

    @router.get("/healthz")
    def healthz() -> JSONResponse:
        runtime = repository.readiness()
        market_runtime = market_reader.readiness()
        user_runtime = users.readiness()
        healthy = bool(
            runtime["connected"]
            and runtime["has_facts"]
            and market_runtime.get("exists")
            and market_runtime.get("latest_date")
            and user_runtime["connected"]
        )
        payload = {
            "status": "ok" if healthy else "degraded",
            "data_date": runtime.get("latest_fact_date") or market_runtime.get("latest_date") or "",
            "databases": {
                "market": bool(market_runtime.get("exists") and "error" not in market_runtime),
                "watchlist": bool(runtime.get("connected")),
                "users": bool(user_runtime.get("connected")),
            },
        }
        return JSONResponse(payload, status_code=200 if healthy else 503)

    return router
