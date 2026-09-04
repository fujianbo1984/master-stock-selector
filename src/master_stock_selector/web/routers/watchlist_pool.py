from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ...watchlist.brooks import BROOKS_POLICY_VERSION
from .auth import current_user
from .watchlist import (
    METHOD_LABELS,
    _base_filter_rows,
    _brooks_phase,
    _brooks_priority_quality,
    _brooks_pullback_tier,
    _change_rows,
    _current_rows,
    _daily_query_string,
    _filter_brooks_rows,
    _filter_rows,
    _market_cap_floor,
    _normalize_daily_query,
    _prepare_daily_rows,
    _projection_rows,
    _selected_date,
    _sort_daily_rows,
)
from .watchlist_context import WatchlistRouteContext


def build_pool_router(context: WatchlistRouteContext) -> APIRouter:
    router = APIRouter()
    render = context.render
    repository = context.repository
    market_reader = context.market_reader
    cached_rows = context.cached_rows
    cached_daily_auxiliary = context.cached_daily_auxiliary
    rows_for_user = context.rows_for_user
    projection_rows_for_user = context.projection_rows_for_user

    @router.get("/a/daily", response_class=HTMLResponse)
    def daily_watchlist(
        request: Request,
        date: str | None = None,
        view: str | None = None,
        method: str = "all",
        state: str = "all",
        manual: str = "all",
        min_cap: int = Query(default=50),
        industry: str = "",
        q: str = "",
    ) -> Response:
        if manual.upper() in {"WATCH", "FOCUS", "ARCHIVED", "DROPPED"}:
            if current_user(request) is None:
                return RedirectResponse("/login?next=/a/observations", status_code=303)
            query = {"state": "ARCHIVED" if manual.upper() == "DROPPED" else manual.upper()}
            if date:
                query["date"] = date
            return RedirectResponse(f"/a/observations?{urlencode(query)}", status_code=307)
        view_mode, method_mode, state_mode = _normalize_daily_query(
            view=view,
            method=method,
            state=state,
        )
        market_cap_floor = _market_cap_floor(min_cap)
        query_date = _selected_date(repository, date, market_reader)
        # 首页仍取总市值以执行默认的小市值过滤，但不加载流通市值和
        # 20 日成交额中位数，减少首次打开观察池的数据库工作量。
        formal_rows = rows_for_user(request, query_date, include_liquidity=False)
        formal_base_rows = _base_filter_rows(
            formal_rows,
            manual="all",
            min_cap=market_cap_floor,
            industry=industry,
            q=q,
        )
        projection_rows = (
            projection_rows_for_user(request, query_date) if view_mode == "projection" else []
        )
        projection_base_rows = _base_filter_rows(
            projection_rows,
            manual="all",
            min_cap=market_cap_floor,
            industry=industry,
            q=q,
        )
        rows = projection_rows if view_mode == "projection" else formal_rows
        base_rows = projection_base_rows if view_mode == "projection" else formal_base_rows
        current_counts = {
            "all": len(_current_rows(formal_base_rows, "all")),
            "weinstein": len(_current_rows(formal_base_rows, "weinstein")),
            "minervini": len(_current_rows(formal_base_rows, "minervini")),
            "both": len(_current_rows(formal_base_rows, "both")),
        }
        method_ready = method_mode in METHOD_LABELS
        change_counts = {
            value: len(_change_rows(formal_base_rows, method_mode, value)) if method_ready else 0
            for value in (
                "NEW",
                "ENTERED",
                "REENTERED",
                "CONTINUING",
                "EXIT",
                "EXITED",
                "DATA_GAP",
            )
        }
        projection_counts = {
            value: len(_projection_rows(projection_base_rows, value))
            for value in (
                "CHANGE",
                "ENTER",
                "PRE_ENTERED",
                "PRE_REENTERED",
                "PRE_EXITED",
                "PASSING",
                "PRE_CONTINUING",
                "DATA_GAP",
            )
        }
        filtered = (
            _projection_rows(projection_base_rows, state_mode)
            if view_mode == "projection"
            else _current_rows(formal_base_rows, method_mode)
            if view_mode == "current"
            else _change_rows(formal_base_rows, method_mode, state_mode)
            if method_ready
            else []
        )
        if view_mode != "projection":
            filtered = _prepare_daily_rows(
                _sort_daily_rows(filtered, view=view_mode, method=method_mode),
                method=method_mode,
            )
        index_rows, _ = cached_daily_auxiliary(query_date)
        date_fallback = date if date and date != query_date else ""
        industry_name = next(
            (
                str(row.get("industry") or "")
                for row in rows
                if str(row.get("industry_code") or "") == industry
            ),
            "",
        )

        def daily_url(
            *,
            target_view: str,
            target_method: str,
            target_state: str = "",
            target_industry: str | None = None,
        ) -> str:
            return "/a/daily?" + _daily_query_string(
                query_date=query_date,
                view=target_view,
                method=target_method,
                state=target_state,
                min_cap=market_cap_floor,
                industry=industry if target_industry is None else target_industry,
                q=q,
            )

        canonical_query = _daily_query_string(
            query_date=query_date,
            view=view_mode,
            method=method_mode,
            state=state_mode,
            min_cap=market_cap_floor,
            industry=industry,
            q=q,
        )
        return render(
            request,
            "ashare/watchlist.html",
            {
                "active": "大师观察池",
                "query_date": query_date,
                "latest_date": repository.latest_fact_date(),
                "available_dates": repository.available_dates(30),
                "indices": index_rows,
                "rows": filtered,
                "date_fallback": date_fallback,
                "industry_name": industry_name,
                "method_ready": method_ready,
                "filters": {
                    "view": view_mode,
                    "method": method_mode,
                    "state": state_mode,
                    "manual": "all",
                    "min_cap": market_cap_floor,
                    "industry": industry,
                    "q": q,
                    "query": canonical_query,
                },
                "summary": {
                    "current": current_counts,
                    "changes": change_counts,
                    "projection": projection_counts,
                    "result_count": len(filtered),
                    "reconstructed": sum(
                        1 for row in base_rows if row.get("origin") == "RECONSTRUCTED"
                    ),
                    "excluded_st": sum(
                        1 for row in formal_rows if row["has_pass"] and row.get("is_st")
                    ),
                    "small_cap": sum(
                        1
                        for row in formal_rows
                        if row["has_pass"] and not row.get("is_st") and row.get("small_cap")
                    ),
                    "missing_market_cap": sum(
                        1
                        for row in formal_rows
                        if row["has_pass"]
                        and not row.get("is_st")
                        and row.get("total_market_cap_yi") is None
                    ),
                },
                "urls": {
                    "current_view": daily_url(
                        target_view="current",
                        target_method=(
                            method_mode if method_mode in {"all", "both", *METHOD_LABELS} else "all"
                        ),
                    ),
                    "changes_view": daily_url(
                        target_view="changes",
                        target_method=method_mode if method_ready else "weinstein",
                        target_state=(
                            state_mode
                            if state_mode
                            in {
                                "NEW",
                                "ENTERED",
                                "REENTERED",
                                "CONTINUING",
                                "EXIT",
                                "EXITED",
                                "DATA_GAP",
                            }
                            else "NEW"
                        ),
                    ),
                    "projection_view": daily_url(
                        target_view="projection",
                        target_method="weinstein",
                        target_state="CHANGE",
                    ),
                    "current_methods": {
                        value: daily_url(target_view="current", target_method=value)
                        for value in ("all", "weinstein", "minervini", "both")
                    },
                    "change_methods": {
                        value: daily_url(
                            target_view="changes",
                            target_method=value,
                            target_state=state_mode or "NEW",
                        )
                        for value in ("weinstein", "minervini")
                    },
                    "change_states": {
                        value: daily_url(
                            target_view="changes",
                            target_method=method_mode,
                            target_state=value,
                        )
                        for value in change_counts
                    }
                    if method_ready
                    else {},
                    "projection_states": {
                        value: daily_url(
                            target_view="projection",
                            target_method="weinstein",
                            target_state=value,
                        )
                        for value in projection_counts
                    },
                    "clear_industry": daily_url(
                        target_view=view_mode,
                        target_method=method_mode,
                        target_state=state_mode,
                        target_industry="",
                    ),
                },
            },
        )

    @router.get("/a/brooks-second-leg", response_class=HTMLResponse)
    def brooks_second_leg(
        request: Request,
        date: str | None = None,
        state: str = "priority",
        industry: str = "",
        min_amount: float = Query(default=0, ge=0, le=10000),
        q: str = "",
        lookback: int = Query(default=20, ge=1, le=120),
    ) -> HTMLResponse:
        aliases = {
            "s2": "h1",
            "s0": "priority",
            "s1": "priority",
            "continuing": "priority",
            "active": "all",
        }
        state_mode = aliases.get(state.lower().strip(), state.lower().strip())
        allowed_states = {
            "priority",
            "watch",
            "pullback",
            "immature",
            "deep",
            "expanding",
            "followthrough",
            "cycle_done",
            "h1",
            "cycle_h1",
            "new",
            "history",
            "terminal",
            "paused",
            "all",
        }
        if state_mode not in allowed_states:
            raise HTTPException(status_code=422, detail="unsupported Brooks state filter")
        dates = repository.brooks_fact_dates(BROOKS_POLICY_VERSION)
        query_date = ""
        if dates:
            if date and date in dates:
                query_date = date
            elif date:
                query_date = ""
            else:
                query_date = dates[0]
        daily_rows = repository.brooks_rows(query_date, BROOKS_POLICY_VERSION) if query_date else []
        history_rows = (
            repository.brooks_h1_rows(query_date, BROOKS_POLICY_VERSION, lookback_dates=lookback)
            if query_date
            else []
        )
        all_rows = history_rows if state_mode == "history" else daily_rows
        industry_filter = industry.strip()
        available_industries = sorted(
            {str(row.get("industry") or "") for row in all_rows if row.get("industry")}
        )
        filtered = _filter_brooks_rows(
            all_rows,
            state=state_mode,
            industry=industry_filter,
            min_amount=min_amount,
            q=q,
        )
        phases = [_brooks_phase(row) for row in daily_rows]
        counts = {
            phase: phases.count(phase)
            for phase in {
                "h1",
                "cycle_h1",
                "new",
                "expanding",
                "followthrough",
                "immature",
                "watch",
                "deep",
                "cycle_done",
                "terminal",
                "paused",
            }
        }
        counts["priority"] = sum(
            phase == "watch" and _brooks_priority_quality(row)
            for row, phase in zip(daily_rows, phases, strict=True)
        )
        counts["pullback"] = counts["immature"] + counts["watch"] + counts["deep"]
        for h1_phase in ("h1", "cycle_h1"):
            for tier in ("watch", "immature", "deep"):
                counts[f"{h1_phase}_{tier}"] = sum(
                    phase == h1_phase and _brooks_pullback_tier(row) == tier
                    for row, phase in zip(daily_rows, phases, strict=True)
                )
        counts["history"] = len(history_rows)
        counts["all"] = len(daily_rows)
        return render(
            request,
            "ashare/brooks_second_leg.html",
            {
                "active": "Brooks 第二腿",
                "query_date": query_date,
                "latest_date": dates[0] if dates else "",
                "date_missing": date if date and not query_date else "",
                "schema_ready": repository.brooks_schema_ready(),
                "state": state_mode,
                "industry": industry_filter,
                "available_industries": available_industries,
                "min_amount": min_amount,
                "q": q,
                "lookback": lookback,
                "rows": filtered,
                "counts": counts,
            },
        )

    @router.get("/api/a/watchlist/{query_date}")
    def api_watchlist(
        query_date: str,
        method: str = Query(default="all", pattern="^(all|minervini|weinstein|both)$"),
        min_cap: int = Query(default=50),
    ) -> dict[str, Any]:
        market_cap_floor = _market_cap_floor(min_cap)
        rows = cached_rows(query_date, include_liquidity=True)
        return {
            "query_date": query_date,
            "method": method,
            "min_cap": market_cap_floor,
            "rows": _filter_rows(
                rows,
                method=method,
                state="all",
                manual="all",
                min_cap=market_cap_floor,
                industry="",
                q="",
            ),
        }

    return router
