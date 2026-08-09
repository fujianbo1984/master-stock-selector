from __future__ import annotations

from collections.abc import Callable
from datetime import date as calendar_date
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from ...watchlist.charting import keltner_channels
from ...watchlist.industry import INDUSTRY_POLICY_VERSION
from ...watchlist.industry_confirmation import build_industry_weinstein_confirmation
from ...watchlist.repository import TRADE_SETUP_LABELS, MarketDataReader, WatchlistRepository
from ..users import AuthenticatedUser, UserRepository
from .auth import current_user, require_user

METHOD_LABELS = {"minervini": "Minervini", "weinstein": "Weinstein"}
RESULT_LABELS = {
    "PASS": "符合",
    "FAIL": "不符合",
    "UNKNOWN": "数据不足",
    "TRANSITION": "转换期",
}
STATE_LABELS = {
    "ENTERED": "今日新进入",
    "CONTINUING": "持续符合",
    "EXITED": "今日退出",
    "REENTERED": "重新进入",
    "DATA_GAP": "数据中断",
}
STAGE_LABELS = {
    "STAGE_1": "第一阶段 · 筑底",
    "STAGE_2": "第二阶段 · 上升",
    "STAGE_3": "第三阶段 · 筑顶",
    "STAGE_4": "第四阶段 · 下降",
    "TRANSITION": "转换期",
    "UNKNOWN": "数据不足",
}
MANUAL_LABELS = {
    "UNREVIEWED": "未加入",
    "WATCH": "观察中",
    "FOCUS": "重点",
    "ARCHIVED": "已归档",
}
MARKET_CAP_FLOORS = {0, 30, 50, 100}
MINERVINI_CHECKS = (
    "close_above_sma50",
    "close_above_sma150",
    "close_above_sma200",
    "sma50_above_sma150",
    "sma50_above_sma200",
    "sma150_above_sma200",
    "sma200_rising_20d",
    "close_30pct_above_52w_low",
    "close_within_25pct_52w_high",
    "rs_252d_percentile_at_least_70",
)
MINERVINI_CHECK_LABELS = {
    "close_above_sma50": "收盘高于50日线",
    "close_above_sma150": "收盘高于150日线",
    "close_above_sma200": "收盘高于200日线",
    "sma50_above_sma150": "50日线高于150日线",
    "sma50_above_sma200": "50日线高于200日线",
    "sma150_above_sma200": "150日线高于200日线",
    "sma200_rising_20d": "200日线近20日上升",
    "close_30pct_above_52w_low": "高于52周低点至少30%",
    "close_within_25pct_52w_high": "距52周高点不超过25%",
    "rs_252d_percentile_at_least_70": "252日相对强度百分位至少70",
}
MINERVINI_METRIC_LABELS = {
    "close": "收盘价",
    "sma50": "50日均线",
    "sma150": "150日均线",
    "sma200": "200日均线",
    "sma200_20d_ago": "20日前200日均线",
    "high_52w": "52周高点",
    "low_52w": "52周低点",
    "rs_252d_percentile": "252日相对强度百分位",
}

Render = Callable[[Request, str, dict[str, Any]], HTMLResponse]


