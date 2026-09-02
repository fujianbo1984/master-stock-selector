"""Independent Weinstein, Minervini, and Brooks A-share observation methods."""

from .brooks import BROOKS_POLICY_VERSION
from .brooks_service import BrooksRunConfig, run_brooks_watchlist
from .methods import (
    MINERVINI_INDEX_STAGE2_POLICY_VERSION,
    MINERVINI_POLICY_VERSION,
    WEINSTEIN_POLICY_VERSION,
)
from .service import WatchlistRunConfig, run_watchlist

__all__ = [
    "BROOKS_POLICY_VERSION",
    "BrooksRunConfig",
    "MINERVINI_INDEX_STAGE2_POLICY_VERSION",
    "MINERVINI_POLICY_VERSION",
    "WEINSTEIN_POLICY_VERSION",
    "WatchlistRunConfig",
    "run_watchlist",
    "run_brooks_watchlist",
]
