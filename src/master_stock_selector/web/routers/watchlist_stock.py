from __future__ import annotations

from datetime import date as calendar_date
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from ...watchlist.brooks import BROOKS_POLICY_VERSION
from ...watchlist.charting import (
    KELTNER_PRESETS,
    default_keltner_preset,
    keltner_channels,
    keltner_config_payload,
)
from ...watchlist.climax_service import chart_climax
from ..stock_catalog import StockCatalog
from ..users import (
    C1_ENTRY_VARIANT_LABELS,
    C1_STRUCTURE_STATE_LABELS,
    OTHER_SETUP_ID,
    OTHER_SETUP_LABEL,
)
from .auth import current_user
from .watchlist import (
    BROOKS_STATE_LABELS,
    EXIT_REASON_OPTIONS,
    MANUAL_LABELS,
    METHOD_LABELS,
    MINERVINI_CHECK_LABELS,
    MINERVINI_METRIC_LABELS,
    RESULT_LABELS,
    STAGE_LABELS,
    STATE_LABELS,
    _chart_method_overlay,
    _chart_method_statuses,
    _chart_moving_averages,
    _market_cap_floor,
    _minervini_check_results,
    _stock_anomaly_evidence,
    _stock_external_urls,
    _stock_navigation,
    _weekly_bars,
    _with_period_changes,
)
from .watchlist_context import WatchlistRouteContext


