from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date as calendar_date
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from ...watchlist.brooks import BROOKS_POLICY_VERSION
from ...watchlist.charting import (
    KELTNER_PRESETS,
    default_keltner_preset,
    keltner_channels,
    keltner_config_payload,
)
from ...watchlist.industry import INDUSTRY_POLICY_VERSION
from ...watchlist.industry_confirmation import build_industry_weinstein_confirmation
from ...watchlist.repository import MarketDataReader, WatchlistRepository
from ..display_time import sqlite_utc_to_shanghai
from ..owner import (
    OWNER_READING_NOTES,
    build_owner_activity_feed,
    build_owner_trade_feed,
)
from ..users import (
    C1_ENTRY_VARIANT_LABELS,
    C1_STRUCTURE_STATE_LABELS,
    OTHER_SETUP_ID,
    OTHER_SETUP_LABEL,
    AuthenticatedUser,
    UserRepository,
    normalize_observation_tags,
)
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
BROOKS_STATE_LABELS = {
    "S0_IMPULSE": "持续跟踪",
    "S1_TOUCH25": "历史 touch25",
    "S2_H1": "今日 H1",
    "S2_H1_H2_TAG": "历史 H1/H2",
    "COMPLETED": "主要路径完成",
    "INVALIDATED": "结构失效",
    "STALE": "观察超时",
    "PAUSED": "资格暂停",
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
EXIT_REASON_OPTIONS = (
    "部分止盈",
    "抵达阻力位",
    "高潮离场",
    "形态不符合预期",
)
MARKET_CAP_FLOORS = {0, 30, 50, 100}
BREADTH_BENCHMARKS = {
    "000985.CSI": "中证全指",
    "000001.SH": "上证综指",
    "000300.SH": "沪深300",
    "000852.SH": "中证1000",
    "399006.SZ": "创业板指",
    "000688.SH": "科创50",
}
STYLE_RELATIVE_PAIRS = (
    {
        "key": "small_cap",
        "label": "小盘 / 大盘",
        "left_symbol": "000852.SH",
        "left_name": "中证1000",
        "right_symbol": "000300.SH",
        "right_name": "沪深300",
        "color": "#2457a7",
    },
    {
        "key": "growth",
        "label": "创业成长 / 大盘",
        "left_symbol": "399006.SZ",
        "left_name": "创业板指",
        "right_symbol": "000300.SH",
        "right_name": "沪深300",
        "color": "#d28b18",
    },
    {
        "key": "star",
        "label": "科创成长 / 大盘",
        "left_symbol": "000688.SH",
        "left_name": "科创50",
        "right_symbol": "000300.SH",
        "right_name": "沪深300",
        "color": "#7656a5",
    },
)
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
PROVISIONAL_STATE_LABELS = {
    "PRE_ENTERED": "今日首次预进入",
    "PRE_REENTERED": "今日再次预进入",
    "PRE_CONTINUING": "继续预符合",
    "PRE_EXITED": "今日预退出",
    "PRE_OUT": "仍未进入",
    "DATA_GAP": "数据中断",
}
PROJECTION_RESULT_LABELS = {
    "PASS": "符合 Stage 2",
    "FAIL": "未符合 Stage 2",
    "TRANSITION": "转换期",
    "UNKNOWN": "数据不足",
}
WEINSTEIN_CHECK_LABELS = {
    "close_above_ma30": "收盘高于30周线",
    "ma30_slope_above_1pct": "30周线4周斜率高于1%",
    "return_13w_positive": "13周收益为正",
}

Render = Callable[[Request, str, dict[str, Any]], HTMLResponse]


def build_watchlist_router(
    *,
    render: Render,
    repository: WatchlistRepository,
    market_reader: MarketDataReader,
    users: UserRepository,
    site_owner_username: str,
) -> APIRouter:
    router = APIRouter()
    row_cache: dict[str, tuple[tuple[int, int], list[dict[str, Any]]]] = {}
    navigation_cache: dict[str, tuple[tuple[int, int], list[dict[str, Any]]]] = {}
    projection_cache: dict[str, tuple[tuple[int, int], list[dict[str, Any]]]] = {}
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
                "c1_record": {},
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

    def cached_projection_rows(query_date: str) -> list[dict[str, Any]]:
        fingerprint = database_fingerprint()
        cached = projection_cache.get(query_date)
        if cached is not None and cached[0] == fingerprint:
            return cached[1]
        rows = [_decorate_provisional_row(row) for row in repository.provisional_rows(query_date)]
        for row in rows:
            row["manual"] = {
                "manual_state": "UNREVIEWED",
                "note": "",
                "reviewed_at": "",
            }
        _attach_market_metrics(rows, market_reader, query_date, include_liquidity=False)
        _attach_quote_changes(rows, market_reader, query_date)
        if len(projection_cache) >= 4:
            projection_cache.pop(next(iter(projection_cache)))
        projection_cache[query_date] = (fingerprint, rows)
        return rows

    def clear_row_caches() -> None:
        row_cache.clear()
        navigation_cache.clear()
        daily_aux_cache.clear()
        projection_cache.clear()

    def rows_for_user(
        request: Request, query_date: str, *, include_liquidity: bool
    ) -> list[dict[str, Any]]:
        rows = cached_rows(query_date, include_liquidity=include_liquidity)
        user = current_user(request)
        reviews = (
            users.reviews_for_symbols(user.user_id, [str(row.get("symbol") or "") for row in rows])
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

    def projection_rows_for_user(request: Request, query_date: str) -> list[dict[str, Any]]:
        rows = cached_projection_rows(query_date)
        user = current_user(request)
        reviews = (
            users.reviews_for_symbols(user.user_id, [str(row.get("symbol") or "") for row in rows])
            if user is not None
            else {}
        )
        return [
            {
                **row,
                "manual": reviews.get(
                    str(row.get("symbol") or ""),
                    {"manual_state": "UNREVIEWED", "note": "", "reviewed_at": ""},
                ),
            }
            for row in rows
        ]

    def stock_navigation_rows_for_user(
        request: Request,
        query_date: str,
        *,
        view: str | None,
        method: str,
        state: str,
    ) -> list[dict[str, Any]]:
        view_mode, _, _ = _normalize_daily_query(
            view=view,
            method=method,
            state=state,
        )
        if view_mode == "projection":
            return projection_rows_for_user(request, query_date)
        return rows_for_user(request, query_date, include_liquidity=False)

    def personal_navigation_rows(request: Request) -> list[dict[str, Any]]:
        """Mirror the complete personal workspace, including stocks absent today."""
        user = current_user(request)
        if user is None:
            return []
        reviews = users.reviews_for_user(user.user_id)
        names = repository.stock_names({str(review.get("symbol") or "") for review in reviews})
        return [
            {
                "symbol": str(review.get("symbol") or ""),
                "name": names.get(str(review.get("symbol") or "")) or "名称待补",
                "manual": review,
            }
            for review in reviews
        ]

    def open_position_navigation_rows(request: Request) -> list[dict[str, Any]]:
        """Mirror the current user's open-position table for chart navigation."""
        user = current_user(request)
        if user is None:
            return []
        names = repository.stock_names(users.trade_symbols(user.user_id))
        review = users.trade_review(user.user_id, names)
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in review["open_positions"]:
            symbol = str(item.get("symbol") or "").upper()
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            rows.append(
                {
                    "symbol": symbol,
                    "name": str(item.get("stock_name") or symbol or "名称待补"),
                }
            )
        return rows

    def brooks_navigation_rows(
        query_date: str,
        *,
        state: str,
        industry: str,
        min_amount: float,
        q: str,
    ) -> list[dict[str, Any]]:
        if not query_date:
            return []
        rows = (
            repository.brooks_h1_rows(
                query_date, BROOKS_POLICY_VERSION, lookback_dates=20
            )
            if state == "history"
            else repository.brooks_rows(query_date, BROOKS_POLICY_VERSION)
        )
        return _filter_brooks_rows(
            rows,
            state=state,
            industry=industry,
            min_amount=min_amount,
            q=q,
        )

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
        date: str | None = None,
        state: str = "WATCH",
        tag: str = "",
    ) -> Response:
        user = current_user(request)
        if user is None:
            return RedirectResponse("/login?next=/a/observations", status_code=303)
        selected_state = state.upper()
        if selected_state not in {"WATCH", "FOCUS", "ARCHIVED"}:
            selected_state = "WATCH"
        query_date = _selected_date(repository, date, market_reader)
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
        observation_query = {"state": selected_state, "date": query_date}
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
            "cycle_done",
            "h1",
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
        daily_rows = (
            repository.brooks_rows(query_date, BROOKS_POLICY_VERSION) if query_date else []
        )
        history_rows = (
            repository.brooks_h1_rows(
                query_date, BROOKS_POLICY_VERSION, lookback_dates=lookback
            )
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
                "new",
                "expanding",
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
            if (
                query
                and query
                not in " ".join(
                    [str(row.get("industry_code") or ""), str(row.get("industry_name") or "")]
                ).lower()
            ):
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
                    "small_sample": sum(
                        1 for row in rows if row.get("quality_state") == "SMALL_SAMPLE"
                    ),
                    "unknown": sum(1 for row in rows if row.get("quality_state") == "UNKNOWN"),
                    "coverage": (rows[0].get("membership_coverage_pct") if rows else None),
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
            limit=None,
        )
        bars = analysis_bars
        observation = _decorate_industry(dict(payload["observation"]))
        navigation = _industry_navigation(
            [_decorate_industry(row) for row in repository.industry_observations(query_date)],
            industry_code=industry_code,
        )
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
                "chart": _industry_chart_summary(bars),
                "navigation": navigation,
            },
        )

    @router.get("/a/indices", response_class=HTMLResponse)
    def indices(
        request: Request,
        date: str | None = None,
        benchmark: str = "000985.CSI",
    ) -> HTMLResponse:
        query_date = _selected_date(repository, date, market_reader)
        benchmark_symbol = (
            benchmark.upper() if benchmark.upper() in BREADTH_BENCHMARKS else "000985.CSI"
        )
        weinstein_history = repository.weinstein_weekly_breadth_history(query_date, limit=52)
        minervini_history = repository.minervini_breadth_history(query_date, limit=260)
        benchmark_series = _normalized_index_series(
            market_reader.safe_index_bars(benchmark_symbol, query_date)[-260:]
        )
        canonical_price_series = _normalized_index_series(
            market_reader.safe_index_bars("000985.CSI", query_date)[-260:]
        )
        method_summaries = {
            "weinstein": _breadth_method_summary(
                weinstein_history,
                method="weinstein",
                period=4,
                period_label="4周",
            ),
            "minervini": _breadth_method_summary(
                minervini_history,
                method="minervini",
                period=20,
                period_label="20日",
            ),
        }
        canonical_price_summary = _market_price_summary(canonical_price_series)
        risk_context = _market_risk_context(method_summaries, canonical_price_summary)
        benchmark_summary = _proxy_summary(benchmark_series)
        benchmark_summary.update(
            {
                "symbol": benchmark_symbol,
                "name": BREADTH_BENCHMARKS[benchmark_symbol],
            }
        )
        style_symbols = {str(item["left_symbol"]) for item in STYLE_RELATIVE_PAIRS} | {
            str(item["right_symbol"]) for item in STYLE_RELATIVE_PAIRS
        }
        style_bars = {
            symbol: market_reader.safe_index_bars(symbol, query_date) for symbol in style_symbols
        }
        style_relative = []
        for pair in STYLE_RELATIVE_PAIRS:
            series = _relative_strength_series(
                style_bars[str(pair["left_symbol"])],
                style_bars[str(pair["right_symbol"])],
                limit=260,
            )
            style_relative.append(
                {
                    **pair,
                    "series": series,
                    "summary": _relative_strength_summary(series),
                }
            )
        rows = [_decorate_index(row) for row in repository.index_facts(query_date)]
        market_overview = _market_overview_summary(risk_context, rows)
        return render(
            request,
            "ashare/market_breadth.html",
            {
                "active": "市场全景",
                "query_date": query_date,
                "latest_date": repository.latest_fact_date(),
                "weinstein_history": weinstein_history,
                "minervini_history": minervini_history,
                "breadth_history_available": bool(weinstein_history or minervini_history),
                "proxy": benchmark_series,
                "method_summaries": method_summaries,
                "risk_context": risk_context,
                "canonical_price_summary": canonical_price_summary,
                "proxy_summary": benchmark_summary,
                "benchmarks": BREADTH_BENCHMARKS,
                "style_relative": style_relative,
                "style_relative_available": any(item["series"] for item in style_relative),
                "indices": rows,
                "market_overview": market_overview,
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

    @router.get("/a/breadth", response_class=HTMLResponse)
    def market_breadth_legacy(
        date: str | None = None,
        benchmark: str = "000985.CSI",
    ) -> RedirectResponse:
        query = {"benchmark": benchmark}
        if date:
            query["date"] = date
        return RedirectResponse(f"/a/indices?{urlencode(query)}", status_code=303)

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
        nav_previous: str = "",
        nav_next: str = "",
        nav_position: str = "",
        nav_total: str = "",
        brooks_state: str = "h1",
        min_amount: float = Query(default=0, ge=0, le=10000),
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
                "c1_record": {},
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
            if str(row.get("state") or "") in {"ENTERED", "REENTERED", "EXITED", "DATA_GAP"}
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
        setup_options = (
            users.trade_setup_options(
                user.user_id,
                plan_version_id=(
                    str(editing_trade.get("plan_version_id"))
                    if editing_trade and editing_trade.get("plan_version_id")
                    else None
                ),
            )
            if user is not None
            else [{"value": OTHER_SETUP_ID, "label": OTHER_SETUP_LABEL}]
        )
        if editing_trade and editing_trade.get("setup_kind") == "LEGACY":
            setup_options.append(
                {"value": "LEGACY", "label": str(editing_trade.get("setup_label") or "历史分类")}
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
            rows_for_date=lambda value: stock_navigation_rows_for_user(
                request,
                value,
                view=view,
                method=method,
                state=state,
            ),
            personal_rows=(
                personal_navigation_rows(request)
                if section.partition("#")[0] == "personal-observations"
                else None
            ),
            position_rows=(
                open_position_navigation_rows(request)
                if section.partition("#")[0] == "open-positions"
                else None
            ),
            nav_previous=nav_previous,
            nav_next=nav_next,
            nav_position=nav_position,
            nav_total=nav_total,
            brooks_state=brooks_state,
            min_amount=min_amount,
            brooks_rows=brooks_navigation_rows(
                query_date,
                state=brooks_state,
                industry=industry,
                min_amount=min_amount,
                q=q,
            )
            if section.partition("#")[0] == "brooks-results"
            else None,
        )
        payload["market_metrics"] = market_reader.safe_stock_market_metrics(
            query_date,
            [symbol],
        ).get(symbol.upper(), {})
        payload["brooks"] = repository.brooks_stock_fact(
            symbol, query_date, BROOKS_POLICY_VERSION
        )
        if payload["brooks"]:
            brooks = payload["brooks"]
            brooks["state_label"] = BROOKS_STATE_LABELS.get(str(brooks.get("state") or ""), "未知")
            current_close = float(brooks.get("evidence", {}).get("current_close") or 0)
            brooks["current_close"] = current_close
            brooks["retracement_pct"] = float(brooks.get("current_retracement") or 0) * 100
        payload["anomaly_evidence"] = _stock_anomaly_evidence(
            repository, market_reader, payload, query_date, symbol
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
                "trade_setup_options": setup_options,
                "exit_reason_options": EXIT_REASON_OPTIONS,
                "default_plan_setup": OTHER_SETUP_ID,
                "editing_trade": editing_trade,
                "trade_prefill": trade_prefill,
                "c1_structure_state_labels": C1_STRUCTURE_STATE_LABELS,
                "c1_entry_variant_labels": C1_ENTRY_VARIANT_LABELS,
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
        brooks_state: str = "h1",
        min_amount: float = Query(default=0, ge=0, le=10000),
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
                "c1_record": {},
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
            rows_for_date=lambda value: stock_navigation_rows_for_user(
                request,
                value,
                view=view,
                method=method,
                state=state,
            ),
            personal_rows=(
                personal_navigation_rows(request)
                if section.partition("#")[0] == "personal-observations"
                else None
            ),
            position_rows=(
                open_position_navigation_rows(request)
                if section.partition("#")[0] == "open-positions"
                else None
            ),
            nav_previous=nav_previous,
            nav_next=nav_next,
            nav_position=nav_position,
            nav_total=nav_total,
            brooks_state=brooks_state,
            min_amount=min_amount,
            brooks_rows=brooks_navigation_rows(
                query_date,
                state=brooks_state,
                industry=industry,
                min_amount=min_amount,
                q=q,
            )
            if section.partition("#")[0] == "brooks-results"
            else None,
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
                "keltner_presets": KELTNER_PRESETS,
                "daily_keltner": default_keltner_preset("day")[1],
            },
        )

    @router.get("/api/a/stocks/{symbol}/anomaly")
    def api_stock_anomaly(symbol: str, date: str) -> dict[str, Any]:
        return _stock_anomaly_evidence(
            repository,
            market_reader,
            {"industry": repository.stock_industry(symbol, date)},
            date,
            symbol,
        )

    @router.get("/a/stocks/{symbol}/realtime", response_class=HTMLResponse)
    def stock_realtime(
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
        brooks_state: str = "h1",
        min_amount: float = Query(default=0, ge=0, le=10000),
    ) -> HTMLResponse:
        payload = repository.stock_detail(symbol)
        if not payload["latest"]:
            raise HTTPException(status_code=404, detail="stock has no watchlist facts")
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
            rows_for_date=lambda value: stock_navigation_rows_for_user(
                request,
                value,
                view=view,
                method=method,
                state=state,
            ),
            personal_rows=(
                personal_navigation_rows(request)
                if section.partition("#")[0] == "personal-observations"
                else None
            ),
            position_rows=(
                open_position_navigation_rows(request)
                if section.partition("#")[0] == "open-positions"
                else None
            ),
            brooks_state=brooks_state,
            min_amount=min_amount,
            brooks_rows=brooks_navigation_rows(
                query_date,
                state=brooks_state,
                industry=industry,
                min_amount=min_amount,
                q=q,
            )
            if section.partition("#")[0] == "brooks-results"
            else None,
        )
        external_urls = _stock_external_urls(symbol)
        if not external_urls["tonghuashun_url"]:
            raise HTTPException(status_code=404, detail="realtime page is unavailable")
        return render(
            request,
            "ashare/watchlist_stock_realtime.html",
            {
                "query_date": query_date,
                "latest_date": repository.latest_fact_date(),
                "symbol": symbol.upper(),
                "stock_name": str(payload["identity"].get("name") or symbol.upper()),
                "industry_name": str(payload["industry"].get("industry_name") or ""),
                "tonghuashun_url": external_urls["tonghuashun_url"],
                "navigation": navigation,
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

    @router.get("/api/a/stocks/{symbol}/chart")
    def api_stock_chart(
        symbol: str,
        date: str,
        length: int | None = Query(default=None, ge=1, le=250),
        multiplier: float | None = Query(default=None, gt=0, le=20),
        source: str | None = Query(default=None, pattern="^(close|open|high|low)$"),
        use_ema: bool | None = None,
        band_style: str | None = Query(default=None, pattern="^(atr|tr|range)$"),
        atr_length: int | None = Query(default=None, ge=1, le=250),
        limit: int | None = Query(default=None, ge=30),
        interval: str = Query(default="day", pattern="^(day|week)$"),
    ) -> dict[str, Any]:
        _, defaults = default_keltner_preset(interval)
        length = length if length is not None else int(defaults["length"])
        multiplier = multiplier if multiplier is not None else float(defaults["multiplier"])
        source = source if source is not None else str(defaults["source"])
        use_ema = use_ema if use_ema is not None else bool(defaults["use_ema"])
        band_style = band_style if band_style is not None else str(defaults["band_style"])
        atr_length = atr_length if atr_length is not None else int(defaults["atr_length"])
        actual_keltner_config = keltner_config_payload(
            interval=interval,
            length=length,
            atr_length=atr_length,
            multiplier=multiplier,
            source=source,
            use_ema=use_ema,
            band_style=band_style,
        )
        warmup_periods = max(200, length, atr_length)
        analysis_limit = (
            None if limit is None else limit + warmup_periods * (6 if interval == "week" else 1)
        )
        analysis_daily_bars = market_reader.safe_stock_chart_bars(
            symbol, date, limit=analysis_limit
        )
        visible_daily_bars = analysis_daily_bars if limit is None else analysis_daily_bars[-limit:]
        bars = _weekly_bars(visible_daily_bars) if interval == "week" else visible_daily_bars
        analysis_bars = (
            _weekly_bars(analysis_daily_bars) if interval == "week" else analysis_daily_bars
        )
        if len(bars) < 2:
            return {
                "status": "DATA_GAP",
                "reason": "INSUFFICIENT_DAILY_OHLC",
                "bars": [],
                "keltner_config": actual_keltner_config,
            }
        bars = _with_period_changes(bars)
        scale_ids = {str(row.get("price_scale_id") or "") for row in bars}
        if len(scale_ids) != 1 or not next(iter(scale_ids)):
            return {
                "status": "DATA_GAP",
                "reason": "INCONSISTENT_PRICE_SCALE",
                "bars": [],
                "keltner_config": actual_keltner_config,
            }
        price_scale_id = next(iter(scale_ids))
        visible_dates = {str(row["trade_date"]) for row in bars}
        moving_average_lengths = (10, 30) if interval == "week" else (50, 150, 200)
        moving_averages = {
            str(period): [point for point in points if str(point["trade_date"]) in visible_dates]
            for period, points in _chart_moving_averages(
                analysis_bars, moving_average_lengths
            ).items()
        }
        method_overlay = _chart_method_overlay(
            repository,
            symbol=symbol,
            bars=bars,
            end_date=date,
            interval=interval,
        )
        return {
            "status": "OK",
            "symbol": symbol.upper(),
            "as_of_date": date,
            "adjustment": "qfq",
            "interval": interval,
            "keltner_config": actual_keltner_config,
            "price_scale_id": price_scale_id,
            "bars": bars,
            "method_events": method_overlay["events"],
            "method_event_coverage": method_overlay["coverage"],
            "moving_averages": moving_averages,
            "keltner": [
                point
                for point in keltner_channels(
                    analysis_bars,
                    length=length,
                    multiplier=multiplier,
                    source=source,
                    use_ema=use_ema,
                    band_style=band_style,
                    atr_length=atr_length,
                )
                if str(point["date"]) in visible_dates
            ],
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
                "brooks_runs": repository.brooks_run_receipts(30),
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
                _decorate_industry(row) for row in repository.industry_observations(query_date)
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


def _decorate_trade_r_multiples(
    closed_trades: list[dict[str, Any]], market_reader: MarketDataReader
) -> None:
    """Apply the trade-review R-multiple definitions to closed FIFO matches."""
    for item in closed_trades:
        item["planned_r_multiple"] = item.get("r_multiple")
        item["actual_r_multiple"] = None
        item["actual_drawdown_low"] = None
        entry_price = float(item["entry_price"])
        exit_price = float(item["exit_price"])
        stop_price = item.get("initial_stop_price", item.get("stop_price"))
        if (
            item["planned_r_multiple"] is None
            and stop_price is not None
            and entry_price > float(stop_price)
        ):
            item["planned_r_multiple"] = round(
                (exit_price - entry_price) / (entry_price - float(stop_price)), 4
            )
        if float(item["pnl"]) <= 0:
            continue
        drawdown_low = market_reader.safe_trade_drawdown_low(
            str(item["symbol"]), str(item["buy_date"]), str(item["sell_date"])
        )
        if drawdown_low is not None and drawdown_low < entry_price:
            item["actual_drawdown_low"] = drawdown_low
            item["actual_r_multiple"] = round(
                (exit_price - entry_price) / (entry_price - drawdown_low), 2
            )


def _selected_date(
    repository: WatchlistRepository,
    requested: str | None,
    market_reader: MarketDataReader,
) -> str:
    if requested and repository.has_fact_date(requested):
        return requested
    return repository.latest_fact_date() or requested or _safe_market_date(market_reader)


def _web_exit_reason(choice: str, other: str, *, legacy: str = "") -> str:
    """Normalize the optional, controlled exit-reason fields from the web form."""
    normalized_choice = choice.strip()
    if not normalized_choice:
        return legacy.strip()
    if normalized_choice == "OTHER":
        return other.strip()
    if normalized_choice not in EXIT_REASON_OPTIONS:
        raise ValueError("卖出原因不受支持")
    return normalized_choice


def _normalize_daily_query(*, view: str | None, method: str, state: str) -> tuple[str, str, str]:
    method_mode = method.lower().strip()
    if method_mode not in {"all", "both", *METHOD_LABELS}:
        method_mode = "all"

    state_mode = state.upper().strip()
    requested_view = str(view or "").lower().strip()
    if requested_view not in {"current", "changes", "projection"}:
        if state_mode.startswith("PRE_"):
            requested_view = "projection"
        else:
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
    if requested_view == "projection":
        if state_mode not in {
            "CHANGE",
            "ENTER",
            "PRE_ENTERED",
            "PRE_REENTERED",
            "PRE_EXITED",
            "PASSING",
            "PRE_CONTINUING",
            "DATA_GAP",
        }:
            state_mode = "CHANGE"
        return "projection", "weinstein", state_mode
    if requested_view == "current":
        return "current", method_mode, ""

    if method_mode not in METHOD_LABELS:
        method_mode = "weinstein"

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
    if view in {"changes", "projection"} and state:
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
        method_row["result_label"] = RESULT_LABELS.get(str(method_row.get("result") or ""), "未知")
        method_row["state_label"] = STATE_LABELS.get(
            str(method_row.get("state") or ""), "未发生变化"
        )
        profile = dict(method_row.get("evidence", {}).get("profile") or {})
        method_row["stage_label"] = STAGE_LABELS.get(str(profile.get("stage") or ""), "")
    item["both_pass"] = all(
        methods.get(method, {}).get("result") == "PASS" for method in ("minervini", "weinstein")
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


def _format_optional_decimal(value: Any) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "—"


def _decorate_provisional_row(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    metrics = dict(item.get("metrics") or {})
    checks = dict(metrics.get("checks") or {})
    failed = [str(value) for value in metrics.get("failed_checks") or []]
    item["state_label"] = PROVISIONAL_STATE_LABELS.get(str(item.get("state") or ""), "周内状态未知")
    item["projected_stage_label"] = STAGE_LABELS.get(
        str(item.get("projected_stage") or "UNKNOWN"), "数据不足"
    )
    item["formal_stage_label"] = STAGE_LABELS.get(
        str(item.get("formal_stage") or "UNKNOWN"), "数据不足"
    )
    item["prior_formal_stage_label"] = STAGE_LABELS.get(
        str(item.get("prior_formal_stage") or "UNKNOWN"), "数据不足"
    )
    item["minervini_result_label"] = RESULT_LABELS.get(
        str(item.get("minervini_result") or "UNKNOWN"), "未知"
    )
    item["check_rows"] = [
        {
            "key": key,
            "label": WEINSTEIN_CHECK_LABELS.get(key, key),
            "passed": bool(value),
        }
        for key, value in checks.items()
    ]
    item["metric_display"] = {
        key: _format_optional_decimal(metrics.get(key))
        for key in (
            "distance_from_ma30_pct",
            "ma30_slope_4w_pct",
            "return_13w_pct",
        )
    }
    item["failed_check_labels"] = [WEINSTEIN_CHECK_LABELS.get(key, key) for key in failed]
    item["passed_check_count"] = sum(bool(value) for value in checks.values())
    item["check_count"] = len(checks)
    previous_result = str(item.get("previous_result") or "UNKNOWN")
    projected_result = str(item.get("projected_result") or "UNKNOWN")
    item["previous_projection_label"] = PROJECTION_RESULT_LABELS.get(previous_result, "状态未知")
    item["current_projection_label"] = PROJECTION_RESULT_LABELS.get(projected_result, "状态未知")
    item["previous_projection_source"] = (
        f"{item.get('previous_as_of_date')} 投影"
        if item.get("previous_as_of_date")
        else "上周正式状态"
    )
    elapsed = int(item.get("sessions_elapsed") or 0)
    total = int(item.get("sessions_total") or 0)
    item["maturity_label"] = f"本周第 {elapsed}/{total} 个交易日" if total else "交易日历不足"
    item["is_change"] = str(item.get("state") or "") in {
        "PRE_ENTERED",
        "PRE_REENTERED",
        "PRE_EXITED",
        "DATA_GAP",
    }
    item.update(_stock_external_urls(str(item.get("symbol") or "")))
    return item


def _chart_method_statuses(history: list[dict[str, Any]], query_date: str) -> list[dict[str, str]]:
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
        profile = dict((row.get("evidence") or {}).get("profile") or {})
        stage = str(profile.get("stage") or "") if method == "weinstein" else ""
        result.append(
            {
                "method": method,
                "label": METHOD_LABELS[method],
                "result": method_result,
                "result_label": RESULT_LABELS.get(method_result, "未知"),
                "as_of_date": str(row.get("as_of_date") or ""),
                "stage_label": STAGE_LABELS.get(stage, "") if stage else "",
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


def _stock_anomaly_evidence(
    repository: WatchlistRepository,
    market_reader: MarketDataReader,
    payload: dict[str, Any],
    as_of_date: str,
    symbol: str,
) -> dict[str, Any]:
    industry_code = str(payload.get("industry", {}).get("industry_code") or "")
    peer_symbols = repository.industry_member_symbols(as_of_date, industry_code)
    return market_reader.safe_stock_anomaly_evidence(as_of_date, symbol, peer_symbols)


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
            "up"
            if change_pct is not None and float(change_pct) > 0
            else "down"
            if change_pct is not None and float(change_pct) < 0
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
    item["price_coverage_pct_label"] = f"{float(coverage):.1f}%" if coverage is not None else "暂无"
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
    tradingview_symbol = (
        f"{exchange}:{code}" if exchange and len(code) == 6 and code.isdigit() else ""
    )
    tradingview_url = (
        f"https://cn.tradingview.com/chart/?symbol={quote(tradingview_symbol, safe='')}"
        if tradingview_symbol
        else ""
    )
    tonghuashun_url = (
        f"https://stockpage.10jqka.com.cn/{code}/" if len(code) == 6 and code.isdigit() else ""
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
        urls["tonghuashun_url"] = f"https://q.10jqka.com.cn/zs/detail/code/1B{code[-4:]}/"
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
        weekly.append(
            {
                **last,
                "open": first["open"],
                "high": max(float(item["high"]) for item in values),
                "low": min(float(item["low"]) for item in values),
                "close": last["close"],
                "volume": sum(float(item.get("volume") or 0) for item in values),
                "amount": sum(float(item.get("amount") or 0) for item in values),
            }
        )
    return weekly


def _chart_method_overlay(
    repository: WatchlistRepository,
    *,
    symbol: str,
    bars: list[dict[str, Any]],
    end_date: str,
    interval: str,
) -> dict[str, Any]:
    if not bars:
        return {"events": [], "coverage": {}}
    first_bar_date = str(bars[0]["trade_date"])
    last_bar_date = str(bars[-1]["trade_date"])
    retained = repository.stock_method_chart_events(symbol, first_bar_date, end_date)
    dates = {str(row["trade_date"]) for row in bars}
    week_ends = {_week_key(str(row["trade_date"])): str(row["trade_date"]) for row in bars}
    events: list[dict[str, Any]] = []
    for item in retained["events"]:
        as_of_date = str(item["as_of_date"])
        plot_date = week_ends.get(_week_key(as_of_date), "") if interval == "week" else as_of_date
        if plot_date not in dates:
            continue
        events.append({**item, "plot_date": plot_date})
    coverage: dict[str, dict[str, Any]] = {}
    for method, values in retained["coverage"].items():
        from_date = str(values.get("from_date") or "")
        to_date = str(values.get("to_date") or "")
        coverage[method] = {
            **values,
            "complete_for_chart_range": bool(
                from_date and to_date and from_date <= first_bar_date and to_date >= last_bar_date
            ),
        }
    return {"events": events, "coverage": coverage}


def _with_period_changes(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach close-to-close changes on the chart's existing adjusted-price scale."""

    decorated: list[dict[str, Any]] = []
    previous_close: float | None = None
    for row in bars:
        item = dict(row)
        close = float(item["close"])
        item["previous_close"] = previous_close
        item["change_amount"] = (
            round(close - previous_close, 6) if previous_close is not None else None
        )
        item["change_pct"] = (
            round((close / previous_close - 1) * 100, 2)
            if previous_close is not None and previous_close != 0.0
            else None
        )
        decorated.append(item)
        previous_close = close
    return decorated


def _industry_chart_summary(bars: list[dict[str, Any]]) -> dict[str, Any]:
    if not bars:
        return {}
    latest = dict(bars[-1])
    first_close = float(bars[0]["close"])
    latest_close = float(latest["close"])
    latest["period_return_pct"] = round((latest_close / first_close - 1) * 100, 2)
    return {
        "latest": latest,
        "first_date": str(bars[0]["trade_date"]),
        "last_date": str(bars[-1]["trade_date"]),
    }


def _industry_navigation(rows: list[dict[str, Any]], *, industry_code: str) -> dict[str, Any]:
    ordered_rows = sorted(
        rows,
        key=lambda row: (
            -int(row.get("union_pass_count") or 0),
            -int(row.get("both_pass_count") or 0),
            -int(row.get("weinstein_pass_count") or 0),
            -int(row.get("minervini_pass_count") or 0),
            str(row.get("industry_code") or ""),
        ),
    )
    items = [
        {
            "industry_code": str(row.get("industry_code") or ""),
            "industry_name": str(row.get("industry_name") or row.get("industry_code") or ""),
        }
        for row in ordered_rows
        if row.get("industry_code")
    ]
    current = industry_code.upper().strip()
    position = next(
        (index for index, item in enumerate(items) if item["industry_code"].upper() == current),
        -1,
    )
    if position < 0:
        return {"position": 0, "total": len(items), "previous": None, "next": None}
    return {
        "position": position + 1,
        "total": len(items),
        "previous": items[position - 1] if position > 0 else None,
        "next": items[position + 1] if position + 1 < len(items) else None,
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
        if (
            query
            and query
            not in " ".join(
                [
                    str(row.get("symbol") or ""),
                    str(row.get("name") or ""),
                    str(row.get("industry") or ""),
                ]
            ).lower()
        ):
            continue
        result.append(row)
    return result


def _current_rows(rows: list[dict[str, Any]], method: str) -> list[dict[str, Any]]:
    if method == "both":
        return [row for row in rows if row["both_pass"]]
    if method in METHOD_LABELS:
        return [
            row for row in rows if row.get("methods", {}).get(method, {}).get("result") == "PASS"
        ]
    return [row for row in rows if row["has_pass"]]


def _change_rows(rows: list[dict[str, Any]], method: str, state: str) -> list[dict[str, Any]]:
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


def _projection_rows(rows: list[dict[str, Any]], state: str) -> list[dict[str, Any]]:
    if state == "CHANGE":
        states = {"PRE_ENTERED", "PRE_REENTERED", "PRE_EXITED", "DATA_GAP"}
        selected = [row for row in rows if str(row.get("state") or "") in states]
    elif state == "ENTER":
        selected = [
            row for row in rows if str(row.get("state") or "") in {"PRE_ENTERED", "PRE_REENTERED"}
        ]
    elif state == "PASSING":
        selected = [row for row in rows if str(row.get("projected_result") or "") == "PASS"]
    else:
        selected = [row for row in rows if str(row.get("state") or "") == state]
    rank = {
        "PRE_ENTERED": 0,
        "PRE_REENTERED": 1,
        "PRE_EXITED": 2,
        "DATA_GAP": 3,
        "PRE_CONTINUING": 4,
    }
    return sorted(
        selected,
        key=lambda row: (
            rank.get(str(row.get("state") or ""), 9),
            -int(row.get("sessions_elapsed") or 0),
            str(row.get("symbol") or ""),
        ),
    )


def _sort_daily_rows(rows: list[dict[str, Any]], *, view: str, method: str) -> list[dict[str, Any]]:
    del view, method

    def change_key(row: dict[str, Any]) -> tuple[bool, float, str]:
        value = row.get("change_pct")
        if value is None:
            return True, 0.0, str(row.get("symbol") or "")
        try:
            change_pct = float(str(value))
        except ValueError:
            return True, 0.0, str(row.get("symbol") or "")
        return False, -change_pct, str(row.get("symbol") or "")

    return sorted(rows, key=change_key)


def _prepare_daily_rows(rows: list[dict[str, Any]], *, method: str) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        methods = dict(item.get("methods") or {})
        if method in METHOD_LABELS:
            method_items = [(method, methods.get(method, {}))]
        else:
            method_items = [
                (key, value) for key, value in methods.items() if value.get("result") == "PASS"
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


def _navigation_item(symbol: str, names: dict[str, str] | None = None) -> dict[str, str] | None:
    normalized = symbol.upper().strip()
    if not normalized:
        return None
    return {
        "symbol": normalized,
        "name": str((names or {}).get(normalized) or normalized),
    }


def _filter_brooks_rows(
    rows: list[dict[str, Any]],
    *,
    state: str,
    industry: str,
    min_amount: float,
    q: str,
) -> list[dict[str, Any]]:
    state_mode = state.lower().strip()
    if state_mode == "continuing":
        state_mode = "priority"
    if state_mode not in {
        "priority",
        "watch",
        "pullback",
        "immature",
        "deep",
        "expanding",
        "cycle_done",
        "h1",
        "new",
        "history",
        "terminal",
        "paused",
        "all",
    }:
        state_mode = "priority"
    industry_filter = industry.strip()
    needle = q.strip().casefold()
    filtered: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        phase = _brooks_phase(row)
        if state_mode == "history":
            visible = phase == "h1"
        elif state_mode == "pullback":
            visible = phase in {"immature", "watch", "deep"}
        elif state_mode == "priority":
            visible = phase == "watch" and _brooks_priority_quality(row)
        elif state_mode == "all":
            visible = True
        else:
            visible = phase == state_mode
        if not visible:
            continue
        if industry_filter and str(row.get("industry") or "") != industry_filter:
            continue
        if float(row.get("median_amount20") or 0) / 100000 < min_amount:
            continue
        haystack = " ".join(
            str(row.get(key) or "") for key in ("symbol", "name", "industry")
        ).casefold()
        if needle and needle not in haystack:
            continue
        evidence = dict(row.get("evidence") or {})
        current_close = float(evidence.get("current_close") or 0)
        row["current_close"] = current_close
        row["brooks_phase"] = phase
        row["priority_quality"] = _brooks_priority_quality(row)
        row["state_label"] = {
            "priority": "优先观察",
            "watch": "H1重点观察",
            "immature": "回调未成熟",
            "deep": "深回调/老化",
            "expanding": "强势扩展",
            "cycle_done": "本轮H1已过",
            "h1": "今日 H1",
            "new": "今日新强势",
        }.get(phase, BROOKS_STATE_LABELS.get(str(row.get("state") or ""), "未知"))
        row["retracement_pct"] = float(row.get("current_retracement") or 0) * 100
        row["maximum_retracement_pct"] = (
            float(row.get("maximum_low_retracement") or 0) * 100
        )
        row["tight_risk_pct"] = (
            (current_close - float(row["tight_invalidation"])) / current_close * 100
            if current_close > 0
            else None
        )
        row["structural_risk_pct"] = (
            (current_close - float(row["structural_invalidation"])) / current_close * 100
            if current_close > 0
            else None
        )
        row["median_amount20_yi"] = float(row.get("median_amount20") or 0) / 100000
        row.update(_stock_external_urls(str(row.get("symbol") or "")))
        filtered.append(row)
    if state_mode in {"priority", "watch"}:
        filtered.sort(
            key=lambda row: (
                not bool(row.get("priority_quality")),
                -float(row.get("median_amount20") or 0),
                -float(row.get("path_efficiency") or 0),
                -float(row.get("mean_close_location") or 0),
                float(row.get("mean_overlap") or 0),
                str(row.get("symbol") or ""),
            )
        )
    return filtered


def _brooks_priority_quality(row: Mapping[str, Any]) -> bool:
    """Transparent first-leg quality tag used only inside the H1 watch phase."""

    mean_overlap = row.get("mean_overlap")
    return (
        float(row.get("path_efficiency") or 0) >= 0.85
        and float(row.get("mean_close_location") or 0) >= 0.75
        and float(mean_overlap if mean_overlap is not None else 1.0) <= 0.30
    )


def _brooks_phase(row: Mapping[str, Any]) -> str:
    """Classify one Brooks fact into one mutually exclusive workload phase."""

    row_state = str(row.get("state") or "")
    row_date = str(row.get("as_of_date") or "")
    if bool(row.get("h1_date")) and str(row.get("h1_date")) == row_date:
        return "h1"
    if row_state in {"COMPLETED", "INVALIDATED", "STALE"}:
        return "terminal"
    if row_state == "PAUSED":
        return "paused"
    if str(row.get("signal_date") or "") == row_date:
        return "new"
    if row_state != "S0_IMPULSE":
        return "all"

    evidence = dict(row.get("evidence") or {})
    pullback_bars = int(evidence.get("pullback_bars") or 0)
    pullback_start = str(evidence.get("pullback_start_date") or "")
    last_h1_date = str(evidence.get("last_h1_date") or "")
    if pullback_bars <= 0:
        return "expanding"
    if pullback_start and last_h1_date and last_h1_date >= pullback_start:
        return "cycle_done"

    maximum_retracement_pct = float(row.get("maximum_low_retracement") or 0) * 100
    pullback_atr = float(row.get("pullback_atr") or 0)
    if maximum_retracement_pct >= 50 or pullback_atr >= 3 or pullback_bars > 10:
        return "deep"
    if pullback_bars == 1 or maximum_retracement_pct < 15 or pullback_atr < 0.5:
        return "immature"
    return "watch"


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
    personal_rows: list[dict[str, Any]] | None = None,
    position_rows: list[dict[str, Any]] | None = None,
    brooks_rows: list[dict[str, Any]] | None = None,
    brooks_state: str = "h1",
    min_amount: float = 0,
    nav_previous: str = "",
    nav_next: str = "",
    nav_position: str = "",
    nav_total: str = "",
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
    if section == "brooks-results":
        query = urlencode(
            {
                "date": query_date,
                "brooks_state": brooks_state,
                "min_amount": min_amount,
                "industry": industry,
                "q": q,
                "section": section,
            }
        )
    else:
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
    if section == "personal-observations":
        list_url = "/a/observations?" + urlencode({"state": manual, "date": query_date})
    elif section == "open-positions":
        list_url = "/a/review#current-positions"
    elif section == "brooks-results":
        list_url = "/a/brooks-second-leg?" + urlencode(
            {
                "date": query_date,
                "state": brooks_state,
                "industry": industry,
                "min_amount": min_amount,
                "q": q,
            }
        )
    labels = {
        "new-candidates": "新进 / 重进",
        "continuing-candidates": "持续符合",
        "exit-candidates": "退出 / 中断",
        "daily-results": (
            "当前观察池"
            if view_mode == "current"
            else "每日变化"
            if view_mode == "projection"
            else "正式状态变化"
        ),
        "focus-candidates": "我的重点观察",
        "personal-observations": "我的观察",
        "open-positions": "当前持仓",
        "brooks-results": "强势股与 H1 观察池",
    }
    empty: dict[str, Any] = {
        "query": query,
        "entry_query": query,
        "list_url": list_url,
        "section_label": labels.get(section, "当前列表"),
        "position": 0,
        "total": 0,
        "previous": None,
        "next": None,
    }
    if not query_date:
        return empty
    submitted_position = _navigation_count(nav_position)
    submitted_total = _navigation_count(nav_total)
    submitted_is_complete = bool(
        0 < submitted_position <= submitted_total
        and (submitted_position == 1 or nav_previous.strip())
        and (submitted_position == submitted_total or nav_next.strip())
    )
    if submitted_is_complete:
        submitted_symbols = {
            value.upper().strip() for value in (nav_previous, nav_next) if value.strip()
        }
        submitted_names = repository.stock_names(submitted_symbols)
        nav_query = urlencode(
            {
                "nav_previous": nav_previous.upper().strip(),
                "nav_next": nav_next.upper().strip(),
                "nav_position": submitted_position,
                "nav_total": submitted_total,
            }
        )
        return {
            **empty,
            "entry_query": f"{query}&{nav_query}",
            "section_label": labels.get(section, "当前列表"),
            "position": submitted_position,
            "total": submitted_total,
            "previous": _navigation_item(nav_previous, submitted_names),
            "next": _navigation_item(nav_next, submitted_names),
        }
    if section not in labels:
        return empty
    if section == "personal-observations" and personal_rows is not None:
        selected = [
            row
            for row in personal_rows
            if manual == "all" or str(row.get("manual", {}).get("manual_state") or "") == manual
        ]
        symbols = [str(row.get("symbol") or "").upper() for row in selected]
        try:
            index = symbols.index(symbol.upper())
        except ValueError:
            return empty

        def personal_item_at(position: int) -> dict[str, str] | None:
            if position < 0 or position >= len(selected):
                return None
            row = selected[position]
            return {
                "symbol": str(row["symbol"]),
                "name": str(row.get("name") or row["symbol"]),
            }

        return {
            "query": query,
            "list_url": list_url,
            "section_label": labels[section],
            "position": index + 1,
            "total": len(selected),
            "previous": personal_item_at(index - 1),
            "next": personal_item_at(index + 1),
        }

    if section == "open-positions" and position_rows is not None:
        symbols = [str(row.get("symbol") or "").upper() for row in position_rows]
        try:
            index = symbols.index(symbol.upper())
        except ValueError:
            return empty

        def position_item_at(position: int) -> dict[str, str] | None:
            if position < 0 or position >= len(position_rows):
                return None
            row = position_rows[position]
            return {
                "symbol": str(row["symbol"]),
                "name": str(row.get("name") or row["symbol"]),
            }

        return {
            "query": query,
            "list_url": list_url,
            "section_label": labels[section],
            "position": index + 1,
            "total": len(position_rows),
            "previous": position_item_at(index - 1),
            "next": position_item_at(index + 1),
        }

    if section == "brooks-results" and brooks_rows is not None:
        symbols = [str(row.get("symbol") or "").upper() for row in brooks_rows]
        try:
            index = symbols.index(symbol.upper())
        except ValueError:
            return empty

        def brooks_item_at(position: int) -> dict[str, str] | None:
            if position < 0 or position >= len(brooks_rows):
                return None
            row = brooks_rows[position]
            return {
                "symbol": str(row["symbol"]),
                "name": str(row.get("name") or row["symbol"]),
            }

        return {
            "query": query,
            "entry_query": query,
            "list_url": list_url,
            "section_label": labels[section],
            "position": index + 1,
            "total": len(brooks_rows),
            "previous": brooks_item_at(index - 1),
            "next": brooks_item_at(index + 1),
        }

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
        if view_mode == "projection":
            selected = _projection_rows(base_rows, state_mode)
        else:
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
                if row["has_pass"] and not row["has_new_state"] and not row["has_exit_state"]
            ]
        elif section == "exit-candidates":
            selected = [row for row in filtered if row["has_exit_state"]]
        elif section == "focus-candidates":
            selected = [
                row for row in filtered if str(row["manual"].get("manual_state") or "") == "FOCUS"
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


def _market_overview_summary(
    risk_context: dict[str, Any],
    indices: list[dict[str, Any]],
) -> dict[str, str]:
    """Describe independent index and breadth evidence without making a score."""
    if not indices:
        headline = "市场内部与主要指数趋势独立呈现"
    else:
        weinstein_stage_2 = sum(1 for row in indices if row.get("stage") == "STAGE_2")
        minervini_stage_2 = sum(
            1 for row in indices if row.get("minervini", {}).get("result") == "PASS"
        )
        all_strong = weinstein_stage_2 == len(indices) and minervini_stage_2 == len(indices)
        none_strong = weinstein_stage_2 == 0 and minervini_stage_2 == 0
        tone = str(risk_context.get("tone") or "mixed")
        if tone == "positive" and none_strong:
            headline = "市场内部正在改善，主要指数趋势尚未转强"
        elif tone == "positive" and all_strong:
            headline = "市场参与面与主要指数趋势同步改善"
        elif tone == "negative" and none_strong:
            headline = "市场参与面偏弱，主要指数趋势尚未转强"
        elif tone == "negative":
            headline = "主要指数仍有上升阶段，市场参与面正在收缩"
        else:
            headline = "市场参与面与主要指数趋势尚未同步"
    return {
        "headline": headline,
        "detail": (
            "四指数逐项展示 Weinstein 阶段与 Minervini 第二阶段结果；"
            "不投票、不加权、不合成市场总分。"
        ),
    }


def _breadth_method_summary(
    history: list[dict[str, Any]],
    *,
    method: str,
    period: int,
    period_label: str,
) -> dict[str, Any]:
    points = [row for row in history if row.get("pass_rate") is not None]
    if not points:
        return {
            "method": method,
            "as_of_date": "",
            "pass_count": 0,
            "evaluable_count": 0,
            "pass_rate": None,
            "delta": None,
            "period_label": period_label,
            "direction": "UNKNOWN",
            "direction_label": "数据不足",
            "policy_version": "",
            "origin": "",
        }
    latest = points[-1]
    baseline = points[-(period + 1)] if len(points) >= period + 1 else None
    latest_rate = float(latest["pass_rate"])
    baseline_rate = float(baseline["pass_rate"]) if baseline is not None else None
    delta = round(latest_rate - baseline_rate, 2) if baseline_rate is not None else None
    if delta is None:
        direction = "UNKNOWN"
        direction_label = "数据不足"
    elif delta >= 1.0:
        direction = "EXPANDING"
        direction_label = f"{period_label}扩张"
    elif delta <= -1.0:
        direction = "CONTRACTING"
        direction_label = f"{period_label}收缩"
    else:
        direction = "FLAT"
        direction_label = f"{period_label}持平"
    return {
        "method": method,
        "as_of_date": str(latest["as_of_date"]),
        "pass_count": int(latest["pass_count"] or 0),
        "evaluable_count": int(latest["evaluable_count"] or 0),
        "pass_rate": latest_rate,
        "delta": delta,
        "period_label": period_label,
        "direction": direction,
        "direction_label": direction_label,
        "policy_version": str(latest.get("policy_version") or ""),
        "origin": str(latest.get("origin") or ""),
    }


def _market_risk_context(
    summaries: dict[str, dict[str, Any]],
    price: dict[str, Any],
) -> dict[str, Any]:
    weinstein = str(summaries["weinstein"].get("direction") or "UNKNOWN")
    minervini = str(summaries["minervini"].get("direction") or "UNKNOWN")
    price_direction = str(price.get("direction") or "UNKNOWN")
    if "UNKNOWN" in {weinstein, minervini}:
        state = (
            "DATA_INSUFFICIENT",
            "数据不足",
            "风险偏好未知",
            "完整周或日线广度历史不足，暂不判断市场风险背景。",
            "neutral",
        )
    elif weinstein == "EXPANDING" and minervini == "EXPANDING":
        if price_direction == "UP":
            state = (
                "PARTICIPATION_IMPROVING_CONFIRMED",
                "参与面改善并获价格确认",
                "风险偏好改善倾向",
                "完整周 Stage 2 广度与近期强趋势质量同时扩张，中证全指20日上涨。",
                "positive",
            )
        elif price_direction == "DOWN":
            state = (
                "INTERNAL_REPAIR",
                "内部修复",
                "风险偏好修复待确认",
                "两种趋势广度正在扩张，但中证全指20日价格尚未确认。",
                "mixed",
            )
        else:
            state = (
                "PARTICIPATION_IMPROVING_UNCONFIRMED",
                "参与面改善，价格待确认",
                "风险偏好改善倾向",
                "两种广度同时扩张，但中证全指20日价格数据不足或持平。",
                "neutral",
            )
    elif weinstein == "CONTRACTING" and minervini == "CONTRACTING":
        if price_direction == "DOWN":
            state = (
                "PARTICIPATION_WEAKENING_CONFIRMED",
                "参与面转弱并获价格确认",
                "风险偏好减弱倾向",
                "完整周 Stage 2 广度与近期强趋势质量同时收缩，中证全指20日下跌。",
                "negative",
            )
        elif price_direction == "UP":
            state = (
                "PRICE_STRONG_BREADTH_CONTRACTING",
                "价格强、参与面收缩",
                "风险偏好分化",
                "中证全指上涨，但完整周与日线广度同时收缩，上涨可能较为集中。",
                "mixed",
            )
        else:
            state = (
                "PARTICIPATION_WEAKENING_UNCONFIRMED",
                "参与面转弱，价格待确认",
                "风险偏好减弱倾向",
                "两种广度同时收缩，但中证全指20日价格数据不足或持平。",
                "neutral",
            )
    elif weinstein == "EXPANDING" and minervini == "CONTRACTING":
        state = (
            "MEDIUM_EXPANDING_RECENT_COOLING",
            "中期扩张、近期降温",
            "风险偏好分化",
            "完整周 Stage 2 基础仍在扩展，但近期严格趋势模板通过率收缩。",
            "mixed",
        )
    elif weinstein == "CONTRACTING" and minervini == "EXPANDING":
        state = (
            "RECENT_REPAIR_MEDIUM_WEAK",
            "近期修复、中期仍弱",
            "风险偏好分化",
            "近期强趋势股票增加，但完整周 Stage 2 广度仍在收缩。",
            "mixed",
        )
    else:
        state = (
            "DIRECTION_PENDING",
            "方向待确认",
            "风险偏好未确认",
            "至少一种方法的约一个月变化不足1个百分点，暂不解读为明确状态。",
            "neutral",
        )
    code, label, tendency, explanation, tone = state
    reconstructed = any(
        str(summaries[key].get("origin") or "") == "RECONSTRUCTED"
        for key in ("weinstein", "minervini")
    )
    return {
        "state": code,
        "label": label,
        "risk_tendency": tendency,
        "explanation": explanation,
        "tone": tone,
        "weinstein_as_of": str(summaries["weinstein"].get("as_of_date") or ""),
        "minervini_as_of": str(summaries["minervini"].get("as_of_date") or ""),
        "price_as_of": str(price.get("as_of_date") or ""),
        "reconstructed": reconstructed,
    }


def _chart_moving_averages(
    bars: list[dict[str, Any]], lengths: tuple[int, ...]
) -> dict[int, list[dict[str, Any]]]:
    """Build warmed simple moving-average points for chart overlays."""

    closes = [float(row["close"]) for row in bars]
    result: dict[int, list[dict[str, Any]]] = {}
    for length in lengths:
        running = 0.0
        points: list[dict[str, Any]] = []
        for index, (row, close) in enumerate(zip(bars, closes, strict=True)):
            running += close
            if index >= length:
                running -= closes[index - length]
            if index >= length - 1:
                points.append(
                    {
                        "trade_date": str(row["trade_date"]),
                        "value": round(running / length, 6),
                    }
                )
        result[length] = points
    return result


def _normalized_index_series(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    valid = [row for row in bars if float(row.get("close") or 0) > 0]
    if not valid:
        return []
    baseline = float(valid[0]["close"])
    return [
        {
            "trade_date": str(row.get("trade_date") or ""),
            "close": round(float(row["close"]) / baseline * 1000, 2),
        }
        for row in valid
    ]


def _proxy_summary(proxy: list[dict[str, Any]]) -> dict[str, Any]:
    if not proxy:
        return {
            "as_of_date": "",
            "close": None,
            "return_20d_pct": None,
        }
    latest = proxy[-1]
    baseline = proxy[-21] if len(proxy) >= 21 else proxy[0]
    baseline_close = float(baseline.get("close") or 0)
    latest_close = float(latest.get("close") or 0)
    return {
        "as_of_date": str(latest.get("trade_date") or ""),
        "close": latest_close,
        "return_20d_pct": (
            round((latest_close / baseline_close - 1) * 100, 2)
            if baseline_close > 0 and len(proxy) > 1
            else None
        ),
    }


def _market_price_summary(series: list[dict[str, Any]]) -> dict[str, Any]:
    if not series:
        return {
            "as_of_date": "",
            "return_20d_pct": None,
            "direction": "UNKNOWN",
            "direction_label": "数据不足",
        }
    latest = series[-1]
    if len(series) < 21:
        return {
            "as_of_date": str(latest.get("trade_date") or ""),
            "return_20d_pct": None,
            "direction": "UNKNOWN",
            "direction_label": "数据不足",
        }
    baseline = float(series[-21].get("close") or 0)
    latest_close = float(latest.get("close") or 0)
    if baseline <= 0:
        change = None
    else:
        change = round((latest_close / baseline - 1) * 100, 2)
    if change is None:
        direction, label = "UNKNOWN", "数据不足"
    elif change > 0:
        direction, label = "UP", "20日上涨"
    elif change < 0:
        direction, label = "DOWN", "20日下跌"
    else:
        direction, label = "FLAT", "20日持平"
    return {
        "as_of_date": str(latest.get("trade_date") or ""),
        "return_20d_pct": change,
        "direction": direction,
        "direction_label": label,
    }


def _relative_strength_series(
    left_bars: list[dict[str, Any]],
    right_bars: list[dict[str, Any]],
    *,
    limit: int = 260,
) -> list[dict[str, Any]]:
    """Return an aligned index ratio path normalized to 1000 at the window start."""

    def valid_closes(bars: list[dict[str, Any]]) -> dict[str, float]:
        values: dict[str, float] = {}
        for row in bars:
            trade_date = str(row.get("trade_date") or "")
            try:
                close = float(row.get("close") or 0)
            except (TypeError, ValueError):
                continue
            if trade_date and close > 0:
                values[trade_date] = close
        return values

    left = valid_closes(left_bars)
    right = valid_closes(right_bars)
    dates = sorted(set(left) & set(right))
    if limit > 0:
        dates = dates[-limit:]
    if not dates:
        return []
    ratios = [(trade_date, left[trade_date] / right[trade_date]) for trade_date in dates]
    baseline = ratios[0][1]
    if baseline <= 0:
        return []
    return [
        {
            "trade_date": trade_date,
            "value": round(ratio / baseline * 1000, 4),
        }
        for trade_date, ratio in ratios
    ]


def _relative_strength_summary(series: list[dict[str, Any]]) -> dict[str, Any]:
    if not series:
        return {
            "as_of_date": "",
            "return_20d_pct": None,
            "return_60d_pct": None,
            "state": "UNKNOWN",
            "state_label": "数据不足",
        }

    def period_return(period: int) -> float | None:
        if len(series) < period + 1:
            return None
        baseline = float(series[-(period + 1)].get("value") or 0)
        latest = float(series[-1].get("value") or 0)
        if baseline <= 0:
            return None
        return round((latest / baseline - 1) * 100, 2)

    return_20d = period_return(20)
    return_60d = period_return(60)
    if return_20d is None:
        state, label = "UNKNOWN", "数据不足"
    elif return_60d is None:
        if return_20d > 0:
            state, label = "SHORT_OUTPERFORMING", "近20日相对占优"
        elif return_20d < 0:
            state, label = "SHORT_UNDERPERFORMING", "近20日相对落后"
        else:
            state, label = "FLAT", "近20日基本持平"
    elif return_20d > 0 and return_60d > 0:
        state, label = "OUTPERFORMING", "短中期相对占优"
    elif return_20d < 0 and return_60d < 0:
        state, label = "UNDERPERFORMING", "短中期相对落后"
    elif return_20d > 0 and return_60d < 0:
        state, label = "RECOVERING", "短期修复，中期仍弱"
    elif return_20d < 0 and return_60d > 0:
        state, label = "WEAKENING", "短期转弱，中期仍强"
    else:
        state, label = "FLAT", "相对基本持平"
    return {
        "as_of_date": str(series[-1].get("trade_date") or ""),
        "return_20d_pct": return_20d,
        "return_60d_pct": return_60d,
        "state": state,
        "state_label": label,
    }
