from __future__ import annotations

from collections.abc import Callable
from time import monotonic
from typing import Any
from urllib.parse import parse_qs, quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..users import SESSION_COOKIE, SESSION_IDLE_SECONDS, AuthenticatedUser, UserRepository

Render = Callable[[Request, str, dict[str, Any]], HTMLResponse]


def build_auth_router(
    *,
    render: Render,
    users: UserRepository,
    secure_cookies: bool,
) -> APIRouter:
    router = APIRouter()
    failed_attempts: dict[str, list[float]] = {}

    def limited(key: str) -> bool:
        now = monotonic()
        recent = [attempt for attempt in failed_attempts.get(key, []) if now - attempt < 300]
        failed_attempts[key] = recent
        return len(recent) >= 5

    @router.get("/login", response_class=HTMLResponse, include_in_schema=False)
    def login_page(request: Request, next: str = "/a/daily") -> Response:
        if current_user(request) is not None:
            return RedirectResponse(_safe_next(next), status_code=303)
        password_changed = request.query_params.get("password_changed") == "1"
        response = render(
            request,
            "login.html",
            {
                "active": "",
                "next_path": _safe_next(next),
                "error": "",
                "notice": "密码已更新，请使用新密码登录。" if password_changed else "",
            },
        )
        response.headers["Cache-Control"] = "private, no-store"
        return response

    @router.post("/login", response_class=HTMLResponse, include_in_schema=False)
    async def login(request: Request) -> Response:
        values = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        username = str((values.get("username") or [""])[0]).strip()
        password = str((values.get("password") or [""])[0])
        next_path = _safe_next(str((values.get("next") or ["/a/daily"])[0]))
        client_host = request.client.host if request.client else "unknown"
        attempt_key = f"{client_host}:{username.lower()}"
        if limited(attempt_key):
            raise HTTPException(status_code=429, detail="登录尝试过多，请五分钟后再试")
        account = users.authenticate(username, password)
        if account is None:
            failed_attempts.setdefault(attempt_key, []).append(monotonic())
            response: Response = render(
                request,
                "login.html",
                {
                    "active": "",
                    "next_path": next_path,
                    "error": "用户名或密码不正确",
                    "notice": "",
                },
            )
            response.status_code = 401
            response.headers["Cache-Control"] = "private, no-store"
            return response
        failed_attempts.pop(attempt_key, None)
        raw_token, _ = users.create_session(str(account["user_id"]))
        response = RedirectResponse(next_path, status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            raw_token,
            max_age=SESSION_IDLE_SECONDS,
            httponly=True,
            secure=secure_cookies,
            samesite="lax",
            path="/",
        )
        response.headers["Cache-Control"] = "private, no-store"
        return response

    @router.get("/account/password", response_class=HTMLResponse, include_in_schema=False)
    def password_page(request: Request) -> Response:
        if current_user(request) is None:
            return RedirectResponse("/login?next=/account/password", status_code=303)
        response = render(
            request,
            "account_password.html",
            {"active": "账户设置", "error": ""},
        )
        response.headers["Cache-Control"] = "private, no-store"
        return response

    @router.post("/account/password", response_class=HTMLResponse, include_in_schema=False)
    async def change_password(request: Request) -> Response:
        user = require_user(request)
        values = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        supplied = str((values.get("csrf_token") or [""])[0])
        if not users.csrf_valid(user, supplied):
            raise HTTPException(status_code=403, detail="CSRF 校验失败")
        current_password = str((values.get("current_password") or [""])[0])
        new_password = str((values.get("new_password") or [""])[0])
        confirmation = str((values.get("new_password_confirmation") or [""])[0])
        attempt_key = f"password:{user.user_id}"
        if limited(attempt_key):
            raise HTTPException(status_code=429, detail="密码尝试过多，请五分钟后再试")
        error = ""
        if new_password != confirmation:
            error = "两次输入的新密码不一致"
        else:
            try:
                changed = users.change_password(user.user_id, current_password, new_password)
            except ValueError as exc:
                error = str(exc)
            else:
                if not changed:
                    failed_attempts.setdefault(attempt_key, []).append(monotonic())
                    error = "当前密码不正确"
        if error:
            response = render(
                request,
                "account_password.html",
                {"active": "账户设置", "error": error},
            )
            response.status_code = 400
            response.headers["Cache-Control"] = "private, no-store"
            return response
        failed_attempts.pop(attempt_key, None)
        redirect = RedirectResponse("/login?password_changed=1", status_code=303)
        redirect.delete_cookie(SESSION_COOKIE, path="/")
        redirect.headers["Cache-Control"] = "private, no-store"
        return redirect

    @router.post("/logout", include_in_schema=False)
    async def logout(request: Request) -> RedirectResponse:
        user = require_user(request)
        values = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        supplied = str((values.get("csrf_token") or [""])[0])
        if not users.csrf_valid(user, supplied):
            raise HTTPException(status_code=403, detail="CSRF 校验失败")
        users.revoke_session(request.cookies.get(SESSION_COOKIE))
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/")
        response.headers["Cache-Control"] = "private, no-store"
        return response

    return router


def current_user(request: Request) -> AuthenticatedUser | None:
    value = getattr(request.state, "user", None)
    return value if isinstance(value, AuthenticatedUser) else None


def require_user(request: Request) -> AuthenticatedUser:
    user = current_user(request)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail=f"请先登录：/login?next={quote(request.url.path)}",
        )
    return user


def _safe_next(value: str) -> str:
    candidate = value.strip()
    if not candidate.startswith("/") or candidate.startswith("//"):
        return "/a/daily"
    return candidate