def build_stock_router(context: WatchlistRouteContext) -> APIRouter:
    router = APIRouter()
    render = context.render
    repository = context.repository
    market_reader = context.market_reader
    users = context.users
    stock_navigation_rows_for_user = context.stock_navigation_rows_for_user
    personal_navigation_rows = context.personal_navigation_rows
    open_position_navigation_rows = context.open_position_navigation_rows
    brooks_navigation_rows = context.brooks_navigation_rows

    catalog = StockCatalog(market_reader.path, repository.path)

    def selected_date(value: str | None) -> str:
        if value:
            try:
                return calendar_date.fromisoformat(value).isoformat()
            except ValueError as exc:
                raise HTTPException(status_code=422, detail="查询日期无效") from exc
        return max(repository.latest_fact_date(), market_reader.safe_latest_market_date()) or calendar_date.today().isoformat()

    def stored_stock(symbol: str, date: str | None) -> tuple[str, dict[str, Any], str]:
        cutoff = selected_date(date)
        inventory = catalog.inventory(cutoff)
        symbol = symbol.strip().upper()
        if len(symbol) == 6 and symbol.isdigit():
            matches = [key for key in inventory if key.split(".")[0] == symbol]
            if not matches:
                matches = [key for key in catalog.inventory("9999-12-31") if key.split(".")[0] == symbol]
            if len(matches) == 1:
                symbol = matches[0]
        item = catalog.lookup(symbol, cutoff)
        if not item:
            # A known stock may not yet have data at the requested historical cutoff.
            item = catalog.inventory("9999-12-31").get(symbol)
            if not item:
                raise HTTPException(status_code=404, detail="库内未找到该股票")
            item = {"symbol": symbol, "name": "", "industry": ""}
        payload = repository.stock_detail(symbol, as_of_date=cutoff)
        payload["identity"] = {**{k: item[k] for k in ("name", "list_date", "is_st", "is_suspended") if k in item}, **payload["identity"]}
        payload["market_date"] = item.get("market_date", "")
        payload["fact_date"] = item.get("fact_date", "")
        if not payload["industry"] and item.get("industry"):
            payload["industry"] = {"industry_name": item["industry"]}
        return symbol, payload, cutoff

    def preserve_context(navigation: dict[str, Any], request: Request) -> None:
        navigation.setdefault("entry_query", navigation["query"])
        event_id = request.query_params.get("event_id", "")[:120]
        additions: dict[str, str] = {}
        source_date = request.query_params.get("source_date", "")
        lookback = request.query_params.get("lookback", "")
        if source_date:
            additions["source_date"] = selected_date(source_date)
        if lookback in {"5", "10", "20", "60"}:
            additions["lookback"] = lookback
        if additions:
            navigation["query"] += "&" + urlencode(additions)
            navigation["entry_query"] += "&" + urlencode(additions)
        if event_id:
            navigation["entry_query"] += "&" + urlencode({"event_id": event_id})
        if request.query_params.get("section") == "brooks-results":
            parsed = urlsplit(navigation["list_url"])
            params = dict(parse_qsl(parsed.query))
            if source_date:
                params["date"] = additions["source_date"]
            if lookback in {"5", "10", "20", "60"}:
                params["lookback"] = lookback
            navigation["list_url"] = parsed.path + "?" + urlencode(params)

    @router.get("/api/a/stocks/search")
    def search_stocks(q: str = Query(default="", max_length=80), date: str | None = None) -> dict[str, Any]:
        cutoff = selected_date(date)
        rows = catalog.search(q, cutoff)
        for item in rows:
            item["url"] = "/a/stocks/" + item["symbol"] + "?" + urlencode({"date": cutoff, "section": "stock-search", "q": q})
            item["data_status"] = "行情与方法事实" if item.get("market_date") and item.get("fact_date") else "已有行情 · 暂无方法事实" if item.get("market_date") else "已有方法事实" if item.get("fact_date") else "仅基础资料"
        return {"query_date": cutoff, "rows": rows}

    @router.get("/a/stocks", response_class=HTMLResponse)
    def stock_search(request: Request, q: str = Query(default="", max_length=80), date: str | None = None) -> HTMLResponse:
        result = search_stocks(q, date)
        return render(request, "ashare/stock_search.html", {**result, "q": q, "active": "查股票", "latest_date": result["query_date"]})

    @router.get("/a/stocks/{symbol}", response_class=HTMLResponse)
    def stock_detail(
        request: Request,
        symbol: str,
        date: str | None = None,
        view: str | None = None,
        method: str = "all",
        state: str = "all",
        manual: str = "all",
        min_cap: int = Query(default=0),
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
        symbol, payload, selected = stored_stock(symbol, date)
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
        query_date = selected
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
        if user is not None and editing_trade and editing_trade.get("plan_version_id"):
            # Keep the frozen version's choices available for faithful display, but
            # also allow an old execution to be deliberately reclassified into a
            # setup added by the current plan version (for example C2).
            known_values = {str(option["value"]) for option in setup_options}
            setup_options.extend(
                option
                for option in users.trade_setup_options(user.user_id)
                if str(option["value"]) not in known_values
            )
        current_plan = users.trading_plans.current(user.user_id) if user is not None else None
        setup_observations = (
            users.setup_observations(user.user_id, symbol) if user is not None else []
        )
        setup_observation_options = [
            item
            for item in (current_plan or {}).get("setup_catalog", [])
            if item.get("enabled")
            and str(item.get("status") or "ACTIVE") == "ACTIVE"
            and str(item.get("setup_id") or "") != "C1"
        ]
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
        preserve_context(navigation, request)
        payload["market_metrics"] = market_reader.safe_stock_market_metrics(
            query_date,
            [symbol],
        ).get(symbol.upper(), {})
        payload["brooks"] = repository.brooks_stock_fact(symbol, query_date, BROOKS_POLICY_VERSION, event_id=request.query_params.get("event_id", "")[:120])
        if payload["brooks"]:
            brooks = payload["brooks"]
            brooks["state_label"] = BROOKS_STATE_LABELS.get(str(brooks.get("state") or ""), "未知")
            if (
                str(brooks.get("state") or "") == "S2_H1"
                and str(brooks.get("h1_date") or "") == str(brooks.get("as_of_date") or "")
            ):
                cycle = int((brooks.get("evidence") or {}).get("pullback_cycle") or 0)
                brooks["state_label"] = (
                    "今日Leg1首次H1" if cycle == 1 else "今日后续周期H1"
                )
            current_close = float(brooks.get("evidence", {}).get("current_close") or 0)
            brooks["current_close"] = current_close
            brooks["retracement_pct"] = float(brooks.get("current_retracement") or 0) * 100
        payload["anomaly_evidence"] = _stock_anomaly_evidence(
            repository, market_reader, payload, query_date, symbol
        )
        payload["chart_ready"] = len(market_reader.safe_stock_chart_bars(symbol, query_date, limit=30)) >= 2
        payload["external_urls"] = _stock_external_urls(symbol)
        return render(
            request,
            "ashare/watchlist_stock.html",
            {
                "active": {"brooks-results": "Brooks 事件观察", "personal-observations": "我的观察", "stock-search": "查股票"}.get(section, "大师观察池"),
                "query_date": query_date,
                "latest_date": query_date,
                "stock": payload,
                "symbol": symbol,
                "stock_name": str(payload["identity"].get("name") or symbol),
                "industry_name": str(payload["industry"].get("industry_name") or "行业待补"),
                "market_metrics": payload["market_metrics"],
                "external_urls": payload["external_urls"],
                "chart_methods": _chart_method_statuses(payload["history"], query_date),
                "keltner_presets": KELTNER_PRESETS,
                "daily_keltner": default_keltner_preset("day")[1],
                "initial_interval": "day" if section == "brooks-results" or method == "minervini" else "week" if method == "weinstein" else "",
                "manual_labels": MANUAL_LABELS,
                "minervini_check_labels": MINERVINI_CHECK_LABELS,
                "minervini_metric_labels": MINERVINI_METRIC_LABELS,
                "trade_setup_options": setup_options,
                "exit_reason_options": EXIT_REASON_OPTIONS,
                "default_plan_setup": OTHER_SETUP_ID,
                "setup_observations": setup_observations,
                "setup_observation_by_id": {
                    str(item["setup_id"]): item for item in setup_observations
                },
                "setup_observation_options": setup_observation_options,
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
        min_cap: int = Query(default=0),
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
        symbol, payload, selected = stored_stock(symbol, date)
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
        query_date = selected
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
        preserve_context(navigation, request)
        chart_methods = _chart_method_statuses(payload["history"], query_date)
        market_metrics = market_reader.safe_stock_market_metrics(query_date, [symbol]).get(
            symbol.upper(), {}
        )
        return render(
            request,
            "ashare/watchlist_stock_chart.html",
            {
                "active": {"brooks-results": "Brooks 事件观察", "personal-observations": "我的观察", "stock-search": "查股票"}.get(section, "大师观察池"),
                "query_date": query_date,
                "latest_date": query_date,
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
                "back_url": f"/a/stocks/{symbol.upper()}?{navigation['entry_query']}",
                "navigation": navigation,
                "private_workspace": user is not None,
                "initial_interval": "day" if section == "brooks-results" or method == "minervini" else "week" if method == "weinstein" else "",
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
        min_cap: int = Query(default=0),
        industry: str = "",
        q: str = "",
        section: str = "",
        brooks_state: str = "h1",
        min_amount: float = Query(default=0, ge=0, le=10000),
    ) -> HTMLResponse:
        symbol, payload, selected = stored_stock(symbol, date)
        query_date = selected
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
                "latest_date": query_date,
                "symbol": symbol.upper(),
                "stock_name": str(payload["identity"].get("name") or symbol.upper()),
                "industry_name": str(payload["industry"].get("industry_name") or ""),
                "tonghuashun_url": external_urls["tonghuashun_url"],
                "navigation": navigation,
            },
        )

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
        analysis_limit = None if interval == "day" else (
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
        brooks_first_legs = (
            repository.brooks_chart_legs(
                symbol,
                str(bars[0]["trade_date"]),
                str(bars[-1]["trade_date"]),
                BROOKS_POLICY_VERSION,
            )
            if interval == "day"
            else []
        )
        chart_closes = {str(row["trade_date"]): float(row["close"]) for row in bars}
        brooks_first_legs = [
            {
                **leg,
                "start_price": chart_closes[str(leg["start_date"])],
                "end_price": chart_closes[str(leg["end_date"])],
            }
            for leg in brooks_first_legs
            if str(leg["start_date"]) in chart_closes and str(leg["end_date"]) in chart_closes
        ]
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
            "brooks_first_legs": brooks_first_legs,
            "climax": chart_climax(repository.path, symbol, date, bars) if interval == "day" else {"status": "DAILY_ONLY", "events": []},
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

    return router
