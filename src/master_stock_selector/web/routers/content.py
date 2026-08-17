from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .auth import current_user

Render = Callable[[Request, str, dict[str, Any]], HTMLResponse]


def build_content_router(*, render: Render) -> APIRouter:
    router = APIRouter()

    def render_private_article(request: Request, template: str) -> Response:
        if current_user(request) is None:
            redirect = RedirectResponse(
                f"/login?next={request.url.path}",
                status_code=303,
            )
            redirect.headers["Cache-Control"] = "private, no-store"
            return redirect
        response = render(
            request,
            template,
            {"active": "", "suppress_research_date_note": True},
        )
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response

    @router.get(
        "/a/reading/trading-system-boundaries",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    def trading_system_boundaries(request: Request) -> Response:
        return render_private_article(
            request,
            "reading/trading_system_boundaries.html",
        )

    @router.get(
        "/a/reading/adam-grimes-trading-templates",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    def adam_grimes_trading_templates(request: Request) -> Response:
        return render_private_article(
            request,
            "reading/adam_grimes_trading_templates.html",
        )

    @router.get(
        "/a/reading/stop-loss-is-not-a-percentage",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    def stop_loss_is_not_a_percentage(request: Request) -> Response:
        return render_private_article(
            request,
            "reading/stop_loss_is_not_a_percentage.html",
        )

    return router
