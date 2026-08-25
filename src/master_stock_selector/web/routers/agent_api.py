from __future__ import annotations

from collections.abc import Mapping
from time import monotonic
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from ...watchlist.repository import WatchlistRepository
from ..reading_notes import ReadingConflict
from ..trading_plans import PlanConflict
from ..users import (
    ApiTokenPrincipal,
    TradeBatchValidationError,
    TradeRevisionConflict,
    UserRepository,
)


def build_agent_api_router(
    *, repository: WatchlistRepository, users: UserRepository, site_owner_username: str
) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["agent-api"])
    recent_requests: dict[str, list[float]] = {}

    def require_token(request: Request, scope: str | None = None) -> ApiTokenPrincipal:
        authorization = request.headers.get("Authorization", "")
        scheme, _, raw_token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not raw_token.strip():
            raise HTTPException(
                status_code=401,
                detail="缺少 Bearer Token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        principal = users.api_token_user(raw_token.strip())
        if principal is None:
            raise HTTPException(
                status_code=401,
                detail="Token 无效、已过期或已撤销",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if scope is not None and scope not in principal.scopes:
            raise HTTPException(status_code=403, detail=f"Token 缺少权限：{scope}")
        now = monotonic()
        recent = [
            value for value in recent_requests.get(principal.token_id, []) if now - value < 60
        ]
        if len(recent) >= 120:
            raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
        recent.append(now)
        recent_requests[principal.token_id] = recent
        return principal

    def request_id(request: Request) -> str:
        supplied = request.headers.get("X-Request-ID", "").strip()
        return supplied[:128] if supplied else uuid4().hex

    async def json_object(request: Request) -> dict[str, Any]:
        try:
            payload = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="请求体必须是 JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
        return payload

    def prepared_trades(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        raw_trades = payload.get("trades")
        if not isinstance(raw_trades, list):
            raise HTTPException(status_code=422, detail="trades 必须是数组")
        if not all(isinstance(item, dict) for item in raw_trades):
            raise HTTPException(status_code=422, detail="每笔交易必须是对象")
        symbols = {str(item.get("symbol") or "").upper().strip() for item in raw_trades}
        names = repository.stock_names({symbol for symbol in symbols if symbol})
        unknown = sorted(symbol for symbol in symbols if symbol and symbol not in names)
        if unknown:
            raise HTTPException(status_code=422, detail={"unknown_symbols": unknown})
        result: list[dict[str, Any]] = []
        for item in raw_trades:
            trade = dict(item)
            symbol = str(trade.get("symbol") or "").upper().strip()
            traded_on = str(trade.get("traded_on") or "")
            if symbol and traded_on:
                trade["observation_snapshot"] = repository.observation_snapshot(symbol, traded_on)
            result.append(trade)
        return result

    def enrich_results(report: dict[str, Any]) -> dict[str, Any]:
        symbols = {
            str(item.get("symbol") or "")
            for item in report.get("results", [])
            if item.get("symbol")
        }
        names = repository.stock_names(symbols)
        for item in report.get("results", []):
            symbol = str(item.get("symbol") or "")
            if symbol:
                item["stock_name"] = names.get(symbol, "名称待补")
        return report

    def batch_response(response: dict[str, Any], principal: ApiTokenPrincipal) -> dict[str, Any]:
        if "trades:read" in principal.scopes:
            return enrich_results(response)
        return {
            key: response[key]
            for key in (
                "batch_id",
                "status",
                "created",
                "duplicates",
                "rejected",
                "replayed",
            )
        } | {
            "results": [
                {
                    key: item[key]
                    for key in ("index", "status", "execution_id", "revision")
                    if key in item
                }
                for item in response["results"]
            ]
        }

    def validation_response(report: dict[str, Any], principal: ApiTokenPrincipal) -> dict[str, Any]:
        if "trades:read" in principal.scopes:
            return enrich_results(report)
        return {key: report[key] for key in ("status", "ready", "duplicates", "rejected")} | {
            "results": [
                {
                    key: item[key]
                    for key in (
                        "index",
                        "client_id",
                        "status",
                        "reason",
                        "execution_id",
                        "revision",
                    )
                    if key in item
                }
                for item in report["results"]
            ]
        }

    def require_site_owner(principal: ApiTokenPrincipal) -> None:
        if principal.username.casefold() != site_owner_username.casefold():
            raise HTTPException(status_code=403, detail="只有站长可以共享阅读感想")

    @router.get("/me")
    def me(request: Request) -> dict[str, Any]:
        principal = require_token(request)
        return {
            "user_id": principal.user_id,
            "username": principal.username,
            "display_name": principal.display_name,
            "token_id": principal.token_id,
            "scopes": list(principal.scopes),
        }

    @router.post("/reading/validate")
    async def validate_reading(request: Request) -> dict[str, Any]:
        require_token(request, "reading:read")
        payload = await json_object(request)
        try:
            normalized = users.reading_notes.validate_payload(payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "status": "READY",
            "slug": normalized["slug"],
            "title": normalized["title"],
            "tag_count": len(normalized["tags"]),
        }

    @router.get("/reading")
    def reading_list(request: Request) -> dict[str, Any]:
        principal = require_token(request, "reading:read")
        return {"notes": users.reading_notes.list_notes(principal.user_id)}

    @router.get("/reading/{note_id}")
    def reading_show(request: Request, note_id: str) -> dict[str, Any]:
        principal = require_token(request, "reading:read")
        note = users.reading_notes.get(principal.user_id, note_id)
        if note is None:
            raise HTTPException(status_code=404, detail="阅读感想不存在")
        return {
            "note": note,
            "versions": users.reading_notes.versions(principal.user_id, note_id),
        }

    @router.post("/reading")
    async def reading_save(request: Request) -> dict[str, Any]:
        principal = require_token(request, "reading:write")
        rid = request_id(request)
        payload = await json_object(request)
        note_id = str(payload.pop("note_id", "") or "")
        raw_revision = payload.pop("expected_revision", None)
        if raw_revision is not None and not isinstance(raw_revision, int):
            raise HTTPException(status_code=422, detail="expected_revision 必须是整数")
        try:
            note = users.reading_notes.save(
                principal.user_id,
                payload,
                note_id=note_id,
                expected_revision=raw_revision,
            )
        except ReadingConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        users.audit_api_action(
            principal, action="reading.save", request_id=rid, outcome="UPDATED"
        )
        return {"request_id": rid, "note": note}

    @router.post("/reading/{note_id}/submit")
    async def reading_submit(request: Request, note_id: str) -> dict[str, Any]:
        principal = require_token(request, "reading:write")
        rid = request_id(request)
        payload = await json_object(request)
        expected_revision = payload.get("expected_revision")
        if not isinstance(expected_revision, int):
            raise HTTPException(status_code=422, detail="expected_revision 必须是整数")
        try:
            version = users.reading_notes.submit(
                principal.user_id, note_id, expected_revision
            )
        except ReadingConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        users.audit_api_action(
            principal, action="reading.submit", request_id=rid, outcome="SUBMITTED"
        )
        return {"request_id": rid, "version": version}

    @router.post("/reading/{note_id}/share")
    async def reading_share(request: Request, note_id: str) -> dict[str, Any]:
        principal = require_token(request, "reading:share")
        require_site_owner(principal)
        rid = request_id(request)
        payload = await json_object(request)
        version_id = str(payload.get("version_id") or "")
        statement = str(payload.get("statement") or "")
        try:
            share = users.reading_notes.share(
                principal.user_id, note_id, version_id, statement
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        users.audit_api_action(
            principal, action="reading.share", request_id=rid, outcome="SHARED"
        )
        return {"request_id": rid, "share": share}

    @router.post("/reading/{note_id}/share/stop")
    def reading_unshare(request: Request, note_id: str) -> dict[str, Any]:
        principal = require_token(request, "reading:share")
        require_site_owner(principal)
        rid = request_id(request)
        revoked = users.reading_notes.revoke(principal.user_id, note_id)
        users.audit_api_action(
            principal, action="reading.unshare", request_id=rid, outcome="REVOKED"
        )
        return {"request_id": rid, "revoked": revoked}

    @router.get("/trades")
    def list_trades(
        request: Request,
        symbol: str = "",
        date_from: str = "",
        date_to: str = "",
        limit: int = Query(default=200, ge=1, le=1000),
    ) -> dict[str, Any]:
        principal = require_token(request, "trades:read")
        try:
            rows = users.trade_executions(
                principal.user_id,
                symbol=symbol or None,
                date_from=date_from or None,
                date_to=date_to or None,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        names = repository.stock_names({str(row["symbol"]) for row in rows})
        for row in rows:
            row["stock_name"] = names.get(str(row["symbol"]), "名称待补")
        return {"count": min(len(rows), limit), "trades": rows[:limit]}

    @router.get("/trades/{execution_id}")
    def get_trade(request: Request, execution_id: str) -> dict[str, Any]:
        principal = require_token(request, "trades:read")
        trade = users.trade_execution(principal.user_id, execution_id)
        if trade is None:
            raise HTTPException(status_code=404, detail="成交记录不存在")
        trade["stock_name"] = repository.stock_names({str(trade["symbol"])}).get(
            str(trade["symbol"]), "名称待补"
        )
        return {"trade": trade}

    @router.post("/trades/validate")
    async def validate_trades(request: Request) -> dict[str, Any]:
        principal = require_token(request, "trades:write")
        rid = request_id(request)
        try:
            payload = await json_object(request)
            report = users.validate_trade_batch(principal.user_id, prepared_trades(payload))
        except HTTPException:
            users.audit_api_action(
                principal,
                action="trades.validate",
                request_id=rid,
                outcome="REJECTED",
            )
            raise
        except (TypeError, ValueError) as exc:
            users.audit_api_action(
                principal,
                action="trades.validate",
                request_id=rid,
                outcome="REJECTED",
            )
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        users.audit_api_action(
            principal,
            action="trades.validate",
            request_id=rid,
            outcome=str(report["status"]),
            details={
                "ready": report["ready"],
                "duplicates": report["duplicates"],
                "rejected": report["rejected"],
            },
        )
        return {"request_id": rid, **validation_response(report, principal)}

    @router.post("/trades/batch")
    async def commit_trades(request: Request) -> JSONResponse:
        principal = require_token(request, "trades:write")
        rid = request_id(request)
        idempotency_key = request.headers.get("Idempotency-Key", "")
        try:
            payload = await json_object(request)
            response = users.record_trade_batch(
                principal.user_id,
                prepared_trades(payload),
                idempotency_key=idempotency_key,
                principal=principal,
                request_id=rid,
            )
        except TradeBatchValidationError as exc:
            report = exc.report
            users.audit_api_action(
                principal,
                action="trades.batch",
                request_id=rid,
                outcome="REJECTED",
                details={"rejected": report["rejected"]},
            )
            return JSONResponse(
                status_code=422,
                content={"request_id": rid, **validation_response(report, principal)},
            )
        except (TypeError, ValueError) as exc:
            users.audit_api_action(
                principal,
                action="trades.batch",
                request_id=rid,
                outcome="REJECTED",
            )
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return JSONResponse(content={"request_id": rid, **batch_response(response, principal)})

    @router.patch("/trades/{execution_id}/stop")
    async def update_stop(request: Request, execution_id: str) -> dict[str, Any]:
        principal = require_token(request, "trades:write")
        rid = request_id(request)
        payload = await json_object(request)
        try:
            if "stop_price" not in payload:
                raise ValueError("缺少 stop_price")
            expected_revision = payload.get("expected_revision")
            if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
                raise ValueError("expected_revision 必须为正整数")
            trade = users.update_trade_stop(
                principal.user_id,
                execution_id,
                float(payload["stop_price"]),
                expected_revision=expected_revision,
                principal=principal,
                request_id=rid,
            )
        except TradeRevisionConflict as exc:
            users.audit_api_action(
                principal,
                action="trades.stop",
                request_id=rid,
                outcome="CONFLICT",
                details={"execution_id": execution_id},
            )
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (TypeError, ValueError) as exc:
            users.audit_api_action(
                principal,
                action="trades.stop",
                request_id=rid,
                outcome="REJECTED",
                details={"execution_id": execution_id},
            )
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        receipt: dict[str, Any] = {
            "request_id": rid,
            "status": "UPDATED",
            "execution_id": execution_id,
            "revision": int(trade["revision"]),
        }
        if "trades:read" in principal.scopes:
            receipt["trade"] = trade
        return receipt

    @router.get("/plans/current")
    def current_plan(request: Request) -> dict[str, Any]:
        principal = require_token(request, "plans:read")
        return {"plan": users.trading_plans.current(principal.user_id)}

    @router.get("/plans/versions")
    def plan_versions(request: Request) -> dict[str, Any]:
        principal = require_token(request, "plans:read")
        return {"versions": users.trading_plans.versions(principal.user_id)}

    @router.get("/plans/versions/{version_id}")
    def plan_version(request: Request, version_id: str) -> dict[str, Any]:
        principal = require_token(request, "plans:read")
        plan = users.trading_plans.show(principal.user_id, version_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="计划版本不存在")
        return {"plan": plan}

    @router.get("/plans/diff")
    def plan_diff(request: Request, old: str, new: str) -> dict[str, Any]:
        principal = require_token(request, "plans:read")
        try:
            return users.trading_plans.diff(principal.user_id, old, new)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/plans/grill/sessions")
    async def start_grill(request: Request) -> dict[str, Any]:
        principal = require_token(request, "plans:write")
        rid = request_id(request)
        payload = await json_object(request)
        try:
            result = users.trading_plans.start(
                principal.user_id,
                template=str(payload.get("template") or "blank"),
                from_version=str(payload.get("from_version") or ""),
                change_goal=str(payload.get("change_goal") or ""),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        users.audit_api_action(
            principal, action="plans.grill.start", request_id=rid, outcome="CREATED"
        )
        return {"request_id": rid, **result}

    @router.get("/plans/grill/sessions/{session_id}")
    def grill_status(request: Request, session_id: str) -> dict[str, Any]:
        principal = require_token(request, "plans:read")
        result = users.trading_plans.status(principal.user_id, session_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Grill 会话不存在")
        return result

    @router.get("/plans/grill/sessions/{session_id}/next")
    def grill_next(request: Request, session_id: str) -> dict[str, Any]:
        principal = require_token(request, "plans:read")
        try:
            return users.trading_plans.next(principal.user_id, session_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/plans/grill/sessions/{session_id}/answers")
    async def grill_answer(request: Request, session_id: str) -> dict[str, Any]:
        principal = require_token(request, "plans:write")
        rid = request_id(request)
        payload = await json_object(request)
        try:
            result = users.trading_plans.answer(principal.user_id, session_id, payload)
        except PlanConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        users.audit_api_action(
            principal, action="plans.grill.answer", request_id=rid, outcome="UPDATED"
        )
        return {"request_id": rid, **result}

    @router.post("/plans/grill/sessions/{session_id}/validate")
    def grill_validate(request: Request, session_id: str) -> dict[str, Any]:
        principal = require_token(request, "plans:read")
        try:
            return users.trading_plans.validate(principal.user_id, session_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/plans/grill/sessions/{session_id}/publish")
    async def grill_publish(request: Request, session_id: str) -> dict[str, Any]:
        principal = require_token(request, "plans:publish")
        rid = request_id(request)
        payload = await json_object(request)
        expected_revision = payload.get("expected_revision")
        if not isinstance(expected_revision, int):
            raise HTTPException(status_code=422, detail="expected_revision 必须是整数")
        try:
            result = users.trading_plans.publish(
                principal.user_id, session_id, expected_revision
            )
        except PlanConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        users.audit_api_action(
            principal, action="plans.grill.publish", request_id=rid, outcome="PUBLISHED"
        )
        return {"request_id": rid, **result}

    return router
