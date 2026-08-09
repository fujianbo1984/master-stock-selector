from __future__ import annotations

import argparse
from dataclasses import dataclass


@dataclass(frozen=True)
class CommandPolicy:
    fact_level: str
    requires_apply: bool = False


COMMAND_POLICIES: dict[str, CommandPolicy] = {
    "agent": CommandPolicy("authenticated_agent_api_client"),
    "database-optimize": CommandPolicy("deployment_database_candidate", requires_apply=True),
    "database-validate": CommandPolicy("deployment_database_validation"),
    "daily": CommandPolicy("market_and_master_watchlist_fact", requires_apply=True),
    "market-backfill": CommandPolicy("market_history_fact"),
    "reference-backfill": CommandPolicy("market_reference_fact", requires_apply=True),
    "reference-materialize": CommandPolicy("master_watchlist_reference_fact", requires_apply=True),
    "watchlist": CommandPolicy("master_watchlist_fact", requires_apply=True),
    "web": CommandPolicy("read_only_product_surface"),
}


def enforce_command_policy(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> None:
    policy = COMMAND_POLICIES[str(args.command)]
    if policy.requires_apply and not bool(getattr(args, "apply", False)):
        parser.error(f"{args.command} 会写入观察池事实；必须显式传入 --apply")
