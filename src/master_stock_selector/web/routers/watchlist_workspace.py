from __future__ import annotations

from datetime import date as CalendarDate
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..display_time import sqlite_utc_to_shanghai
from ..owner import (
    OWNER_READING_NOTES,
    build_owner_activity_feed,
    build_owner_trade_feed,
)
from ..users import (
    OTHER_SETUP_ID,
    normalize_observation_tags,
)
from .auth import current_user, require_user
from .watchlist import (
    MANUAL_LABELS,
    _decorate_trade_r_multiples,
    _selected_date,
    _stock_external_urls,
    _web_exit_reason,
    _week_key,
    _weekly_bars,
)
from .watchlist_context import WatchlistRouteContext


def build_workspace_router(context: WatchlistRouteContext) -> APIRouter:
    router = APIRouter()
    render = context.render
    repository = context.repository
    market_reader = context.market_reader
    users = context.users
    site_owner_username = context.site_owner_username
    cached_rows = context.cached_rows
    require_csrf = context.require_csrf

    @router.get("/a/owner", response_class=HTMLResponse, include_in_schema=False)
    def owner_activity(request: Request) -> Response:
        if current_user(request) is None:
            redirect = RedirectResponse("/login?next=/a/owner", status_code=303)
            redirect.headers["X-Robots-Tag"] = "noindex, nofollow"
            return redirect

        query_date = repository.latest_fact_date()
        owner = users.active_user_by_username(site_owner_username)
        positions: list[dict[str, Any]] = []
        executions: list[dict[str, Any]] = []
        focus_items: list[dict[str, Any]] = []
        shared_plans: list[dict[str, Any]] = []
        current_shared_plan: dict[str, Any] | None = None
        reading_notes = sorted(
            OWNER_READING_NOTES,
            key=lambda article: str(article.get("updated_on") or ""),
            reverse=True,
        )
        latest_updated_at = ""
        if owner is not None:
            owner_id = str(owner["user_id"])
            reading_notes_by_slug = {
                str(article.get("slug") or article.get("href")): article
                for article in reading_notes
            }
            reading_notes_by_slug.update(
                {
                    str(article["slug"]): article
                    for article in users.reading_notes.shared_notes(owner_id)
                }
            )
            reading_notes = sorted(
                reading_notes_by_slug.values(),
                key=lambda article: str(article.get("updated_on") or ""),
                reverse=True,
            )
            shared_plans = users.trading_plans.shared_versions(owner_id)
            current_shared_plan = next(
                (plan for plan in shared_plans if str(plan.get("share_status") or "") == "ACTIVE"),
                None,
            )
            for shared_plan in shared_plans:
                latest_updated_at = max(
                    latest_updated_at, str(shared_plan.get("published_at") or "")
                )
            symbols = users.trade_symbols(owner_id)
            names = repository.stock_names(symbols)
            review = users.trade_review(owner_id, names)
            _decorate_trade_r_multiples(review["closed_fragments"], market_reader)
            feed = build_owner_trade_feed(
                review["executions"], names, closed_trades=review["closed_fragments"]
            )
            positions = list(feed["positions"])
            executions = list(feed["executions"])
            latest_updated_at = sqlite_utc_to_shanghai(feed["latest_updated_at"])

            reviews = [
                row
                for row in users.reviews_for_user(owner_id)
                if str(row.get("manual_state") or "") == "FOCUS"
            ]
            focus_symbols = {str(row.get("symbol") or "") for row in reviews}
            focus_names = repository.stock_names(focus_symbols)
            public_by_symbol = (
                {
                    str(row.get("symbol") or ""): row
                    for row in cached_rows(query_date, include_liquidity=False)
                }
                if query_date
                else {}
            )
            held_symbols = {str(item["symbol"]) for item in positions}
            for row in reviews:
                symbol = str(row.get("symbol") or "")
                focus_items.append(
                    {
                        "symbol": symbol,
                        "name": focus_names.get(symbol)
                        or (public_by_symbol.get(symbol) or {}).get("name")
                        or "名称待补",
                        "note": str(row.get("note") or ""),
                        "reviewed_at": sqlite_utc_to_shanghai(row.get("reviewed_at")),
                        "public": public_by_symbol.get(symbol),
                        "is_held": symbol in held_symbols,
                    }
                )
                latest_updated_at = max(latest_updated_at, str(row.get("reviewed_at") or ""))
            focus_items.sort(key=lambda item: (item["reviewed_at"], item["symbol"]), reverse=True)
            for item in positions:
                item["is_focus"] = str(item["symbol"]) in focus_symbols

        activity_items = build_owner_activity_feed(
            executions=executions,
            positions=positions,
            focus_items=focus_items,
            reading_notes=reading_notes,
            shared_plans=shared_plans,
        )
        latest_updated_at = max(
            [
                latest_updated_at,
                *(str(article.get("updated_on") or "") for article in reading_notes),
            ]
        )

        page = render(
            request,
            "ashare/owner_activity.html",
            {
                "active": "站长动态",
                "query_date": query_date,
                "latest_date": query_date,
                "owner": owner,
                "owner_available": owner is not None,
                "positions": positions,
                "focus_items": focus_items,
                "executions": executions,
                "reading_notes": reading_notes,
                "shared_plans": shared_plans,
                "current_shared_plan": current_shared_plan,
                "activity_items": activity_items,
                "latest_updated_at": latest_updated_at,
                "suppress_research_date_note": True,
                "suppress_investment_notice": True,
            },
        )
        page.headers["X-Robots-Tag"] = "noindex, nofollow"
        return page

    @router.get("/a/observations", response_class=HTMLResponse)
    def personal_observations(
        request: Request,
        date: CalendarDate | None = None,
        state: str = "WATCH",
        tag: str = "",
        changes: bool = False,
    ) -> Response:
        user = current_user(request)
        if user is None:
            return RedirectResponse("/login?next=/a/observations", status_code=303)
        selected_state = state.upper()
        if selected_state not in {"WATCH", "FOCUS", "ARCHIVED"}:
            selected_state = "WATCH"
        query_date = date.isoformat() if date else _selected_date(repository, None, market_reader)
        reviews = users.reviews_for_user(user.user_id)
        selected_tag = ""
        if selected_state == "WATCH" and tag.strip():
            try:
                selected_tag = (normalize_observation_tags(tag) or [""])[0]
            except ValueError:
                selected_tag = ""
        reviews_by_symbol = {str(row["symbol"]): row for row in reviews}
        public_by_symbol = {
            str(row.get("symbol") or ""): row
            for row in cached_rows(query_date, include_liquidity=False)
        }
        names = repository.stock_names(set(reviews_by_symbol))
        items: list[dict[str, Any]] = []
        for symbol, review in reviews_by_symbol.items():
            public_row = public_by_symbol.get(symbol)
            urls = _stock_external_urls(symbol)
            items.append(
                {
                    "symbol": symbol,
                    "name": names.get(symbol) or (public_row or {}).get("name") or "名称待补",
                    "manual": review,
                    "manual_label": MANUAL_LABELS.get(str(review["manual_state"]), "未加入"),
                    "public": public_row,
                    **urls,
                }
            )
        counts = {
            value: sum(1 for item in items if item["manual"]["manual_state"] == value)
            for value in ("WATCH", "FOCUS", "ARCHIVED")
        }
        visible_items = [item for item in items if item["manual"]["manual_state"] == selected_state]
        visible_items.sort(
            key=lambda item: (str(item["manual"].get("reviewed_at") or ""), item["symbol"]),
            reverse=True,
        )
        watch_tags: dict[str, int] = {}
        for item in items:
            if item["manual"]["manual_state"] != "WATCH":
                continue
            for item_tag in item["manual"].get("tags", []):
                watch_tags[item_tag] = watch_tags.get(item_tag, 0) + 1
        if selected_tag:
            visible_items = [
                item
                for item in visible_items
                if selected_tag.casefold()
                in {str(item_tag).casefold() for item_tag in item["manual"].get("tags", [])}
            ]
        if changes:
            visible_items = [item for item in visible_items if any(
                method.get("state") in {"ENTERED", "REENTERED", "EXITED", "DATA_GAP"}
                for method in (item.get("public") or {}).get("methods", {}).values()
            )]
        observation_query = {"state": selected_state, "date": query_date}
        if changes:
            observation_query["changes"] = "true"
        if selected_state == "WATCH" and selected_tag:
            observation_query["tag"] = selected_tag
        return render(
            request,
            "ashare/personal_observations.html",
            {
                "active": "我的观察",
                "query_date": query_date,
                "latest_date": repository.latest_fact_date(),
                "selected_state": selected_state,
                "selected_tag": selected_tag,
                "changes_only": changes,
                "watch_tags": [
                    {"label": label, "count": count} for label, count in sorted(watch_tags.items())
                ],
                "state_urls": {
                    value: f"/a/observations?{urlencode({'state': value, 'date': query_date})}"
                    for value in ("WATCH", "FOCUS", "ARCHIVED")
                },
                "watch_list_path": (
                    f"/a/observations?{urlencode({'state': 'WATCH', 'date': query_date})}"
                ),
                "return_path": f"/a/observations?{urlencode(observation_query)}",
                "counts": counts,
                "items": visible_items,
                "manual_labels": MANUAL_LABELS,
            },
        )

    @router.get("/a/review", response_class=HTMLResponse)
    def trade_review(request: Request) -> Response:
        user = current_user(request)
        if user is None:
            return RedirectResponse("/login?next=/a/review", status_code=303)
        names = repository.stock_names(users.trade_symbols(user.user_id))
        review = users.trade_review(user.user_id, names)
        _decorate_trade_r_multiples(review["completed_lots"], market_reader)
        return render(
            request,
            "ashare/trade_review.html",
            {
                "active": "交易复盘",
                "query_date": repository.latest_fact_date(),
                "latest_date": repository.latest_fact_date(),
                "review": review,
            },
        )

    @router.post("/a/stocks/{symbol}/review", response_class=RedirectResponse)
    async def save_stock_review(request: Request, symbol: str) -> RedirectResponse:
        user = require_user(request)
        values = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        require_csrf(request, user, str((values.get("csrf_token") or [""])[0]))
        state = str((values.get("manual_state") or ["UNREVIEWED"])[0])
        note_values = values.get("note")
        tag_values = values.get("tags")
        note = (
            str(note_values[0])
            if note_values is not None
            else str(users.review(user.user_id, symbol).get("note") or "")
        )
        return_to = str((values.get("return_to") or [""])[0])
        try:
            users.save_review(
                user.user_id,
                symbol,
                state,
                note,
                str(tag_values[0]) if tag_values is not None else None,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        chart_prefix = f"/a/stocks/{symbol.upper()}/chart"
        if return_to.startswith(chart_prefix) and (
            len(return_to) == len(chart_prefix) or return_to[len(chart_prefix)] in "?#"
        ):
            navigation_context = {
                key: str((values.get(key) or [""])[0])
                for key in ("nav_previous", "nav_next", "nav_position", "nav_total")
                if str((values.get(key) or [""])[0])
            }
            if navigation_context:
                path, marker, fragment = return_to.partition("#")
                path = f"{path}{'&' if '?' in path else '?'}{urlencode(navigation_context)}"
                return_to = f"{path}#{fragment}" if marker else path
            return RedirectResponse(return_to, status_code=303)
        parsed_return = urlsplit(return_to)
        if (
            return_to.startswith("/a/")
            and not return_to.startswith("//")
            and not parsed_return.scheme
            and not parsed_return.netloc
        ):
            return RedirectResponse(return_to, status_code=303)
        return RedirectResponse(f"/a/stocks/{symbol.upper()}", status_code=303)

    @router.post("/a/stocks/{symbol}/c1", response_class=RedirectResponse)
    async def save_stock_c1_record(request: Request, symbol: str) -> RedirectResponse:
        user = require_user(request)
        values = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        require_csrf(request, user, str((values.get("csrf_token") or [""])[0]))
        action = str((values.get("action") or ["save"])[0]).lower()
        try:
            if action == "clear":
                users.clear_c1_record(user.user_id, symbol)
            else:
                users.save_c1_record(
                    user.user_id,
                    symbol,
                    {
                        key: str((values.get(key) or [""])[0])
                        for key in (
                            "as_of_date",
                            "range_started_on",
                            "lower_bound",
                            "upper_bound",
                            "atr14",
                            "support_test_count",
                            "structure_state",
                            "entry_variant",
                            "note",
                        )
                    },
                )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(
            f"/a/stocks/{symbol.upper()}?date="
            f"{str((values.get('as_of_date') or [''])[0])}#c1-structure",
            status_code=303,
        )

    @router.post(
        "/a/stocks/{symbol}/setups/{setup_id}/observation",
        response_class=RedirectResponse,
    )
    async def save_stock_setup_observation(
        request: Request, symbol: str, setup_id: str
    ) -> RedirectResponse:
        user = require_user(request)
        values = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        require_csrf(request, user, str((values.get("csrf_token") or [""])[0]))
        action = str((values.get("action") or ["save"])[0]).lower()
        as_of_date = str((values.get("as_of_date") or [""])[0])
        try:
            if action == "clear":
                users.clear_setup_observation(user.user_id, symbol, setup_id)
            else:
                users.save_setup_observation(
                    user.user_id,
                    symbol,
                    setup_id,
                    {
                        key: str((values.get(key) or [""])[0])
                        for key in (
                            "status",
                            "as_of_date",
                            "structure_started_on",
                            "signal_on",
                            "reference_low",
                            "reference_high",
                            "trigger_price",
                            "invalidation_price",
                            "summary",
                        )
                    },
                )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(
            f"/a/stocks/{symbol.upper()}?date={as_of_date}#setup-observations",
            status_code=303,
        )

    @router.post("/a/stocks/{symbol}/trades", response_class=RedirectResponse)
    async def record_stock_trade(request: Request, symbol: str) -> RedirectResponse:
        user = require_user(request)
        values = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)

        def value(key: str, default: str = "") -> str:
            return str((values.get(key) or [default])[0])

        stop_price = value("stop_price")
        require_csrf(request, user, value("csrf_token"))

        try:
            exit_reason = _web_exit_reason(
                value("exit_reason_choice"),
                value("exit_reason_other"),
                legacy=value("exit_reason"),
            )
            traded_on = value("traded_on")
            users.record_trade(
                user.user_id,
                traded_on=traded_on,
                traded_at=value("traded_at"),
                symbol=symbol,
                side=value("side"),
                quantity=int(value("quantity")),
                price=float(value("price")),
                fee=float(value("fee", "0") or "0"),
                method="MANUAL",
                plan_setup_id=value("plan_setup_id", OTHER_SETUP_ID),
                other_setup_note=value("other_setup_note"),
                stop_price=float(stop_price) if stop_price else None,
                rationale=value("rationale"),
                invalidation=value("invalidation"),
                exit_reason=exit_reason,
                market_context=value("market_context"),
                observation_snapshot=repository.observation_snapshot(symbol, traded_on),
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(f"/a/stocks/{symbol.upper()}#trade-journal", status_code=303)

    @router.post("/a/trades/{execution_id}", response_class=RedirectResponse)
    async def update_trade(request: Request, execution_id: str) -> RedirectResponse:
        user = require_user(request)
        values = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)

        def value(key: str, default: str = "") -> str:
            return str((values.get(key) or [default])[0])

        stop_price = value("stop_price")
        require_csrf(request, user, value("csrf_token"))

        existing = users.trade_execution(user.user_id, execution_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="trade not found")
        try:
            exit_reason = _web_exit_reason(
                value("exit_reason_choice"),
                value("exit_reason_other"),
                legacy=value("exit_reason"),
            )
            traded_on = value("traded_on")
            users.update_trade(
                user.user_id,
                execution_id,
                traded_on=traded_on,
                traded_at=value("traded_at"),
                side=value("side"),
                quantity=int(value("quantity")),
                price=float(value("price")),
                fee=float(value("fee", "0") or "0"),
                method="MANUAL",
                plan_setup_id=value("plan_setup_id", OTHER_SETUP_ID),
                other_setup_note=value("other_setup_note"),
                stop_price=float(stop_price) if stop_price else None,
                rationale=value("rationale"),
                invalidation=value("invalidation"),
                exit_reason=exit_reason,
                market_context=value("market_context"),
                observation_snapshot=repository.observation_snapshot(
                    str(existing["symbol"]), traded_on
                ),
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(f"/a/stocks/{existing['symbol']}#trade-journal", status_code=303)

    @router.get("/api/me/stocks/{symbol}/overlay")
    def private_chart_overlay(
        request: Request,
        symbol: str,
        date: str,
        price_scale_id: str,
        interval: str = Query(default="day", pattern="^(day|week)$"),
    ) -> dict[str, Any]:
        user = require_user(request)
        overlay = users.chart_trade_overlay(user.user_id, symbol, date)
        for stop in overlay["open_stops"]:
            stop["chart_price"] = market_reader.safe_chart_price_from_raw(
                symbol, str(stop["buy_date"]), float(stop["stop_price"]), date
            )
        if interval == "week":
            bars = _weekly_bars(market_reader.safe_stock_chart_bars(symbol, date))
            week_end = {_week_key(str(row["trade_date"])): str(row["trade_date"]) for row in bars}
            for execution in overlay["executions"]:
                execution["traded_on"] = week_end.get(
                    _week_key(str(execution["traded_on"])), str(execution["traded_on"])
                )
        return {
            "drawings": users.chart_drawings(user.user_id, symbol, price_scale_id, interval),
            "trade_overlay": overlay,
        }

    @router.post("/api/me/stocks/{symbol}/chart/drawings")
    async def create_chart_drawing(request: Request, symbol: str) -> dict[str, Any]:
        user = require_user(request)
        require_csrf(request, user, request.headers.get("X-CSRF-Token", ""))
        try:
            values = await request.json()
            drawing = users.save_chart_drawing(
                user.user_id,
                str(values.get("drawing_id") or uuid4().hex),
                symbol,
                str(values.get("price_scale_id") or ""),
                str(values.get("tool") or ""),
                values.get("anchors") or [],
                values.get("content_text") or "",
                values.get("interval_scope") or "both",
            )
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return drawing

    @router.delete("/api/me/stocks/{symbol}/chart/drawings/{drawing_id}")
    def delete_chart_drawing(
        request: Request, symbol: str, drawing_id: str, price_scale_id: str
    ) -> dict[str, bool]:
        user = require_user(request)
        require_csrf(request, user, request.headers.get("X-CSRF-Token", ""))
        return {
            "deleted": users.delete_chart_drawing(user.user_id, drawing_id, symbol, price_scale_id)
        }

    return router
