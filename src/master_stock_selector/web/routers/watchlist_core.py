from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from .auth import current_user
from .watchlist_context import WatchlistRouteContext


def build_core_router(context: WatchlistRouteContext) -> APIRouter:
    router = APIRouter()
    repository = context.repository
    market_reader = context.market_reader
    users = context.users

    @router.get("/", include_in_schema=False)
    def home() -> RedirectResponse:
        return RedirectResponse("/a/daily", status_code=307)

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

    @router.get("/a/dashboard", include_in_schema=False)
    def old_dashboard_redirect() -> RedirectResponse:
        return RedirectResponse("/a/daily", status_code=307)

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
