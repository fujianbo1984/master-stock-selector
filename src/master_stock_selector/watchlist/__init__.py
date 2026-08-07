"""Two-master watchlist for A-share stocks and market indices."""

from .methods import (
    MINERVINI_INDEX_STAGE2_POLICY_VERSION,
    MINERVINI_POLICY_VERSION,
    WEINSTEIN_POLICY_VERSION,
)
from .service import WatchlistRunConfig, run_watchlist

__all__ = [
    "MINERVINI_INDEX_STAGE2_POLICY_VERSION",
    "MINERVINI_POLICY_VERSION",
    "WEINSTEIN_POLICY_VERSION",
    "WatchlistRunConfig",
    "run_watchlist",
]
