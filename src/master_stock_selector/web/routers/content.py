from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from ..articles import published_article, published_articles
from .auth import current_user

Render = Callable[[Request, str, dict[str, Any]], HTMLResponse]


def build_content_router(*, render: Render) -> APIRouter:
    router = APIRouter()

    def require_reader(request: Request) -> Response | None:
        if current_user(request) is not None:
            return None
        redirect = RedirectResponse(f"/login?next={request.url.path}", status_code=303)
        redirect.headers["Cache-Control"] = "private, no-store"
        return redirect

    def mark_private(response: Response) -> Response:
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response

    @router.get("/a/reading", response_class=HTMLResponse, include_in_schema=False)
    def reading_archive(request: Request) -> Response:
        redirect = require_reader(request)
        if redirect is not None:
            return redirect
        return mark_private(
            render(
                request,
                "reading/archive.html",
                {
                    "active": "",
                    "articles": published_articles(),
                    "suppress_research_date_note": True,
                },
            )
        )

    @router.get(
        "/a/reading/{slug}/assets/{asset_path:path}",
        include_in_schema=False,
    )
    def reading_asset(request: Request, slug: str, asset_path: str) -> Response:
        redirect = require_reader(request)
        if redirect is not None:
            return redirect
        article = published_article(slug)
        if article is None:
            raise HTTPException(status_code=404)
        article_dir = Path(article["source_path"]).parent.resolve()
        asset = (article_dir / asset_path).resolve()
        if not asset.is_relative_to(article_dir) or not asset.is_file():
            raise HTTPException(status_code=404)
        return mark_private(FileResponse(asset))

    @router.get("/a/reading/{slug}", response_class=HTMLResponse, include_in_schema=False)
    def reading_article(request: Request, slug: str) -> Response:
        redirect = require_reader(request)
        if redirect is not None:
            return redirect
        article = published_article(slug)
        if article is None:
            raise HTTPException(status_code=404)
        articles = sorted(
            published_articles(),
            key=lambda item: (str(item["published_on"]), str(item["slug"])),
        )
        current_index = next(
            index for index, item in enumerate(articles) if item["slug"] == slug
        )
        response = render(
            request,
            "reading/article.html",
            {
                "active": "",
                "article": article,
                "previous_article": articles[current_index - 1] if current_index > 0 else None,
                "next_article": (
                    articles[current_index + 1] if current_index < len(articles) - 1 else None
                ),
                "suppress_research_date_note": True,
            },
        )
        return mark_private(response)

    return router
