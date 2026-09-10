from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .brooks import BROOKS_POLICY_VERSION
from .brooks_service import BrooksRunConfig, run_brooks_watchlist
from .climax_service import ready as climax_ready
from .climax_service import run_climax
from .collector import CollectionConfig, MarketProvider, collect_market_data
from .experimental_daily import run_experiment
from .repository import WatchlistRepository
from .service import WatchlistRunConfig, run_watchlist


@dataclass(frozen=True)
class DailyRunConfig:
    market_database: Path
    watchlist_database: Path
    trade_date: str = ""
    minimum_stock_count: int = 4500
    minimum_coverage_ratio: float = 0.95


def run_daily_closed_loop(
    config: DailyRunConfig,
    provider: MarketProvider | None = None,
) -> dict[str, Any]:
    target_date = config.trade_date or date.today().isoformat()
    if target_date != date.today().isoformat():
        raise ValueError(
            "每日闭环只保存真实当日 OBSERVED 事实；历史数据请使用 watchlist reconstructed"
        )
    repository = WatchlistRepository(config.watchlist_database)
    if not climax_ready(config.watchlist_database):
        raise ValueError("扩张图层表尚未初始化；请先显式运行 climax_service --migrate --apply")
    if not repository.brooks_schema_ready():
        raise ValueError(
            "Brooks 表尚未初始化；发布前请显式运行 "
            "watchlist --brooks-schema --apply"
        )
    collection = collect_market_data(
        CollectionConfig(
            market_database=config.market_database,
            trade_date=target_date,
            minimum_stock_count=config.minimum_stock_count,
            minimum_coverage_ratio=config.minimum_coverage_ratio,
        ),
        provider=provider,
    )
    if collection.get("status") == "SKIPPED":
        return {
            "status": "SKIPPED",
            "trade_date": target_date,
            "collection": collection,
            "selection": {"state": "NOT_RUN"},
            "brooks_selection": {"state": "NOT_RUN"},
        }

    selection = (
        {
            "state": "REUSED",
            "as_of_date": target_date,
        }
        if repository.latest_fact_date() >= target_date
        else run_watchlist(
            WatchlistRunConfig(
                market_database=config.market_database,
                watchlist_database=config.watchlist_database,
                as_of_date=target_date,
                from_date=target_date,
                origin="OBSERVED",
            )
        )
    )
    brooks_selection = (
        {
            "state": "REUSED",
            "as_of_date": target_date,
        }
        if repository.latest_brooks_run_date(BROOKS_POLICY_VERSION) >= target_date
        else run_brooks_watchlist(
            BrooksRunConfig(
                market_database=config.market_database,
                watchlist_database=config.watchlist_database,
                as_of_date=target_date,
                from_date=target_date,
                origin="OBSERVED",
            )
        )
    )
    return {
        "status": "SUCCESS",
        "trade_date": target_date,
        "collection": collection,
        "selection": selection,
        "brooks_selection": brooks_selection,
        "climax": run_climax(config.market_database, config.watchlist_database, target_date),
        "experimental": run_experiment(
            config.market_database,
            Path(os.environ.get("MASTERSTOCK_EXPERIMENT_DATABASE") or
                 config.watchlist_database.with_name("experimental_watchlist.sqlite3")),
            target_date,
            provider=provider,
            minimum=config.minimum_stock_count,
        ),
    }
