from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ...watchlist.industry import INDUSTRY_POLICY_VERSION
from ...watchlist.industry_confirmation import build_industry_weinstein_confirmation
from .watchlist import (
    BREADTH_BENCHMARKS,
    STYLE_RELATIVE_PAIRS,
    _breadth_method_summary,
    _decorate_index,
    _decorate_industry,
    _decorate_industry_confirmation,
    _decorate_industry_member,
    _industry_chart_summary,
    _industry_navigation,
    _market_overview_summary,
    _market_price_summary,
    _market_risk_context,
    _normalized_index_series,
    _proxy_summary,
    _relative_strength_series,
    _relative_strength_summary,
    _selected_date,
)
from .watchlist_context import WatchlistRouteContext


def build_market_router(context: WatchlistRouteContext) -> APIRouter:
    router = APIRouter()
    render = context.render
    repository = context.repository
    market_reader = context.market_reader
    cached_industry_heat = context.cached_industry_heat

    @router.get("/a/industries", response_class=HTMLResponse)
    def industries(
        request: Request,
        date: str | None = None,
        view: str = "hot",
        horizon: int = Query(default=5),
        method: str = "all",
        min_members: int = Query(default=1, ge=1, le=10000),
        q: str = "",
    ) -> HTMLResponse:
        query_date = _selected_date(repository, date, market_reader)
        view_mode = view.lower().strip()
        if view_mode not in {"hot", "all"}:
            raise HTTPException(status_code=422, detail="unsupported industry view")
        if horizon not in {5, 20}:
            raise HTTPException(status_code=422, detail="unsupported heat horizon")
        rows = [_decorate_industry(row) for row in cached_industry_heat(query_date)]
        query = q.strip().lower()
        filtered = []
        for row in rows:
            if view_mode == "hot" and not bool(row.get("is_hot")):
                continue
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
        filtered.sort(
            key=lambda row: (
                -(
                    float(row[f"return_{horizon}d_pct"])
                    if row.get(f"return_{horizon}d_pct") is not None
                    else -1000000.0
                ),
                -(
                    float(row["breadth_5d_pct"])
                    if row.get("breadth_5d_pct") is not None
                    else -1.0
                ),
                str(row.get("industry_code") or ""),
            )
        )
        return render(
            request,
            "ashare/watchlist_industries.html",
            {
                "active": "行业观察",
                "query_date": query_date,
                "latest_date": repository.latest_fact_date(),
                "available_dates": repository.available_dates(30),
                "rows": filtered,
                "filters": {
                    "view": view_mode,
                    "horizon": horizon,
                    "method": method,
                    "min_members": min_members,
                    "q": q,
                },
                "summary": {
                    "industries": len(rows),
                    "hot": sum(1 for row in rows if bool(row.get("is_hot"))),
                    "active": sum(1 for row in rows if int(row.get("union_pass_count") or 0) > 0),
                    "small_sample": sum(
                        1 for row in rows if row.get("quality_state") == "SMALL_SAMPLE"
                    ),
                    "unknown": sum(1 for row in rows if row.get("quality_state") == "UNKNOWN"),
                    "coverage": (
                        round(float(rows[0].get("membership_coverage_pct") or 0), 1)
                        if rows
                        else None
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
        heat = next(
            (
                row
                for row in cached_industry_heat(query_date)
                if str(row.get("industry_code") or "").upper() == industry_code.upper()
            ),
            {},
        )
        brooks_phases = {
            str(row.get("symbol") or "").upper(): str(row.get("phase") or "")
            for row in heat.get("brooks_members", [])
        }
        members = []
        for raw in payload["members"]:
            item = _decorate_industry_member(row=raw)
            item["brooks_phase"] = brooks_phases.get(str(item.get("symbol") or "").upper(), "")
            item["brooks_phase_label"] = {
                "new": "今日新Leg1",
                "h1": "今日首次H1",
                "cycle_h1": "今日周期H1",
            }.get(
                item["brooks_phase"],
                "当前Leg1" if item["brooks_phase"] else "暂无事实",
            )
            members.append(item)
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
                "heat": _decorate_industry(dict(heat)) if heat else {},
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

    @router.get("/api/a/industries/{query_date}")
    def api_industries(query_date: str) -> dict[str, Any]:
        return {
            "query_date": query_date,
            "taxonomy": "SW2021",
            "industry_level": "L3",
            "policy_version": INDUSTRY_POLICY_VERSION,
            "rows": [
                _decorate_industry(row) for row in cached_industry_heat(query_date)
            ],
        }

    @router.get("/api/a/industries/{query_date}/{industry_code}")
    def api_industry_detail(query_date: str, industry_code: str) -> dict[str, Any]:
        payload = repository.industry_detail(query_date, industry_code)
        if not payload:
            raise HTTPException(status_code=404, detail="industry observation not found")
        heat = next(
            (
                row
                for row in cached_industry_heat(query_date)
                if str(row.get("industry_code") or "").upper() == industry_code.upper()
            ),
            {},
        )
        phases = {
            str(row.get("symbol") or "").upper(): str(row.get("phase") or "")
            for row in heat.get("brooks_members", [])
        }
        members = [
            {
                **row,
                "brooks_phase": phases.get(str(row.get("symbol") or "").upper(), ""),
            }
            for row in payload["members"]
        ]
        return {"query_date": query_date, **payload, "members": members, "heat": heat}

    @router.get("/api/a/indices/{query_date}")
    def api_indices(query_date: str) -> dict[str, Any]:
        return {
            "query_date": query_date,
            "methods": {
                "weinstein": "full_stage",
            },
            "rows": [_decorate_index(row) for row in repository.index_facts(query_date)],
        }

    return router
