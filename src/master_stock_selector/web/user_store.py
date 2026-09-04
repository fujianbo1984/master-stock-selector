from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any

from argon2 import PasswordHasher

if TYPE_CHECKING:
    from .trading_plans import TradingPlanService
    from .users import ApiTokenPrincipal, UserRepository


class UserStore:
    """Shared database and cross-service access for composed user repositories."""

    def __init__(self, repository: UserRepository) -> None:
        self.repository = repository

    def connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        return self.repository.connect(read_only=read_only)

    @property
    def path(self) -> Path:
        return self.repository.path

    @property
    def password_hasher(self) -> PasswordHasher:
        return self.repository.password_hasher

    @property
    def trading_plans(self) -> TradingPlanService:
        return self.repository.trading_plans

    def _insert_api_audit(
        self,
        connection: sqlite3.Connection,
        principal: ApiTokenPrincipal,
        *,
        action: str,
        request_id: str,
        outcome: str,
        details: dict[str, Any],
    ) -> None:
        self.repository.identity._insert_api_audit(
            connection,
            principal,
            action=action,
            request_id=request_id,
            outcome=outcome,
            details=details,
        )
