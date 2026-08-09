from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .auth import current_user

Render = Callable[[Request, str, dict[str, Any]], HTMLResponse]


def build_content_router(*, render: Render) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/a/reading/trading-system-boundaries",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    def trading_system_boundaries(request: Request) -> Response:
        if current_user(request) is None:
            redirect = RedirectResponse(
                "/login?next=/a/reading/trading-system-boundaries",
                status_code=303,
            )
            redirect.headers["Cache-Control"] = "private, no-store"
            return redirect
        response = render(
            request,
            "reading/trading_system_boundaries.html",
            {"active": "", "suppress_research_date_note": True},
        )
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response

    return router
