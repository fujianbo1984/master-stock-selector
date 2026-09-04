from __future__ import annotations

from typing import Any, Callable
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..trading_plans import SETUP_FIELD_LABELS, PlanConflict
from ..users import UserRepository
from .auth import current_user


def build_trading_plan_router(
    *,
    render: Callable[[Request, str, dict[str, Any]], HTMLResponse],
    users: UserRepository,
    site_owner_username: str,
) -> APIRouter:
    router = APIRouter()

    async def form_values(request: Request) -> dict[str, str]:
        values = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        return {key: str(items[0]) for key, items in values.items() if items}

    def is_site_owner(request: Request) -> bool:
        user = current_user(request)
        return bool(
            user is not None
            and user.username.casefold() == site_owner_username.casefold()
        )

    @router.get("/a/plan", response_class=HTMLResponse)
    def page(request: Request, session: str = "") -> Response:
        user = current_user(request)
        if user is None:
            return RedirectResponse("/login?next=/a/plan", status_code=303)
        grill = users.trading_plans.status(user.user_id, session) if session else None
        if session and grill is None:
            raise HTTPException(status_code=404, detail="Grill 会话不存在")
        next_question = users.trading_plans.next(user.user_id, session) if grill else None
        validation = users.trading_plans.validate(user.user_id, session) if grill else None
        owner_share = users.trading_plans.current_share(user.user_id) if is_site_owner(request) else None
        latest_share = users.trading_plans.latest_share(user.user_id) if is_site_owner(request) else None
        return render(
            request,
            "ashare/trading_plan.html",
            {
                "active": "交易计划",
                "plan": users.trading_plans.current(user.user_id),
                "versions": users.trading_plans.versions(user.user_id),
                "grill": grill,
                "next_question": next_question,
                "validation": validation,
                "owner_share": owner_share,
                "latest_share": latest_share,
                "setup_field_labels": SETUP_FIELD_LABELS,
            },
        )

    @router.post("/a/plan/share")
    async def share(request: Request) -> RedirectResponse:
        user = current_user(request)
        if user is None:
            raise HTTPException(401, "请先登录")
        if not is_site_owner(request):
            raise HTTPException(403, "只有站长可以共享交易计划")
        form = await form_values(request)
        if not users.csrf_valid(user, str(form.get("csrf_token") or "")):
            raise HTTPException(403, "CSRF 校验失败")
        try:
            users.trading_plans.share_current(
                user.user_id, str(form.get("statement") or "")
            )
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from exc
        return RedirectResponse("/a/plan?share=published", status_code=303)

    @router.post("/a/plan/share/stop")
    async def stop_share(request: Request) -> RedirectResponse:
        user = current_user(request)
        if user is None:
            raise HTTPException(401, "请先登录")
        if not is_site_owner(request):
            raise HTTPException(403, "只有站长可以停止共享交易计划")
        form = await form_values(request)
        if not users.csrf_valid(user, str(form.get("csrf_token") or "")):
            raise HTTPException(403, "CSRF 校验失败")
        users.trading_plans.revoke_shares(user.user_id)
        return RedirectResponse("/a/plan?share=stopped", status_code=303)

    @router.get(
        "/a/owner/plans/{share_id}",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    def shared_plan(request: Request, share_id: str) -> Response:
        if current_user(request) is None:
            redirect = RedirectResponse(
                f"/login?next=/a/owner/plans/{share_id}", status_code=303
            )
            redirect.headers["X-Robots-Tag"] = "noindex, nofollow"
            return redirect
        owner = users.active_user_by_username(site_owner_username)
        if owner is None:
            raise HTTPException(404, "站长交易计划不存在")
        owner_id = str(owner["user_id"])
        plan = users.trading_plans.shared_by_id(owner_id, share_id)
        if plan is None:
            raise HTTPException(404, "共享的交易计划不存在或已撤回")
        page = render(
            request,
            "ashare/shared_trading_plan.html",
            {
                "active": "站长动态",
                "owner": owner,
                "plan": plan,
                "shared_versions": users.trading_plans.shared_versions(owner_id),
                "suppress_research_date_note": True,
                "suppress_investment_notice": True,
            },
        )
        page.headers["X-Robots-Tag"] = "noindex, nofollow"
        return page

    @router.post("/a/plan/grill/start")
    async def start(request: Request) -> RedirectResponse:
        user = current_user(request)
        if user is None:
            raise HTTPException(401, "请先登录")
        form = await form_values(request)
        if not users.csrf_valid(user, str(form.get("csrf_token") or "")):
            raise HTTPException(403, "CSRF 校验失败")
        session = users.trading_plans.start(
            user.user_id,
            template=str(form.get("template") or "blank"),
            from_version=str(form.get("from_version") or ""),
            change_goal=str(form.get("change_goal") or ""),
        )
        return RedirectResponse(f"/a/plan?session={session['session_id']}", status_code=303)

    @router.post("/a/plan/grill/{session_id}/answer")
    async def answer(request: Request, session_id: str) -> RedirectResponse:
        user = current_user(request)
        if user is None:
            raise HTTPException(401, "请先登录")
        form = await form_values(request)
        if not users.csrf_valid(user, str(form.get("csrf_token") or "")):
            raise HTTPException(403, "CSRF 校验失败")
        try:
            users.trading_plans.answer(
                user.user_id,
                session_id,
                {
                    "question_id": str(form.get("question_id") or ""),
                    "expected_revision": int(str(form.get("revision") or "0")),
                    "decision": {
                        "state": "CONFIRMED",
                        "value": str(form.get("value") or ""),
                        "user_summary": str(form.get("value") or ""),
                    },
                },
                actor_type="USER_WEB",
            )
        except (ValueError, PlanConflict) as exc:
            raise HTTPException(
                409 if isinstance(exc, PlanConflict) else 422, detail=str(exc)
            ) from exc
        return RedirectResponse(f"/a/plan?session={session_id}", status_code=303)

    @router.post("/a/plan/grill/{session_id}/setups/save")
    async def save_setup(request: Request, session_id: str) -> RedirectResponse:
        user = current_user(request)
        if user is None:
            raise HTTPException(401, "请先登录")
        form = await form_values(request)
        if not users.csrf_valid(user, str(form.get("csrf_token") or "")):
            raise HTTPException(403, "CSRF 校验失败")
        try:
            users.trading_plans.save_setup(
                user.user_id,
                session_id,
                form,
                expected_revision=int(str(form.get("revision") or "0")),
            )
        except PlanConflict as exc:
            raise HTTPException(409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from exc
        return RedirectResponse(f"/a/plan?session={session_id}#setup-editor", status_code=303)

    @router.post("/a/plan/grill/{session_id}/setups/{setup_id}/archive")
    async def archive_setup(
        request: Request, session_id: str, setup_id: str
    ) -> RedirectResponse:
        user = current_user(request)
        if user is None:
            raise HTTPException(401, "请先登录")
        form = await form_values(request)
        if not users.csrf_valid(user, str(form.get("csrf_token") or "")):
            raise HTTPException(403, "CSRF 校验失败")
        try:
            users.trading_plans.archive_setup(
                user.user_id,
                session_id,
                setup_id,
                expected_revision=int(str(form.get("revision") or "0")),
            )
        except PlanConflict as exc:
            raise HTTPException(409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from exc
        return RedirectResponse(f"/a/plan?session={session_id}#setup-editor", status_code=303)

    @router.post("/a/plan/grill/{session_id}/publish")
    async def publish(request: Request, session_id: str) -> RedirectResponse:
        user = current_user(request)
        if user is None:
            raise HTTPException(401, "请先登录")
        form = await form_values(request)
        if not users.csrf_valid(user, str(form.get("csrf_token") or "")):
            raise HTTPException(403, "CSRF 校验失败")
        try:
            users.trading_plans.publish(
                user.user_id, session_id, int(str(form.get("revision") or "0"))
            )
        except (ValueError, PlanConflict) as exc:
            raise HTTPException(
                409 if isinstance(exc, PlanConflict) else 422, detail=str(exc)
            ) from exc
        return RedirectResponse("/a/plan", status_code=303)

    return router