def build_watchlist_router(
    *,
    render: Render,
    repository: WatchlistRepository,
    market_reader: MarketDataReader,
    users: UserRepository,
) -> APIRouter:
    router = APIRouter()
    row_cache: dict[str, tuple[tuple[int, int], list[dict[str, Any]]]] = {}
    navigation_cache: dict[str, tuple[tuple[int, int], list[dict[str, Any]]]] = {}
    daily_aux_cache: dict[
        str, tuple[tuple[int, int], tuple[list[dict[str, Any]], list[dict[str, Any]]]]
    ] = {}

    def database_fingerprint() -> tuple[int, int]:
        def modified_at(path: Path) -> int:
            try:
                return path.stat().st_mtime_ns
            except FileNotFoundError:
                return -1

        return (modified_at(repository.path), modified_at(market_reader.path))

    def cached_rows(query_date: str, *, include_liquidity: bool) -> list[dict[str, Any]]:
        cache = row_cache if include_liquidity else navigation_cache
        fingerprint = database_fingerprint()
        if not include_liquidity:
            full_cached = row_cache.get(query_date)
            if full_cached is not None and full_cached[0] == fingerprint:
                return full_cached[1]
        cached = cache.get(query_date)
        if cached is not None and cached[0] == fingerprint:
            return cached[1]

        rows = [_decorate_watchlist_row(row) for row in repository.watchlist_rows(query_date)]
        for row in rows:
            row["manual"] = {
                "manual_state": "UNREVIEWED",
                "note": "",
                "reviewed_at": "",
            }
        _attach_market_metrics(
            rows,
            market_reader,
            query_date,
            include_liquidity=include_liquidity,
        )
        # 涨跌信息只需读取相邻两个交易日；首页保留它，但不必为了展示而
        # 计算 20 日成交额窗口。
        _attach_quote_changes(rows, market_reader, query_date)
        if len(cache) >= 4:
            cache.pop(next(iter(cache)))
        cache[query_date] = (fingerprint, rows)
        return rows

    def cached_daily_auxiliary(
        query_date: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        fingerprint = database_fingerprint()
        cached = daily_aux_cache.get(query_date)
        if cached is not None and cached[0] == fingerprint:
            return cached[1]

        index_rows = [_decorate_index(row) for row in repository.index_facts(query_date)]
        industry_rows = [
            _decorate_industry(row)
            for row in repository.industry_observations(query_date)
            if int(row.get("union_pass_count") or 0) > 0
        ][:10]
        if len(daily_aux_cache) >= 4:
            daily_aux_cache.pop(next(iter(daily_aux_cache)))
        value = (index_rows, industry_rows)
        daily_aux_cache[query_date] = (fingerprint, value)
        return value

    def clear_row_caches() -> None:
        row_cache.clear()
        navigation_cache.clear()
        daily_aux_cache.clear()

    def rows_for_user(
        request: Request, query_date: str, *, include_liquidity: bool
    ) -> list[dict[str, Any]]:
        rows = cached_rows(query_date, include_liquidity=include_liquidity)
        user = current_user(request)
        reviews = (
            users.reviews_for_symbols(
                user.user_id, [str(row.get("symbol") or "") for row in rows]
            )
            if user is not None
            else {}
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            review = reviews.get(
                str(row.get("symbol") or ""),
                {"manual_state": "UNREVIEWED", "note": "", "reviewed_at": ""},
            )
            result.append(
                {
                    **row,
                    "manual": review,
                    "manual_label": MANUAL_LABELS.get(
                        str(review.get("manual_state") or "UNREVIEWED"), "未加入"
                    ),
                }
            )
        return result

    def require_csrf(request: Request, user: AuthenticatedUser, supplied: str) -> None:
        if not users.csrf_valid(user, supplied):
            raise HTTPException(status_code=403, detail="CSRF 校验失败")

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

    @router.get("/a/observations", response_class=HTMLResponse)
    def personal_observations(
        request: Request,
        date: str | None = None,
        state: str = "WATCH",
    ) -> Response:
        user = current_user(request)
        if user is None:
            return RedirectResponse("/login?next=/a/observations", status_code=303)
        selected_state = state.upper()
        if selected_state not in {"WATCH", "FOCUS", "ARCHIVED"}:
            selected_state = "WATCH"
        query_date = _selected_date(repository, date, market_reader)
        reviews = users.reviews_for_user(user.user_id)
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
        visible_items = [
            item for item in items if item["manual"]["manual_state"] == selected_state
        ]
        return render(
            request,
            "ashare/personal_observations.html",
            {
                "active": "我的观察",
                "query_date": query_date,
                "latest_date": repository.latest_fact_date(),
                "selected_state": selected_state,
                "counts": counts,
                "items": visible_items,
                "manual_labels": MANUAL_LABELS,
            },
        )

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
        rows = rows_for_user(request, query_date, include_liquidity=False)
        base_rows = _base_filter_rows(
            rows,
            manual="all",
            min_cap=market_cap_floor,
            industry=industry,
            q=q,
        )
        current_counts = {
            "all": len(_current_rows(base_rows, "all")),
            "weinstein": len(_current_rows(base_rows, "weinstein")),
            "minervini": len(_current_rows(base_rows, "minervini")),
            "both": len(_current_rows(base_rows, "both")),
        }
        method_ready = method_mode in METHOD_LABELS
        change_counts = {
            value: len(_change_rows(base_rows, method_mode, value))
            if method_ready
            else 0
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
        filtered = (
            _current_rows(base_rows, method_mode)
            if view_mode == "current"
            else _change_rows(base_rows, method_mode, state_mode)
            if method_ready
            else []
        )
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
                    "result_count": len(filtered),
                    "reconstructed": sum(
                        1 for row in base_rows if row.get("origin") == "RECONSTRUCTED"
                    ),
                    "excluded_st": sum(
                        1 for row in rows if row["has_pass"] and row.get("is_st")
                    ),
                    "small_cap": sum(
                        1
                        for row in rows
                        if row["has_pass"]
                        and not row.get("is_st")
                        and row.get("small_cap")
                    ),
                    "missing_market_cap": sum(
                        1
                        for row in rows
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
                        target_method=method_mode if method_ready else "all",
                        target_state=state_mode or "NEW",
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
                    "clear_industry": daily_url(
                        target_view=view_mode,
                        target_method=method_mode,
                        target_state=state_mode,
                        target_industry="",
                    ),
                },
            },
        )

    @router.get("/a/industries", response_class=HTMLResponse)
    def industries(
        request: Request,
        date: str | None = None,
        method: str = "all",
        min_members: int = Query(default=1, ge=1, le=10000),
        q: str = "",
    ) -> HTMLResponse:
        query_date = _selected_date(repository, date, market_reader)
        rows = [_decorate_industry(row) for row in repository.industry_observations(query_date)]
        query = q.strip().lower()
        filtered = []
        for row in rows:
            if int(row.get("eligible_member_count") or 0) < min_members:
                continue
            if method == "weinstein" and int(row.get("weinstein_pass_count") or 0) <= 0:
                continue
            if method == "minervini" and int(row.get("minervini_pass_count") or 0) <= 0:
                continue
            if method == "both" and int(row.get("both_pass_count") or 0) <= 0:
                continue
            if query and query not in " ".join(
                [str(row.get("industry_code") or ""), str(row.get("industry_name") or "")]
            ).lower():
                continue
            filtered.append(row)
        return render(
            request,
            "ashare/watchlist_industries.html",
            {
                "active": "行业观察",
                "query_date": query_date,
                "latest_date": repository.latest_fact_date(),
                "available_dates": repository.available_dates(30),
                "rows": filtered,
                "filters": {"method": method, "min_members": min_members, "q": q},
                "summary": {
                    "industries": len(rows),
                    "active": sum(1 for row in rows if int(row.get("union_pass_count") or 0) > 0),
                    "small_sample": sum(1 for row in rows if row.get("quality_state") == "SMALL_SAMPLE"),
                    "unknown": sum(1 for row in rows if row.get("quality_state") == "UNKNOWN"),
                    "coverage": (
                        rows[0].get("membership_coverage_pct") if rows else None
                    ),
                },
            },
        )

    @router.get("/a/industries/{industry_code}/chart", response_class=HTMLResponse)
    def industry_chart(
        request: Request,
        industry_code: str,
        date: str | None = None,
    ) -> HTMLResponse:
        query_date = _selected_date(repository, date, market_reader)
        payload = repository.industry_detail(query_date, industry_code)
        if not payload:
            raise HTTPException(status_code=404, detail="industry observation not found")
        members = [_decorate_industry_member(row) for row in payload["members"]]
        analysis_bars = market_reader.safe_industry_proxy_bars(
            [str(row.get("symbol") or "") for row in members],
            query_date,
            limit=320,
        )
        bars = analysis_bars[-180:]
        observation = _decorate_industry(dict(payload["observation"]))
        confirmation = _decorate_industry_confirmation(
            build_industry_weinstein_confirmation(analysis_bars, observation)
        )
        return render(
            request,
            "ashare/watchlist_industry_chart.html",
            {
                "active": "行业观察",
                "query_date": query_date,
                "latest_date": repository.latest_fact_date(),
                "observation": observation,
                "confirmation": confirmation,
                "members": members,
                "bars": bars,
                "chart": _build_candlestick_chart(bars),
            },
        )

    @router.get("/a/indices", response_class=HTMLResponse)
    def indices(request: Request, date: str | None = None) -> HTMLResponse:
        query_date = date or repository.latest_fact_date() or _safe_market_date(market_reader)
        rows = [_decorate_index(row) for row in repository.index_facts(query_date)]
        return render(
            request,
            "ashare/watchlist_indices.html",
            {
                "active": "四指数判断",
                "query_date": query_date,
                "latest_date": repository.latest_fact_date(),
                "indices": rows,
                "weinstein_policy_version": (
                    str(rows[0].get("policy_version") or "") if rows else "weinstein-stage-30w-v1"
                ),
                "minervini_policy_version": (
                    str(rows[0].get("minervini", {}).get("policy_version") or "")
                    if rows
                    else "minervini-index-stage2-price-template-v1"
                ),
            },
        )

    @router.get("/a/review", response_class=HTMLResponse)
    def trade_review(request: Request) -> Response:
        user = current_user(request)
        if user is None:
            return RedirectResponse("/login?next=/a/review", status_code=303)
        names = repository.stock_names(users.trade_symbols(user.user_id))
        review = users.trade_review(user.user_id, names)
        for item in review["closed"]:
            item["planned_r_multiple"] = None
            item["actual_r_multiple"] = None
            item["actual_drawdown_low"] = None
            if float(item["pnl"]) <= 0:
                continue
            entry_price = float(item["entry_price"])
            exit_price = float(item["exit_price"])
            stop_price = item.get("stop_price")
            if stop_price is not None and entry_price > float(stop_price):
                item["planned_r_multiple"] = round(
                    (exit_price - entry_price) / (entry_price - float(stop_price)), 2
                )
            drawdown_low = market_reader.safe_trade_drawdown_low(
                str(item["symbol"]), str(item["buy_date"]), str(item["sell_date"])
            )
            if drawdown_low is not None and drawdown_low < entry_price:
                item["actual_drawdown_low"] = drawdown_low
                item["actual_r_multiple"] = round(
                    (exit_price - entry_price) / (entry_price - drawdown_low), 2
                )
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

    @router.get("/a/stocks/{symbol}", response_class=HTMLResponse)
    def stock_detail(
        request: Request,
        symbol: str,
        date: str | None = None,
        view: str | None = None,
        method: str = "all",
        state: str = "all",
        manual: str = "all",
        min_cap: int = Query(default=50),
        industry: str = "",
        q: str = "",
        section: str = "",
        edit_trade: str = "",
        traded_on: str = "",
        trade_price: str = "",
    ) -> HTMLResponse:
        payload = repository.stock_detail(symbol)
        if not payload["latest"]:
            raise HTTPException(status_code=404, detail="stock has no watchlist facts")
        user = current_user(request)
        payload["manual"] = (
            users.review(user.user_id, symbol)
            if user is not None
            else {
                "symbol": symbol.upper(),
                "manual_state": "UNREVIEWED",
                "note": "",
                "reviewed_at": "",
            }
        )
        for row in payload["latest"].values():
            row["result_label"] = RESULT_LABELS.get(str(row.get("result") or ""), "未知")
            row["state_label"] = STATE_LABELS.get(str(row.get("state") or ""), "未发生变化")
            profile = dict(row.get("evidence", {}).get("profile") or {})
            if str(row.get("method") or "") == "minervini":
                profile["checks"] = _minervini_check_results(profile)
            row["profile"] = profile
            row["stage_label"] = STAGE_LABELS.get(str(profile.get("stage") or ""), "")
        for row in payload["history"]:
            row["method_label"] = METHOD_LABELS.get(str(row.get("method") or ""), "")
            row["result_label"] = RESULT_LABELS.get(str(row.get("result") or ""), "未知")
            row["state_label"] = STATE_LABELS.get(str(row.get("state") or ""), "")
        recent_history = payload["history"][:20]
        change_history = [
            row
            for row in payload["history"]
            if str(row.get("state") or "")
            in {"ENTERED", "REENTERED", "EXITED", "DATA_GAP"}
        ]
        visible_by_key = {
            (str(row.get("as_of_date") or ""), str(row.get("method") or "")): row
            for row in [*recent_history, *change_history]
        }
        payload["visible_history"] = sorted(
            visible_by_key.values(),
            key=lambda row: (str(row.get("as_of_date") or ""), str(row.get("method") or "")),
            reverse=True,
        )[:120]
        payload["manual_label"] = MANUAL_LABELS.get(
            str(payload["manual"].get("manual_state") or "UNREVIEWED"), "未加入"
        )
        fact_date = max(
            (str(row.get("as_of_date") or "") for row in payload["latest"].values()),
            default="",
        )
        query_date = date or fact_date
        editing_trade = (
            users.trade_execution(user.user_id, edit_trade)
            if user is not None and edit_trade
            else None
        )
        trade_prefill: dict[str, Any] = {}
        if traded_on:
            try:
                calendar_date.fromisoformat(traded_on)
                trade_prefill["traded_on"] = traded_on
            except ValueError:
                pass
        if trade_price:
            try:
                price = float(trade_price)
                if price > 0:
                    trade_prefill["price"] = price
            except ValueError:
                pass
        if editing_trade is not None and str(editing_trade["symbol"]) != symbol.upper():
            raise HTTPException(status_code=404, detail="trade does not belong to this stock")
        navigation = _stock_navigation(
            repository,
            market_reader,
            symbol=symbol,
            query_date=query_date,
            view=view,
            method=method,
            state=state,
            manual=manual,
            min_cap=_market_cap_floor(min_cap),
            industry=industry,
            q=q,
            section=section,
            rows_for_date=lambda value: rows_for_user(
                request, value, include_liquidity=False
            ),
        )
        payload["market_metrics"] = market_reader.safe_stock_market_metrics(
            query_date,
            [symbol],
        ).get(symbol.upper(), {})
        industry_code = str(payload["industry"].get("industry_code") or "")
        if industry_code:
            industry_payload = repository.industry_detail(query_date, industry_code)
            if industry_payload:
                industry_observation = _decorate_industry(
                    dict(industry_payload["observation"])
                )
                industry_bars = market_reader.safe_industry_proxy_bars(
                    [str(row.get("symbol") or "") for row in industry_payload["members"]],
                    query_date,
                    limit=320,
                )
                payload["industry_confirmation"] = _decorate_industry_confirmation(
                    build_industry_weinstein_confirmation(
                        industry_bars, industry_observation
                    )
                )
        payload["external_urls"] = _stock_external_urls(symbol)
        return render(
            request,
            "ashare/watchlist_stock.html",
            {
                "active": "大师观察池",
                "query_date": query_date,
                "latest_date": repository.latest_fact_date(),
                "stock": payload,
                "manual_labels": MANUAL_LABELS,
                "minervini_check_labels": MINERVINI_CHECK_LABELS,
                "minervini_metric_labels": MINERVINI_METRIC_LABELS,
                "trade_setup_labels": TRADE_SETUP_LABELS,
                "editing_trade": editing_trade,
                "trade_prefill": trade_prefill,
                "navigation": navigation,
                "private_workspace": user is not None,
            },
        )

    @router.get("/a/stocks/{symbol}/chart", response_class=HTMLResponse)
    def stock_chart(
        request: Request,
        symbol: str,
        date: str | None = None,
        view: str | None = None,
        method: str = "all",
        state: str = "all",
        manual: str = "all",
        min_cap: int = Query(default=50),
        industry: str = "",
        q: str = "",
        section: str = "",
        nav_previous: str = "",
        nav_next: str = "",
        nav_position: str = "",
        nav_total: str = "",
    ) -> HTMLResponse:
        payload = repository.stock_detail(symbol)
        if not payload["latest"]:
            raise HTTPException(status_code=404, detail="stock has no watchlist facts")
        user = current_user(request)
        payload["manual"] = (
            users.review(user.user_id, symbol)
            if user is not None
            else {
                "symbol": symbol.upper(),
                "manual_state": "UNREVIEWED",
                "note": "",
                "reviewed_at": "",
            }
        )
        payload["manual_label"] = MANUAL_LABELS.get(
            str(payload["manual"].get("manual_state") or "UNREVIEWED"), "未加入"
        )
        query_date = date or max(
            (str(row.get("as_of_date") or "") for row in payload["latest"].values()),
            default="",
        )
        navigation = _stock_navigation(
            repository,
            market_reader,
            symbol=symbol,
            query_date=query_date,
            view=view,
            method=method,
            state=state,
            manual=manual,
            min_cap=_market_cap_floor(min_cap),
            industry=industry,
            q=q,
            section=section,
            rows_for_date=lambda value: rows_for_user(
                request, value, include_liquidity=False
            ),
        )
        submitted_position = _navigation_count(nav_position)
        submitted_total = _navigation_count(nav_total)
        if 0 < submitted_position <= submitted_total and (nav_previous or nav_next):
            submitted_symbols = {
                value.upper().strip() for value in (nav_previous, nav_next) if value.strip()
            }
            submitted_names = repository.stock_names(submitted_symbols)
            navigation.update(
                {
                    "position": submitted_position,
                    "total": submitted_total,
                    "previous": _navigation_item(nav_previous, submitted_names),
                    "next": _navigation_item(nav_next, submitted_names),
                }
            )
        chart_methods = _chart_method_statuses(payload["history"], query_date)
        market_metrics = market_reader.safe_stock_market_metrics(query_date, [symbol]).get(
            symbol.upper(), {}
        )
        return render(
            request,
            "ashare/watchlist_stock_chart.html",
            {
                "active": "大师观察池",
                "query_date": query_date,
                "latest_date": repository.latest_fact_date(),
                "symbol": symbol.upper(),
                "stock_name": str(payload["identity"].get("name") or symbol.upper()),
                "industry_name": str(payload["industry"].get("industry_name") or "行业待补"),
                "market_metrics": market_metrics,
                "external_urls": _stock_external_urls(symbol),
                "stock": payload,
                "manual_labels": MANUAL_LABELS,
                "chart_methods": chart_methods,
                "is_observation_today": any(
                    item["result"] == "PASS" and item["as_of_date"] == query_date
                    for item in chart_methods
                ),
                "back_url": f"/a/stocks/{symbol.upper()}?{navigation['query']}",
                "navigation": navigation,
                "private_workspace": user is not None,
            },
        )

    @router.post("/a/stocks/{symbol}/review", response_class=RedirectResponse)
    async def save_stock_review(request: Request, symbol: str) -> RedirectResponse:
        user = require_user(request)
        values = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        require_csrf(request, user, str((values.get("csrf_token") or [""])[0]))
        state = str((values.get("manual_state") or ["UNREVIEWED"])[0])
        note_values = values.get("note")
        note = (
            str(note_values[0])
            if note_values is not None
            else str(users.review(user.user_id, symbol).get("note") or "")
        )
        return_to = str((values.get("return_to") or [""])[0])
        try:
            users.save_review(user.user_id, symbol, state, note)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        chart_prefix = f"/a/stocks/{symbol.upper()}/chart"
        if return_to.startswith(chart_prefix) and (
            len(return_to) == len(chart_prefix)
            or return_to[len(chart_prefix)] in "?#"
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

    @router.post("/a/stocks/{symbol}/trades", response_class=RedirectResponse)
    async def record_stock_trade(request: Request, symbol: str) -> RedirectResponse:
        user = require_user(request)
        values = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)

        def value(key: str, default: str = "") -> str:
            return str((values.get(key) or [default])[0])

        stop_price = value("stop_price")
        require_csrf(request, user, value("csrf_token"))

        try:
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
                method="MANUAL", setup_method=value("setup_method", "PULLBACK"),
                stop_price=float(stop_price) if stop_price else None,
                rationale=value("rationale"),
                invalidation=value("invalidation"),
                exit_reason=value("exit_reason"),
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
            traded_on = value("traded_on")
            users.update_trade(
                user.user_id,
                execution_id,
                traded_on=traded_on, traded_at=value("traded_at"), side=value("side"),
                quantity=int(value("quantity")), price=float(value("price")),
                fee=float(value("fee", "0") or "0"), method="MANUAL",
                setup_method=value("setup_method", "PULLBACK"),
                stop_price=float(stop_price) if stop_price else None,
                rationale=value("rationale"), invalidation=value("invalidation"),
                exit_reason=value("exit_reason"), market_context=value("market_context"),
                observation_snapshot=repository.observation_snapshot(
                    str(existing["symbol"]), traded_on
                ),
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(f"/a/stocks/{existing['symbol']}#trade-journal", status_code=303)

    @router.get("/api/a/stocks/{symbol}/chart")
    def api_stock_chart(
        symbol: str,
        date: str,
        length: int = Query(default=20, ge=1, le=250),
        multiplier: float = Query(default=2.25, gt=0, le=20),
        source: str = Query(default="close", pattern="^(close|open|high|low)$"),
        use_ema: bool = True,
        band_style: str = Query(default="atr", pattern="^(atr|tr|range)$"),
        atr_length: int = Query(default=10, ge=1, le=250),
        limit: int | None = Query(default=None, ge=30),
        interval: str = Query(default="day", pattern="^(day|week)$"),
    ) -> dict[str, Any]:
        bars = market_reader.safe_stock_chart_bars(symbol, date, limit=limit)
        if interval == "week":
            bars = _weekly_bars(bars)
        if len(bars) < 2:
            return {"status": "DATA_GAP", "reason": "INSUFFICIENT_DAILY_OHLC", "bars": []}
        scale_ids = {str(row.get("price_scale_id") or "") for row in bars}
        if len(scale_ids) != 1 or not next(iter(scale_ids)):
            return {"status": "DATA_GAP", "reason": "INCONSISTENT_PRICE_SCALE", "bars": []}
        price_scale_id = next(iter(scale_ids))
        return {
            "status": "OK",
            "symbol": symbol.upper(),
            "as_of_date": date,
            "adjustment": "qfq",
            "interval": interval,
            "price_scale_id": price_scale_id,
            "bars": bars,
            "keltner": keltner_channels(
                bars,
                length=length,
                multiplier=multiplier,
                source=source,
                use_ema=use_ema,
                band_style=band_style,
                atr_length=atr_length,
            ),
        }

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
            week_end = {
                _week_key(str(row["trade_date"])): str(row["trade_date"])
                for row in bars
            }
            for execution in overlay["executions"]:
                execution["traded_on"] = week_end.get(
                    _week_key(str(execution["traded_on"])), str(execution["traded_on"])
                )
        return {
            "drawings": users.chart_drawings(user.user_id, symbol, price_scale_id),
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
            "deleted": users.delete_chart_drawing(
                user.user_id, drawing_id, symbol, price_scale_id
            )
        }

    @router.get("/a/runs", response_class=HTMLResponse)
    def runs(request: Request) -> HTMLResponse:
        return render(
            request,
            "ashare/watchlist_runs.html",
            {
                "active": "",
                "latest_date": repository.latest_fact_date(),
                "query_date": repository.latest_fact_date(),
                "runs": repository.run_receipts(30),
                "collections": market_reader.collection_receipts(30),
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

    @router.get("/api/a/industries/{query_date}")
    def api_industries(query_date: str) -> dict[str, Any]:
        return {
            "query_date": query_date,
            "taxonomy": "SW2021",
            "industry_level": "L3",
            "policy_version": INDUSTRY_POLICY_VERSION,
            "rows": [
                _decorate_industry(row)
                for row in repository.industry_observations(query_date)
            ],
        }

    @router.get("/api/a/industries/{query_date}/{industry_code}")
    def api_industry_detail(query_date: str, industry_code: str) -> dict[str, Any]:
        payload = repository.industry_detail(query_date, industry_code)
        if not payload:
            raise HTTPException(status_code=404, detail="industry observation not found")
        return {"query_date": query_date, **payload}

    @router.get("/api/a/indices/{query_date}")
    def api_indices(query_date: str) -> dict[str, Any]:
        return {
            "query_date": query_date,
            "methods": {
                "weinstein": "full_stage",
                "minervini": "stage2_only",
            },
            "rows": [_decorate_index(row) for row in repository.index_facts(query_date)],
        }

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


def _selected_date(
    repository: WatchlistRepository,
    requested: str | None,
    market_reader: MarketDataReader,
) -> str:
    if requested and repository.has_fact_date(requested):
        return requested
    return repository.latest_fact_date() or requested or _safe_market_date(market_reader)


def _normalize_daily_query(
    *, view: str | None, method: str, state: str
) -> tuple[str, str, str]:
    method_mode = method.lower().strip()
    if method_mode not in {"all", "both", *METHOD_LABELS}:
        method_mode = "all"

    state_mode = state.upper().strip()
    requested_view = str(view or "").lower().strip()
    if requested_view not in {"current", "changes"}:
        requested_view = (
            "changes"
            if state_mode
            in {
                "NEW",
                "ENTERED",
                "REENTERED",
                "STABLE",
                "CONTINUING",
                "EXIT",
                "EXITED",
                "DATA_GAP",
            }
            else "current"
        )
    if requested_view == "current":
        return "current", method_mode, ""

    if state_mode == "STABLE":
        state_mode = "CONTINUING"
    if state_mode not in {
        "NEW",
        "ENTERED",
        "REENTERED",
        "CONTINUING",
        "EXIT",
        "EXITED",
        "DATA_GAP",
    }:
        state_mode = "NEW"
    return "changes", method_mode, state_mode


def _daily_query_string(
    *,
    query_date: str,
    view: str,
    method: str,
    state: str,
    min_cap: int,
    industry: str,
    q: str,
) -> str:
    query: dict[str, str | int] = {
        "date": query_date,
        "view": view,
        "method": method,
        "min_cap": min_cap,
    }
    if view == "changes" and state:
        query["state"] = state
    if industry:
        query["industry"] = industry
    if q.strip():
        query["q"] = q.strip()
    return urlencode(query)


def _safe_market_date(market_reader: MarketDataReader) -> str:
    return market_reader.safe_latest_market_date()


def _decorate_watchlist_row(row: dict[str, Any]) -> dict[str, Any]:
    methods = dict(row.get("methods") or {})
    states = [str(item.get("state") or "") for item in methods.values()]
    item = dict(row)
    for method, method_row in methods.items():
        method_row["method_label"] = METHOD_LABELS.get(method, method)
        method_row["result_label"] = RESULT_LABELS.get(
            str(method_row.get("result") or ""), "未知"
        )
        method_row["state_label"] = STATE_LABELS.get(
            str(method_row.get("state") or ""), "未发生变化"
        )
        profile = dict(method_row.get("evidence", {}).get("profile") or {})
        method_row["stage_label"] = STAGE_LABELS.get(str(profile.get("stage") or ""), "")
    item["both_pass"] = all(
        methods.get(method, {}).get("result") == "PASS"
        for method in ("minervini", "weinstein")
    )
    item["has_pass"] = any(value.get("result") == "PASS" for value in methods.values())
    item["has_new_state"] = any(state in {"ENTERED", "REENTERED"} for state in states)
    item["has_exit_state"] = any(state in {"EXITED", "DATA_GAP"} for state in states)
    item["max_streak"] = max(
        (int(value.get("consecutive_sessions") or 0) for value in methods.values()),
        default=0,
    )
    item["state_labels"] = [STATE_LABELS[state] for state in states if state in STATE_LABELS]
    item["method_labels"] = [METHOD_LABELS[key] for key in methods if key in METHOD_LABELS]
    item["manual_label"] = MANUAL_LABELS.get(
        str(item.get("manual", {}).get("manual_state") or "UNREVIEWED"), "未加入"
    )
    item.update(_stock_external_urls(str(item.get("symbol") or "")))
    return item


def _chart_method_statuses(
    history: list[dict[str, Any]], query_date: str
) -> list[dict[str, str]]:
    """Return independent method facts as of the chart date; do not combine them."""

    result: list[dict[str, str]] = []
    for method in ("minervini", "weinstein"):
        row = next(
            (
                item
                for item in history
                if str(item.get("method") or "") == method
                and str(item.get("as_of_date") or "") <= query_date
            ),
            None,
        )
        if row is None:
            result.append(
                {
                    "method": method,
                    "label": METHOD_LABELS[method],
                    "result": "UNKNOWN",
                    "result_label": "数据不足",
                    "as_of_date": "",
                }
            )
            continue
        method_result = str(row.get("result") or "UNKNOWN")
        result.append(
            {
                "method": method,
                "label": METHOD_LABELS[method],
                "result": method_result,
                "result_label": RESULT_LABELS.get(method_result, "未知"),
                "as_of_date": str(row.get("as_of_date") or ""),
            }
        )
    return result


def _attach_market_metrics(
    rows: list[dict[str, Any]],
    market_reader: MarketDataReader,
    as_of_date: str,
    *,
    include_liquidity: bool = True,
) -> None:
    metrics = market_reader.safe_stock_market_metrics(
        as_of_date,
        [str(row.get("symbol") or "") for row in rows],
        include_liquidity=include_liquidity,
    )
    for row in rows:
        values = metrics.get(str(row.get("symbol") or "").upper(), {})
        row.update(values)
        total_cap = row.get("total_market_cap_yi")
        row["small_cap"] = total_cap is not None and float(total_cap) < 50.0


def _attach_quote_changes(
    rows: list[dict[str, Any]],
    market_reader: MarketDataReader,
    as_of_date: str,
) -> None:
    quotes = market_reader.safe_stock_quote_changes(
        as_of_date,
        [str(row.get("symbol") or "") for row in rows],
    )
    for row in rows:
        values = quotes.get(
            str(row.get("symbol") or "").upper(),
            {"close": None, "change_amount": None, "change_pct": None},
        )
        row.update(values)
        change_pct = row.get("change_pct")
        row["change_class"] = (
            "up" if change_pct is not None and float(change_pct) > 0
            else "down" if change_pct is not None and float(change_pct) < 0
            else "flat"
        )


def _navigation_count(value: str) -> int:
    """Tolerate installed-web-app URLs that fold the chart anchor into a value."""

    try:
        return max(0, int(value.partition("#")[0]))
    except ValueError:
        return 0


def _market_cap_floor(value: int) -> int:
    if value not in MARKET_CAP_FLOORS:
        raise HTTPException(
            status_code=422,
            detail="min_cap must be one of 0, 30, 50, or 100",
        )
    return value


def _decorate_index(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["stage_label"] = STAGE_LABELS.get(str(row.get("stage") or ""), "未知")
    evidence = dict(row.get("evidence") or {})
    item["metrics"] = evidence
    minervini = dict(row.get("minervini") or {})
    minervini_evidence = dict(minervini.get("evidence") or {})
    minervini_result = str(minervini.get("result") or "UNKNOWN")
    minervini["result_label"] = {
        "PASS": "是",
        "FAIL": "否",
        "UNKNOWN": "数据不足",
    }.get(minervini_result, "数据不足")
    minervini["status_class"] = {
        "PASS": "pass",
        "FAIL": "fail",
        "UNKNOWN": "unknown",
    }.get(minervini_result, "unknown")
    minervini["metrics"] = dict(minervini_evidence.get("metrics") or {})
    minervini["failed_checks"] = list(minervini_evidence.get("failed_checks") or [])
    minervini["failed_check_count"] = len(minervini["failed_checks"])
    item["minervini"] = minervini
    item.update(_index_external_urls(str(item.get("index_symbol") or "")))
    return item


def _decorate_industry(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    for key in ("weinstein_pass_rate", "minervini_pass_rate"):
        value = row.get(key)
        item[f"{key}_pct"] = round(float(value) * 100, 1) if value is not None else None
    item["quality_label"] = {
        "COMPLETE": "样本可比较",
        "SMALL_SAMPLE": "小样本，仅看数量",
        "UNKNOWN": "来源不足",
    }.get(str(row.get("quality_state") or ""), "未知")
    return item


def _decorate_industry_confirmation(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["stage_label"] = STAGE_LABELS.get(str(item.get("stage") or ""), "数据不足")
    coverage = item.get("price_coverage_pct")
    item["price_coverage_pct_label"] = (
        f"{float(coverage):.1f}%" if coverage is not None else "暂无"
    )
    return item


def _decorate_industry_member(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item.update(_stock_external_urls(str(item.get("symbol") or "")))
    for method in ("weinstein", "minervini"):
        result = str(item.get(f"{method}_result") or "")
        state = str(item.get(f"{method}_state") or "")
        item[f"{method}_result_label"] = RESULT_LABELS.get(result, "暂无事实")
        item[f"{method}_state_label"] = STATE_LABELS.get(state, "")
    return item


def _plain_code(symbol: str) -> str:
    return str(symbol or "").strip().upper().split(".", 1)[0]


def _stock_external_urls(symbol: str) -> dict[str, str]:
    value = str(symbol or "").strip().upper()
    code = _plain_code(value)
    suffix = value.split(".", 1)[1] if "." in value else ""
    exchange = {"SH": "SSE", "SZ": "SZSE", "BJ": "BSE"}.get(suffix, "")
    tradingview_symbol = f"{exchange}:{code}" if exchange and len(code) == 6 and code.isdigit() else ""
    tradingview_url = (
        f"https://cn.tradingview.com/chart/?symbol={quote(tradingview_symbol, safe='')}"
        if tradingview_symbol
        else ""
    )
    tonghuashun_url = (
        f"https://stockpage.10jqka.com.cn/{code}/"
        if len(code) == 6 and code.isdigit()
        else ""
    )
    return {
        "tradingview_url": tradingview_url,
        "tradingview_symbol": tradingview_symbol,
        "tonghuashun_url": tonghuashun_url,
    }


def _index_external_urls(symbol: str) -> dict[str, str]:
    urls = _stock_external_urls(symbol)
    code = _plain_code(symbol)
    suffix = str(symbol or "").upper().split(".", 1)[1] if "." in symbol else ""
    if suffix == "SH" and len(code) == 6:
        urls["tonghuashun_url"] = (
            f"https://q.10jqka.com.cn/zs/detail/code/1B{code[-4:]}/"
        )
    elif suffix == "SZ" and len(code) == 6:
        urls["tonghuashun_url"] = f"https://stockpage.10jqka.com.cn/{code}/index/"
    return urls


def _week_key(value: str) -> tuple[int, int]:
    parsed = calendar_date.fromisoformat(value)
    iso = parsed.isocalendar()
    return (iso.year, iso.week)


def _weekly_bars(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for bar in bars:
        grouped.setdefault(_week_key(str(bar["trade_date"])), []).append(bar)
    weekly: list[dict[str, Any]] = []
    for values in grouped.values():
        first, last = values[0], values[-1]
        weekly.append({
            **last,
            "open": first["open"],
            "high": max(float(item["high"]) for item in values),
            "low": min(float(item["low"]) for item in values),
            "close": last["close"],
            "volume": sum(float(item.get("volume") or 0) for item in values),
            "amount": sum(float(item.get("amount") or 0) for item in values),
        })
    return weekly


def _build_candlestick_chart(bars: list[dict[str, Any]]) -> dict[str, Any]:
    if not bars:
        return {}
    width, height = 1120.0, 430.0
    left, right, top, bottom = 66.0, 24.0, 20.0, 48.0
    plot_width = width - left - right
    plot_height = height - top - bottom
    minimum = min(float(row["low"]) for row in bars)
    maximum = max(float(row["high"]) for row in bars)
    span = max(maximum - minimum, maximum * 0.02, 1.0)
    minimum -= span * 0.05
    maximum += span * 0.05
    price_span = maximum - minimum

    def y(value: float) -> float:
        return round(top + (maximum - value) / price_span * plot_height, 2)

    slot = plot_width / len(bars)
    body_width = max(1.6, min(7.0, slot * 0.58))
    candles: list[dict[str, Any]] = []
    closes: list[float] = []
    for index, row in enumerate(bars):
        open_price = float(row["open"])
        close = float(row["close"])
        center = left + slot * (index + 0.5)
        open_y, close_y = y(open_price), y(close)
        candles.append(
            {
                "x": round(center, 2),
                "body_x": round(center - body_width / 2, 2),
                "body_y": min(open_y, close_y),
                "body_width": round(body_width, 2),
                "body_height": max(1.2, round(abs(close_y - open_y), 2)),
                "high_y": y(float(row["high"])),
                "low_y": y(float(row["low"])),
                "direction": "up" if close >= open_price else "down",
                "date": str(row["trade_date"]),
            }
        )
        closes.append(close)

    def moving_average_points(window: int) -> str:
        points = []
        for index in range(window - 1, len(closes)):
            average = sum(closes[index - window + 1 : index + 1]) / window
            center = left + slot * (index + 0.5)
            points.append(f"{center:.2f},{y(average):.2f}")
        return " ".join(points)

    tick_indexes = sorted(
        {
            0,
            len(bars) // 4,
            len(bars) // 2,
            len(bars) * 3 // 4,
            len(bars) - 1,
        }
    )
    date_ticks = [
        {
            "x": round(left + slot * (index + 0.5), 2),
            "label": str(bars[index]["trade_date"])[5:],
        }
        for index in tick_indexes
    ]
    price_ticks = []
    for index in range(5):
        value = maximum - price_span * index / 4
        price_ticks.append({"y": y(value), "label": f"{value:.1f}"})
    latest = dict(bars[-1])
    first_close = float(bars[0]["close"])
    latest_close = float(latest["close"])
    latest["period_return_pct"] = round((latest_close / first_close - 1) * 100, 2)
    return {
        "width": int(width),
        "height": int(height),
        "plot_left": left,
        "plot_right": width - right,
        "plot_top": top,
        "plot_bottom": height - bottom,
        "candles": candles,
        "ma20_points": moving_average_points(20),
        "ma50_points": moving_average_points(50),
        "date_ticks": date_ticks,
        "price_ticks": price_ticks,
        "latest": latest,
        "first_date": str(bars[0]["trade_date"]),
        "last_date": str(bars[-1]["trade_date"]),
    }


def _minervini_check_results(profile: dict[str, Any]) -> dict[str, bool]:
    existing = dict(profile.get("checks") or {})
    if existing:
        return {str(key): bool(value) for key, value in existing.items()}
    failed = {str(value) for value in profile.get("failed_checks") or []}
    if int(profile.get("evaluated_check_count") or 0) <= 0:
        return {}
    return {key: key not in failed for key in MINERVINI_CHECKS}


def _base_filter_rows(
    rows: list[dict[str, Any]],
    *,
    manual: str,
    min_cap: int,
    industry: str,
    q: str,
) -> list[dict[str, Any]]:
    query = q.strip().lower()
    result: list[dict[str, Any]] = []
    for row in rows:
        if row.get("is_st"):
            continue
        total_cap = row.get("total_market_cap_yi")
        if min_cap and (total_cap is None or float(total_cap) < min_cap):
            continue
        manual_state = str(row.get("manual", {}).get("manual_state") or "UNREVIEWED")
        if manual != "all" and manual_state != manual:
            continue
        if manual == "all" and manual_state == "DROPPED":
            continue
        if industry and str(row.get("industry_code") or "") != industry:
            continue
        if query and query not in " ".join(
            [
                str(row.get("symbol") or ""),
                str(row.get("name") or ""),
                str(row.get("industry") or ""),
            ]
        ).lower():
            continue
        result.append(row)
    return result


def _current_rows(rows: list[dict[str, Any]], method: str) -> list[dict[str, Any]]:
    if method == "both":
        return [row for row in rows if row["both_pass"]]
    if method in METHOD_LABELS:
        return [
            row
            for row in rows
            if row.get("methods", {}).get(method, {}).get("result") == "PASS"
        ]
    return [row for row in rows if row["has_pass"]]


def _change_rows(
    rows: list[dict[str, Any]], method: str, state: str
) -> list[dict[str, Any]]:
    if method not in METHOD_LABELS:
        return []

    def matches(row: dict[str, Any]) -> bool:
        method_state = str(row.get("methods", {}).get(method, {}).get("state") or "")
        if state == "NEW":
            return method_state in {"ENTERED", "REENTERED"}
        if state == "EXIT":
            return method_state in {"EXITED", "DATA_GAP"}
        return method_state == state

    return [row for row in rows if matches(row)]


def _sort_daily_rows(
    rows: list[dict[str, Any]], *, view: str, method: str
) -> list[dict[str, Any]]:
    if method not in METHOD_LABELS:
        return sorted(rows, key=lambda row: str(row.get("symbol") or ""))
    return sorted(
        rows,
        key=lambda row: (
            0
            if str(row.get("methods", {}).get(method, {}).get("state") or "")
            in {"ENTERED", "REENTERED"}
            else 1,
            -int(
                row.get("methods", {})
                .get(method, {})
                .get("consecutive_sessions")
                or 0
            ),
            str(row.get("symbol") or ""),
        ),
    )


def _prepare_daily_rows(
    rows: list[dict[str, Any]], *, method: str
) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        methods = dict(item.get("methods") or {})
        if method in METHOD_LABELS:
            method_items = [(method, methods.get(method, {}))]
        else:
            method_items = [
                (key, value)
                for key, value in methods.items()
                if value.get("result") == "PASS"
            ]
        item["display_facts"] = [
            {
                "method_label": METHOD_LABELS.get(key, key),
                "streak_started_on": str(value.get("streak_started_on") or ""),
                "consecutive_sessions": int(value.get("consecutive_sessions") or 0),
            }
            for key, value in method_items
        ]
        item["display_start_sort"] = next(
            (
                str(value.get("streak_started_on") or "")
                for _, value in method_items
                if value.get("streak_started_on")
            ),
            "",
        )
        item["display_max_streak"] = max(
            (int(value.get("consecutive_sessions") or 0) for _, value in method_items),
            default=0,
        )
        prepared.append(item)
    return prepared


def _filter_rows(
    rows: list[dict[str, Any]],
    *,
    method: str,
    state: str,
    manual: str,
    min_cap: int,
    industry: str,
    q: str,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in _base_filter_rows(
        rows,
        manual=manual,
        min_cap=min_cap,
        industry=industry,
        q=q,
    ):
        methods = row["methods"]
        if method == "both" and not row["both_pass"]:
            continue
        if method in METHOD_LABELS and method not in methods:
            continue
        if state == "PASSING" and not row["has_pass"]:
            continue
        if state == "NEW" and not row["has_new_state"]:
            continue
        if state == "STABLE" and not (
            row["has_pass"] and not row["has_new_state"] and not row["has_exit_state"]
        ):
            continue
        if state == "EXIT" and not row["has_exit_state"]:
            continue
        if state not in {"all", "PASSING", "NEW", "STABLE", "EXIT"} and not any(
            str(value.get("state") or "") == state for value in methods.values()
        ):
            continue
        result.append(row)
    return sorted(
        result,
        key=lambda row: (
            0 if row["has_new_state"] else 1,
            0 if str(row["manual"].get("manual_state") or "") == "FOCUS" else 1,
            -int(row["max_streak"]),
            str(row["symbol"]),
        ),
    )


def _navigation_item(
    symbol: str, names: dict[str, str] | None = None
) -> dict[str, str] | None:
    normalized = symbol.upper().strip()
    if not normalized:
        return None
    return {
        "symbol": normalized,
        "name": str((names or {}).get(normalized) or normalized),
    }


def _stock_navigation(
    repository: WatchlistRepository,
    market_reader: MarketDataReader,
    *,
    symbol: str,
    query_date: str,
    view: str | None,
    method: str,
    state: str,
    manual: str,
    min_cap: int,
    industry: str,
    q: str,
    section: str,
    rows_for_date: Callable[[str], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Keep chart navigation inside the selected daily-list context, not a global rank."""

    # URL fragments normally stay in the browser, but some installed web-app
    # clients have sent the trailing chart anchor as part of the query value.
    # Keep old/bookmarked links usable instead of losing the list context.
    section = section.partition("#")[0]
    view_mode, method_mode, state_mode = _normalize_daily_query(
        view=view,
        method=method,
        state=state,
    )
    query = urlencode(
        {
            "date": query_date,
            "view": view_mode,
            "method": method_mode,
            "state": state_mode,
            "manual": manual,
            "min_cap": min_cap,
            "industry": industry,
            "q": q,
            "section": section,
        }
    )
    list_url = "/a/daily?" + _daily_query_string(
        query_date=query_date,
        view=view_mode,
        method=method_mode,
        state=state_mode,
        min_cap=min_cap,
        industry=industry,
        q=q,
    )
    labels = {
        "new-candidates": "新进 / 重进",
        "continuing-candidates": "持续符合",
        "exit-candidates": "退出 / 中断",
        "daily-results": "当前观察池" if view_mode == "current" else "方法状态变化",
        "focus-candidates": "我的重点观察",
        "personal-observations": "我的观察",
    }
    empty: dict[str, Any] = {
        "query": query,
        "list_url": list_url,
        "section_label": labels.get(section, "当前列表"),
        "position": 0,
        "total": 0,
        "previous": None,
        "next": None,
    }
    if section not in labels or not query_date:
        return empty
    rows = (
        rows_for_date(query_date)
        if rows_for_date is not None
        else [_decorate_watchlist_row(row) for row in repository.watchlist_rows(query_date)]
    )
    if rows_for_date is None:
        _attach_market_metrics(rows, market_reader, query_date, include_liquidity=False)
    base_rows = _base_filter_rows(
        rows,
        manual=manual,
        min_cap=min_cap,
        industry=industry,
        q=q,
    )
    if section == "daily-results":
        selected = (
            _current_rows(base_rows, method_mode)
            if view_mode == "current"
            else _change_rows(base_rows, method_mode, state_mode)
        )
        selected = _sort_daily_rows(selected, view=view_mode, method=method_mode)
    else:
        filtered = _filter_rows(
            base_rows,
            method=method,
            state=state,
            manual="all",
            min_cap=0,
            industry="",
            q="",
        )
        selected = filtered
    if section != "daily-results":
        if section == "new-candidates":
            selected = (
                filtered
                if state == "PASSING"
                else [row for row in filtered if row["has_new_state"]]
            )
        elif section == "continuing-candidates":
            selected = [
                row
                for row in filtered
                if row["has_pass"]
                and not row["has_new_state"]
                and not row["has_exit_state"]
            ]
        elif section == "exit-candidates":
            selected = [row for row in filtered if row["has_exit_state"]]
        elif section == "focus-candidates":
            selected = [
                row
                for row in filtered
                if str(row["manual"].get("manual_state") or "") == "FOCUS"
            ]
    symbols = [str(row.get("symbol") or "").upper() for row in selected]
    try:
        index = symbols.index(symbol.upper())
    except ValueError:
        return empty

    def item_at(position: int) -> dict[str, str] | None:
        if position < 0 or position >= len(selected):
            return None
        row = selected[position]
        return {"symbol": str(row["symbol"]), "name": str(row.get("name") or row["symbol"])}

    return {
        "query": query,
        "list_url": list_url,
        "section_label": labels[section],
        "position": index + 1,
        "total": len(selected),
        "previous": item_at(index - 1),
        "next": item_at(index + 1),
    }
