from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from ..articles import published_article, published_articles
from ..reading_notes import ReadingConflict
from ..users import UserRepository
from .auth import current_user

Render = Callable[[Request, str, dict[str, Any]], HTMLResponse]


def build_content_router(
    *, render: Render, users: UserRepository, site_owner_username: str
) -> APIRouter:
    router = APIRouter()

    def owner_id() -> str | None:
        owner = users.active_user_by_username(site_owner_username)
        return str(owner["user_id"]) if owner is not None else None

    def shared_articles() -> list[dict[str, Any]]:
        identifier = owner_id()
        dynamic = users.reading_notes.shared_notes(identifier) if identifier else []
        by_slug = {str(article["slug"]): article for article in published_articles()}
        by_slug.update({str(article["slug"]): article for article in dynamic})
        return sorted(
            by_slug.values(),
            key=lambda article: (str(article["published_on"]), str(article.get("slug") or "")),
            reverse=True,
        )

    def require_user(request: Request) -> Any:
        user = current_user(request)
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录")
        return user

    def valid_csrf(request: Request, user: Any, values: dict[str, list[str]]) -> None:
        supplied = str((values.get("csrf_token") or [""])[0])
        if not users.csrf_valid(user, supplied):
            raise HTTPException(status_code=403, detail="CSRF 校验失败")

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
                    "articles": shared_articles(),
                    "suppress_research_date_note": True,
                },
            )
        )

    @router.get("/a/reading/mine", response_class=HTMLResponse, include_in_schema=False)
    def reading_workspace(request: Request) -> Response:
        redirect = require_reader(request)
        if redirect is not None:
            return redirect
        user = require_user(request)
        return mark_private(
            render(
                request,
                "reading/workspace.html",
                {"active": "阅读感想", "notes": users.reading_notes.list_notes(user.user_id)},
            )
        )

    @router.get("/a/reading/mine/new", response_class=HTMLResponse, include_in_schema=False)
    def new_reading_note(request: Request) -> Response:
        redirect = require_reader(request)
        if redirect is not None:
            return redirect
        return mark_private(
            render(request, "reading/editor.html", {"active": "阅读感想", "note": None, "versions": [], "share": None})
        )

    @router.get(
        "/a/reading/mine/{note_id}", response_class=HTMLResponse, include_in_schema=False
    )
    def edit_reading_note(request: Request, note_id: str) -> Response:
        redirect = require_reader(request)
        if redirect is not None:
            return redirect
        user = require_user(request)
        note = users.reading_notes.get(user.user_id, note_id)
        if note is None:
            raise HTTPException(status_code=404, detail="阅读感想不存在")
        share = (
            users.reading_notes.active_share_for_note(user.user_id, note_id)
            if user.username.casefold() == site_owner_username.casefold()
            else None
        )
        return mark_private(
            render(
                request,
                "reading/editor.html",
                {
                    "active": "阅读感想",
                    "note": note,
                    "versions": users.reading_notes.versions(user.user_id, note_id),
                    "share": share,
                },
            )
        )

    @router.post("/a/reading/mine/save", include_in_schema=False)
    async def save_reading_note(request: Request) -> RedirectResponse:
        user = require_user(request)
        values = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        valid_csrf(request, user, values)
        note_id = str((values.get("note_id") or [""])[0])
        revision_text = str((values.get("expected_revision") or [""])[0])
        tags = [tag.strip() for tag in str((values.get("tags") or [""])[0]).split(",")]
        try:
            result = users.reading_notes.save(
                user.user_id,
                {
                    "title": str((values.get("title") or [""])[0]),
                    "slug": str((values.get("slug") or [""])[0]),
                    "summary": str((values.get("summary") or [""])[0]),
                    "tags": [tag for tag in tags if tag],
                    "body_markdown": str((values.get("body_markdown") or [""])[0]),
                },
                note_id=note_id,
                expected_revision=int(revision_text) if revision_text else None,
            )
        except ReadingConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return RedirectResponse(f"/a/reading/mine/{result['note_id']}?saved=1", status_code=303)

    @router.post("/a/reading/mine/{note_id}/submit", include_in_schema=False)
    async def submit_reading_note(request: Request, note_id: str) -> RedirectResponse:
        user = require_user(request)
        values = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        valid_csrf(request, user, values)
        try:
            revision = int(str((values.get("expected_revision") or [""])[0]))
            users.reading_notes.submit(user.user_id, note_id, revision)
        except ReadingConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return RedirectResponse(f"/a/reading/mine/{note_id}?submitted=1", status_code=303)

    @router.post("/a/reading/mine/{note_id}/share", include_in_schema=False)
    async def share_reading_note(request: Request, note_id: str) -> RedirectResponse:
        user = require_user(request)
        if user.username.casefold() != site_owner_username.casefold():
            raise HTTPException(status_code=403, detail="只有站长可以共享阅读感想")
        values = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        valid_csrf(request, user, values)
        version_id = str((values.get("version_id") or [""])[0])
        statement = str((values.get("statement") or [""])[0])
        try:
            users.reading_notes.share(user.user_id, note_id, version_id, statement)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return RedirectResponse(f"/a/reading/mine/{note_id}?shared=1", status_code=303)

    @router.post("/a/reading/mine/{note_id}/share/stop", include_in_schema=False)
    async def stop_reading_share(request: Request, note_id: str) -> RedirectResponse:
        user = require_user(request)
        if user.username.casefold() != site_owner_username.casefold():
            raise HTTPException(status_code=403, detail="只有站长可以停止共享")
        values = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        valid_csrf(request, user, values)
        users.reading_notes.revoke(user.user_id, note_id)
        return RedirectResponse(f"/a/reading/mine/{note_id}?unshared=1", status_code=303)

    @router.get(
        "/a/owner/reading/{share_id}/assets/{asset_path:path}", include_in_schema=False
    )
    def shared_reading_asset(request: Request, share_id: str, asset_path: str) -> Response:
        redirect = require_reader(request)
        if redirect is not None:
            return redirect
        identifier = owner_id()
        asset = (
            users.reading_notes.shared_asset(identifier, share_id, asset_path)
            if identifier
            else None
        )
        if asset is None:
            raise HTTPException(status_code=404)
        data, media_type = asset
        return mark_private(Response(content=data, media_type=media_type))

    @router.get(
        "/a/owner/reading/{share_id}", response_class=HTMLResponse, include_in_schema=False
    )
    def shared_reading_note(request: Request, share_id: str) -> Response:
        redirect = require_reader(request)
        if redirect is not None:
            return redirect
        identifier = owner_id()
        article = users.reading_notes.shared_by_id(identifier, share_id) if identifier else None
        if article is None:
            raise HTTPException(status_code=404, detail="共享的阅读感想不存在或已撤回")
        return mark_private(
            render(
                request,
                "reading/article.html",
                {
                    "active": "",
                    "article": article,
                    "previous_article": None,
                    "next_article": None,
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
        identifier = owner_id()
        migrated = next(
            (
                article
                for article in users.reading_notes.shared_notes(identifier)
                if str(article["slug"]) == slug
            ),
            None,
        ) if identifier else None
        if migrated is not None:
            return mark_private(RedirectResponse(str(migrated["href"]), status_code=307))
        article = published_article(slug)
        if article is None:
            raise HTTPException(status_code=404)
        articles = sorted(
            shared_articles(),
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
