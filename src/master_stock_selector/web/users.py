from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from argon2 import PasswordHasher

from ..trade_domain import (
    DEFAULT_TRADE_SETUP,
    TRADE_METHOD_LABELS,
    TRADE_SETUP_ALIASES,
    TRADE_SETUP_LABELS,
)
from .plan_setups import (
    PLAN_SETUP_BY_TRADE_SETUP,
    TRADE_SETUP_BY_PLAN_SETUP,
)

SESSION_COOKIE = "masterstock_session"
SESSION_IDLE_SECONDS = 7 * 24 * 60 * 60
API_TOKEN_PREFIX = "mst_"
API_TOKEN_DEFAULT_EXPIRES_DAYS = 365
API_TOKEN_MAX_EXPIRES_DAYS = 365
API_TOKEN_SCOPES = frozenset(
    {
        "observations:write",
        "trades:read",
        "trades:write",
        "plans:read",
        "plans:write",
        "plans:publish",
        "reading:read",
        "reading:write",
        "reading:share",
    }
)
OTHER_SETUP_ID = "OTHER"
OTHER_SETUP_LABEL = "其他（未归入交易计划）"
C1_STRUCTURE_STATES = frozenset(
    {"RANGE", "NEAR_LOWER", "FAILED_TEST", "WAIT_SECOND", "READY", "INVALID"}
)
C1_ENTRY_VARIANTS = frozenset({"FIRST", "SECOND"})
C1_STRUCTURE_STATE_LABELS = {
    "RANGE": "区间观察",
    "NEAR_LOWER": "接近下沿",
    "FAILED_TEST": "首次失败测试",
    "WAIT_SECOND": "等待二次测试",
    "READY": "触发准备",
    "INVALID": "结构失效",
}
C1_ENTRY_VARIANT_LABELS = {"FIRST": "首次入场", "SECOND": "二次入场"}
USER_SCHEMA_VERSION = 13
OBSERVATION_TAG_MAX_COUNT = 8
OBSERVATION_TAG_MAX_LENGTH = 20
DEFAULT_ACCESS_LOG_RETENTION_DAYS = 90
USER_REQUIRED_TABLES = frozenset(
    {
        "user_account",
        "user_session",
        "user_api_token",
        "user_trade_batch",
        "user_api_audit",
        "user_stock_review",
        "user_setup_observation",
        "user_trade_execution",
        "user_chart_drawing",
        "user_access_log",
        "user_access_daily_summary",
        "user_trading_plan",
        "user_trading_plan_version",
        "user_plan_grill_session",
        "user_plan_grill_event",
        "user_trading_plan_share",
        "user_reading_note",
        "user_reading_note_version",
        "user_reading_note_share",
    }
)
USER_REQUIRED_INDEXES = frozenset(
    {
        "idx_user_trade_source_ref",
        "idx_user_access_occurred",
        "idx_user_plan_version_plan",
        "idx_user_plan_session_user_updated",
        "idx_user_plan_event_session_revision",
        "idx_user_plan_share_plan_status",
        "idx_user_plan_share_active",
        "idx_user_reading_note_user_updated",
        "idx_user_reading_version_note",
        "idx_user_reading_share_note_status",
        "idx_user_reading_share_active",
        "idx_user_setup_observation_updated",
    }
)


def _setup_assignment(
    plan: Mapping[str, Any] | None,
    *,
    plan_setup_id: str = OTHER_SETUP_ID,
    setup_code: str | None = None,
    setup_method: str = "",
    other_setup_note: str = "",
) -> dict[str, Any]:
    requested = str(plan_setup_id or "").strip().upper()
    explicit_code = str(setup_code or "").strip().upper()
    normalized_method = str(setup_method or "").strip().upper()
    normalized_method = TRADE_SETUP_ALIASES.get(normalized_method, normalized_method)
    if plan is None and normalized_method:
        return {
            "setup_kind": "LEGACY",
            "plan_setup_id": None,
            "setup_code": None,
            "setup_code_snapshot": "",
            "setup_name_snapshot": TRADE_SETUP_LABELS.get(normalized_method, normalized_method),
            "other_setup_note": "",
            "setup_method": normalized_method,
            "evaluation_cycle_id": None,
        }
    if explicit_code:
        requested = explicit_code
    elif not requested and normalized_method:
        requested = PLAN_SETUP_BY_TRADE_SETUP.get(normalized_method, "")
    elif requested == OTHER_SETUP_ID and normalized_method:
        # Backwards-compatible explicit callers may still submit setup_method.
        requested = PLAN_SETUP_BY_TRADE_SETUP.get(normalized_method, requested)
    if requested and requested != OTHER_SETUP_ID:
        if plan is None:
            raise ValueError("没有可绑定的交易计划版本")
        match = next(
            (
                item
                for item in plan.get("setup_catalog", [])
                if requested
                in {
                    str(item.get("setup_id") or "").upper(),
                    str(item.get("code") or "").upper(),
                }
                and bool(item.get("enabled"))
                and str(item.get("status") or "ACTIVE").upper() == "ACTIVE"
            ),
            None,
        )
        if match is None:
            raise ValueError("交易方案不属于所选交易计划版本或未启用")
        code = str(match.get("code") or requested)
        return {
            "setup_kind": "PLAN",
            "plan_setup_id": str(match.get("setup_id") or requested),
            "setup_code": code,
            "setup_code_snapshot": code,
            "setup_name_snapshot": str(match.get("name") or code),
            "other_setup_note": "",
            "setup_method": TRADE_SETUP_BY_PLAN_SETUP.get(code, DEFAULT_TRADE_SETUP),
            "evaluation_cycle_id": str(
                match.get("evaluation_cycle_id") or plan.get("evaluation_cycle_id") or ""
            )
            or None,
        }
    return {
        "setup_kind": "OTHER",
        "plan_setup_id": None,
        "setup_code": None,
        "setup_code_snapshot": "",
        "setup_name_snapshot": OTHER_SETUP_LABEL,
        "other_setup_note": str(other_setup_note or "").strip()[:300],
        # Kept only for the legacy CHECK-constrained compatibility column.
        "setup_method": DEFAULT_TRADE_SETUP,
        "evaluation_cycle_id": None,
    }


def _decorate_setup(item: dict[str, Any]) -> None:
    kind = str(item.get("setup_kind") or "LEGACY")
    snapshot = str(item.get("setup_name_snapshot") or "").strip()
    if kind == "OTHER":
        item["setup_label"] = OTHER_SETUP_LABEL
    elif snapshot:
        prefix = "历史：" if kind == "LEGACY" else ""
        item["setup_label"] = prefix + snapshot
    else:
        setup = str(item.get("setup_method") or DEFAULT_TRADE_SETUP)
        item["setup_label"] = TRADE_SETUP_LABELS.get(setup, setup)


USER_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS user_account (
    user_id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'DISABLED')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_login_at TEXT
);
CREATE TABLE IF NOT EXISTS user_session (
    session_id_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    csrf_secret TEXT NOT NULL,
    created_at_epoch INTEGER NOT NULL,
    last_seen_epoch INTEGER NOT NULL,
    expires_at_epoch INTEGER NOT NULL,
    revoked_at_epoch INTEGER,
    FOREIGN KEY (user_id) REFERENCES user_account(user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_user_session_user_expiry
ON user_session(user_id, expires_at_epoch);
CREATE TABLE IF NOT EXISTS user_api_token (
    token_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    token_prefix TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    scopes_json TEXT NOT NULL,
    created_at_epoch INTEGER NOT NULL,
    expires_at_epoch INTEGER NOT NULL,
    last_used_epoch INTEGER,
    revoked_at_epoch INTEGER,
    FOREIGN KEY (user_id) REFERENCES user_account(user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_user_api_token_user
ON user_api_token(user_id, created_at_epoch DESC);
CREATE TABLE IF NOT EXISTS user_trade_batch (
    batch_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, idempotency_key),
    FOREIGN KEY (user_id) REFERENCES user_account(user_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS user_api_audit (
    audit_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    action TEXT NOT NULL,
    request_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES user_account(user_id) ON DELETE CASCADE,
    FOREIGN KEY (token_id) REFERENCES user_api_token(token_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_user_api_audit_user_created
ON user_api_audit(user_id, created_at DESC);
CREATE TABLE IF NOT EXISTS user_stock_review (
    user_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    manual_state TEXT NOT NULL CHECK (
        manual_state IN ('WATCH', 'FOCUS', 'ARCHIVED')
    ),
    note TEXT NOT NULL DEFAULT '',
    tags_json TEXT NOT NULL DEFAULT '[]',
    c1_record_json TEXT NOT NULL DEFAULT '{}',
    reviewed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, symbol),
    FOREIGN KEY (user_id) REFERENCES user_account(user_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS user_setup_observation (
    user_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    setup_id TEXT NOT NULL,
    plan_version_id TEXT,
    setup_code_snapshot TEXT NOT NULL,
    setup_name_snapshot TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('WATCH', 'READY', 'INVALID')),
    evidence_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, symbol, setup_id),
    FOREIGN KEY (user_id) REFERENCES user_account(user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_user_setup_observation_updated
ON user_setup_observation(user_id, updated_at DESC);
CREATE TABLE IF NOT EXISTS user_trade_execution (
    execution_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    traded_on TEXT NOT NULL,
    traded_at TEXT NOT NULL DEFAULT '',
    source_ref TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    price REAL NOT NULL CHECK (price > 0),
    fee REAL NOT NULL DEFAULT 0 CHECK (fee >= 0),
    method TEXT NOT NULL CHECK (method IN ('WEINSTEIN', 'MINERVINI', 'MANUAL')),
    setup_method TEXT NOT NULL DEFAULT 'PULLBACK_SUPPORT'
        CHECK (setup_method IN (
            'FAILED_TEST', 'PULLBACK_SUPPORT', 'LOWER_TIMEFRAME_BREAKOUT',
            'COMPLEX_PULLBACK', 'ANTI', 'PRE_BREAKOUT_BASE',
            'POST_BREAKOUT_FIRST_PULLBACK', 'FAILED_BREAKOUT'
        )),
    stop_price REAL CHECK (stop_price IS NULL OR stop_price > 0),
    initial_stop_price REAL CHECK (initial_stop_price IS NULL OR initial_stop_price > 0),
    rationale TEXT NOT NULL DEFAULT '',
    invalidation TEXT NOT NULL DEFAULT '',
    exit_reason TEXT NOT NULL DEFAULT '',
    market_context TEXT NOT NULL DEFAULT '',
                            observation_snapshot_json TEXT NOT NULL DEFAULT '{}',
                            plan_version_id TEXT,
                            evaluation_cycle_id TEXT,
                            setup_code TEXT,
                            setup_kind TEXT NOT NULL DEFAULT 'LEGACY'
                                CHECK (setup_kind IN ('PLAN', 'OTHER', 'LEGACY')),
                            plan_setup_id TEXT,
                            setup_code_snapshot TEXT NOT NULL DEFAULT '',
                            setup_name_snapshot TEXT NOT NULL DEFAULT '',
                            other_setup_note TEXT NOT NULL DEFAULT '',
                            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    FOREIGN KEY (user_id) REFERENCES user_account(user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_user_trade_date
ON user_trade_execution(user_id, traded_on, created_at);
CREATE INDEX IF NOT EXISTS idx_user_trade_symbol
ON user_trade_execution(user_id, symbol, traded_on, created_at);
CREATE TABLE IF NOT EXISTS user_trading_plan (
    plan_id TEXT PRIMARY KEY, user_id TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
    current_version_id TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES user_account(user_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS user_trading_plan_version (
    version_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, version_number TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('DRAFT','ACTIVE','ARCHIVED','REVOKED')),
    schema_version TEXT NOT NULL, content_json TEXT NOT NULL, change_summary TEXT NOT NULL DEFAULT '',
    change_class TEXT NOT NULL DEFAULT 'SUBSTANTIVE', evaluation_cycle_id TEXT NOT NULL,
    supersedes_version_id TEXT, content_hash TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    activated_at TEXT, archived_at TEXT, UNIQUE(plan_id, version_number),
    FOREIGN KEY(plan_id) REFERENCES user_trading_plan(plan_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_user_plan_version_plan ON user_trading_plan_version(plan_id, created_at DESC);
CREATE TABLE IF NOT EXISTS user_plan_grill_session (
    session_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, plan_id TEXT NOT NULL, base_version_id TEXT,
    head_version_id_at_start TEXT,
    grill_version TEXT NOT NULL, change_goal TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
    answers_json TEXT NOT NULL DEFAULT '{}', revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    published_version_id TEXT,
    FOREIGN KEY(user_id) REFERENCES user_account(user_id) ON DELETE CASCADE,
    FOREIGN KEY(plan_id) REFERENCES user_trading_plan(plan_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_user_plan_session_user_updated
ON user_plan_grill_session(user_id, updated_at DESC);
CREATE TABLE IF NOT EXISTS user_plan_grill_event (
    event_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, question_id TEXT NOT NULL, event_type TEXT NOT NULL,
    value_json TEXT NOT NULL, revision INTEGER NOT NULL, actor_type TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(session_id) REFERENCES user_plan_grill_session(session_id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_user_plan_event_session_revision
ON user_plan_grill_event(session_id, revision);
CREATE TABLE IF NOT EXISTS user_trading_plan_share (
    share_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    statement TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK(status IN ('ACTIVE','SUPERSEDED','REVOKED')),
    published_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revoked_at TEXT,
    FOREIGN KEY(plan_id) REFERENCES user_trading_plan(plan_id) ON DELETE CASCADE,
    FOREIGN KEY(version_id) REFERENCES user_trading_plan_version(version_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_user_plan_share_plan_status
ON user_trading_plan_share(plan_id, status, published_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_user_plan_share_active
ON user_trading_plan_share(plan_id) WHERE status='ACTIVE';
CREATE TABLE IF NOT EXISTS user_reading_note (
    note_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    slug TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    tags_json TEXT NOT NULL,
    body_markdown TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    assets_json TEXT NOT NULL DEFAULT '{}',
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
    current_version_id TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, slug),
    FOREIGN KEY(user_id) REFERENCES user_account(user_id) ON DELETE CASCADE,
    FOREIGN KEY(current_version_id) REFERENCES user_reading_note_version(version_id)
);
CREATE INDEX IF NOT EXISTS idx_user_reading_note_user_updated
ON user_reading_note(user_id, updated_at DESC);
CREATE TABLE IF NOT EXISTS user_reading_note_version (
    version_id TEXT PRIMARY KEY,
    note_id TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK(version_number > 0),
    slug TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    tags_json TEXT NOT NULL,
    body_markdown TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    assets_json TEXT NOT NULL DEFAULT '{}',
    content_hash TEXT NOT NULL,
    submitted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(note_id, version_number),
    FOREIGN KEY(note_id) REFERENCES user_reading_note(note_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_user_reading_version_note
ON user_reading_note_version(note_id, submitted_at DESC);
CREATE TABLE IF NOT EXISTS user_reading_note_share (
    share_id TEXT PRIMARY KEY,
    note_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    statement TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK(status IN ('ACTIVE','SUPERSEDED','REVOKED')),
    shared_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revoked_at TEXT,
    FOREIGN KEY(note_id) REFERENCES user_reading_note(note_id) ON DELETE CASCADE,
    FOREIGN KEY(version_id) REFERENCES user_reading_note_version(version_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_user_reading_share_note_status
ON user_reading_note_share(note_id, status, shared_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_user_reading_share_active
ON user_reading_note_share(note_id) WHERE status='ACTIVE';
CREATE TABLE IF NOT EXISTS user_chart_drawing (
    drawing_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    price_scale_id TEXT NOT NULL,
    tool TEXT NOT NULL CHECK (tool IN ('trendline', 'horizontal', 'text')),
    anchors_json TEXT NOT NULL,
    content_text TEXT NOT NULL DEFAULT '',
    interval_scope TEXT NOT NULL DEFAULT 'both'
        CHECK (interval_scope IN ('day', 'week', 'both')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES user_account(user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_user_chart_symbol
ON user_chart_drawing(user_id, symbol, price_scale_id, created_at);
CREATE TABLE IF NOT EXISTS user_access_log (
    access_id TEXT PRIMARY KEY,
    occurred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    user_id TEXT,
    client_ip TEXT NOT NULL,
    country_code TEXT NOT NULL DEFAULT '',
    country_name TEXT NOT NULL DEFAULT '',
    region_name TEXT NOT NULL DEFAULT '',
    city_name TEXT NOT NULL DEFAULT '',
    location_source TEXT NOT NULL,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    FOREIGN KEY (user_id) REFERENCES user_account(user_id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_user_access_occurred
ON user_access_log(occurred_at DESC);
CREATE TABLE IF NOT EXISTS user_access_daily_summary (
    summary_date TEXT NOT NULL,
    country_code TEXT NOT NULL DEFAULT '',
    region_name TEXT NOT NULL DEFAULT '',
    path_group TEXT NOT NULL,
    method TEXT NOT NULL,
    status_class INTEGER NOT NULL,
    request_count INTEGER NOT NULL CHECK (request_count >= 0),
    unique_ip_count INTEGER NOT NULL CHECK (unique_ip_count >= 0),
    summarized_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (
        summary_date, country_code, region_name, path_group, method, status_class
    )
);
"""


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: str
    username: str
    display_name: str
    csrf_token: str


@dataclass(frozen=True)
class ApiTokenPrincipal:
    user_id: str
    username: str
    display_name: str
    token_id: str
    scopes: tuple[str, ...]


class TradeBatchValidationError(ValueError):
    def __init__(self, report: dict[str, Any]):
        super().__init__("交易批次未通过预检")
        self.report = report


class TradeRevisionConflict(ValueError):
    pass


class UserRepository:
    """Account and private-workspace storage; user_id is the tenant boundary."""

    def __init__(self, path: str | Path):
        from .user_database import UserDatabase
        from .user_identity import UserIdentityStore
        from .user_observations import UserObservationStore
        from .user_trades import UserTradeStore

        self.path = Path(path)
        self.password_hasher = PasswordHasher()
        self._trading_plans: TradingPlanService | None = None
        self._reading_notes: ReadingNoteService | None = None
        self.database = UserDatabase(self)
        self.identity = UserIdentityStore(self)
        self.observations = UserObservationStore(self)
        self.trades = UserTradeStore(self)

    @property
    def trading_plans(self) -> TradingPlanService:
        if self._trading_plans is None:
            from .trading_plans import TradingPlanService

            self._trading_plans = TradingPlanService(self)
        return self._trading_plans

    @property
    def reading_notes(self) -> ReadingNoteService:
        if self._reading_notes is None:
            from .reading_notes import ReadingNoteService

            self._reading_notes = ReadingNoteService(self)
        return self._reading_notes

    def connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        return self.database.connect(read_only=read_only)

    def initialize(self) -> None:
        """Create or migrate a user database explicitly (tests/provisioning only)."""
        self.database.initialize()

    def schema_status(self) -> dict[str, Any]:
        return self.database.schema_status()

    def require_schema(self) -> None:
        self.database.require_schema()

    def migrate_schema(self) -> dict[str, Any]:
        return self.database.migrate_schema()

    def create_user(self, username: str, password: str, display_name: str = "") -> str:
        return self.identity.create_user(username, password, display_name)

    def list_users(self) -> list[dict[str, Any]]:
        return self.identity.list_users()

    def active_user_by_username(self, username: str) -> dict[str, Any] | None:
        return self.identity.active_user_by_username(username)

    def readiness(self) -> dict[str, Any]:
        return self.identity.readiness()

    def record_access(
        self,
        *,
        user_id: str | None,
        client_ip: str,
        country_code: str,
        country_name: str,
        region_name: str,
        city_name: str,
        location_source: str,
        method: str,
        path: str,
        status_code: int,
    ) -> str:
        return self.identity.record_access(
            user_id=user_id,
            client_ip=client_ip,
            country_code=country_code,
            country_name=country_name,
            region_name=region_name,
            city_name=city_name,
            location_source=location_source,
            method=method,
            path=path,
            status_code=status_code,
        )

    def access_log_maintenance_status(
        self, *, retention_days: int = DEFAULT_ACCESS_LOG_RETENTION_DAYS, as_of: date | None = None
    ) -> dict[str, Any]:
        return self.identity.access_log_maintenance_status(
            retention_days=retention_days, as_of=as_of
        )

    def maintain_access_log(
        self, *, retention_days: int = DEFAULT_ACCESS_LOG_RETENTION_DAYS, as_of: date | None = None
    ) -> dict[str, Any]:
        return self.identity.maintain_access_log(retention_days=retention_days, as_of=as_of)

    def set_user_status(self, username: str, status: str) -> None:
        return self.identity.set_user_status(username, status)

    def reset_password(self, username: str, password: str) -> None:
        return self.identity.reset_password(username, password)

    def change_password(self, user_id: str, current_password: str, new_password: str) -> bool:
        return self.identity.change_password(user_id, current_password, new_password)

    def authenticate(self, username: str, password: str) -> dict[str, str] | None:
        return self.identity.authenticate(username, password)

    def create_session(self, user_id: str) -> tuple[str, str]:
        return self.identity.create_session(user_id)

    def session_user(self, raw_token: str | None) -> AuthenticatedUser | None:
        return self.identity.session_user(raw_token)

    def revoke_session(self, raw_token: str | None) -> None:
        return self.identity.revoke_session(raw_token)

    def create_api_token(
        self,
        user_id: str,
        name: str,
        *,
        expires_days: int = API_TOKEN_DEFAULT_EXPIRES_DAYS,
        scopes: Sequence[str] = tuple(sorted(API_TOKEN_SCOPES)),
    ) -> tuple[str, dict[str, Any]]:
        return self.identity.create_api_token(
            user_id, name, expires_days=expires_days, scopes=scopes
        )

    def list_api_tokens(self, user_id: str) -> list[dict[str, Any]]:
        return self.identity.list_api_tokens(user_id)

    def revoke_api_token(self, user_id: str, token_id: str) -> bool:
        return self.identity.revoke_api_token(user_id, token_id)

    def api_token_user(
        self, raw_token: str | None, required_scope: str | None = None
    ) -> ApiTokenPrincipal | None:
        return self.identity.api_token_user(raw_token, required_scope)

    def audit_api_action(
        self,
        principal: ApiTokenPrincipal,
        *,
        action: str,
        request_id: str,
        outcome: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        return self.identity.audit_api_action(
            principal, action=action, request_id=request_id, outcome=outcome, details=details
        )

    @staticmethod
    def csrf_valid(user: AuthenticatedUser, supplied: str | None) -> bool:
        from .user_identity import UserIdentityStore

        return UserIdentityStore.csrf_valid(user, supplied)

    def reviews_for_symbols(
        self, user_id: str, symbols: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        return self.observations.reviews_for_symbols(user_id, symbols)

    def reviews_for_user(self, user_id: str) -> list[dict[str, Any]]:
        return self.observations.reviews_for_user(user_id)

    def review(self, user_id: str, symbol: str) -> dict[str, Any]:
        return self.observations.review(user_id, symbol)

    def save_review(
        self,
        user_id: str,
        symbol: str,
        manual_state: str,
        note: str,
        tags: str | Sequence[str] | None = None,
    ) -> None:
        return self.observations.save_review(user_id, symbol, manual_state, note, tags)

    def add_observations(
        self,
        principal: ApiTokenPrincipal,
        symbols: Sequence[str],
        *,
        note: str = "",
        tags: str | Sequence[str] | None = None,
        request_id: str,
    ) -> dict[str, Any]:
        return self.observations.add_observations(
            principal, symbols, note=note, tags=tags, request_id=request_id
        )

    def save_c1_record(
        self, user_id: str, symbol: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        return self.observations.save_c1_record(user_id, symbol, payload)

    def clear_c1_record(self, user_id: str, symbol: str) -> None:
        return self.observations.clear_c1_record(user_id, symbol)

    def setup_observations(self, user_id: str, symbol: str) -> list[dict[str, Any]]:
        return self.observations.setup_observations(user_id, symbol)

    def save_setup_observation(
        self, user_id: str, symbol: str, setup_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        return self.observations.save_setup_observation(user_id, symbol, setup_id, payload)

    def clear_setup_observation(self, user_id: str, symbol: str, setup_id: str) -> None:
        return self.observations.clear_setup_observation(user_id, symbol, setup_id)

    def trade_setup_options(
        self, user_id: str, *, plan_version_id: str | None = None
    ) -> list[dict[str, str]]:
        return self.observations.trade_setup_options(user_id, plan_version_id=plan_version_id)

    def record_trade(
        self,
        user_id: str,
        *,
        traded_on: str,
        traded_at: str = "",
        symbol: str,
        side: str,
        quantity: int,
        price: float,
        fee: float,
        method: str,
        setup_method: str = "",
        stop_price: float | None = None,
        rationale: str = "",
        invalidation: str = "",
        exit_reason: str = "",
        market_context: str = "",
        observation_snapshot: Mapping[str, Any] | None = None,
        permit_unmatched_sell: bool = False,
        plan_version_id: str | None = None,
        setup_code: str | None = None,
        plan_setup_id: str = OTHER_SETUP_ID,
        other_setup_note: str = "",
    ) -> str:
        return self.trades.record_trade(
            user_id,
            traded_on=traded_on,
            traded_at=traded_at,
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            fee=fee,
            method=method,
            setup_method=setup_method,
            stop_price=stop_price,
            rationale=rationale,
            invalidation=invalidation,
            exit_reason=exit_reason,
            market_context=market_context,
            observation_snapshot=observation_snapshot,
            permit_unmatched_sell=permit_unmatched_sell,
            plan_version_id=plan_version_id,
            setup_code=setup_code,
            plan_setup_id=plan_setup_id,
            other_setup_note=other_setup_note,
        )

    def trade_review(self, user_id: str, names: Mapping[str, str]) -> dict[str, Any]:
        return self.trades.trade_review(user_id, names)

    def trade_symbols(self, user_id: str) -> set[str]:
        return self.trades.trade_symbols(user_id)

    def trade_execution(self, user_id: str, execution_id: str) -> dict[str, Any] | None:
        return self.trades.trade_execution(user_id, execution_id)

    def update_trade(
        self,
        user_id: str,
        execution_id: str,
        *,
        traded_on: str,
        traded_at: str,
        side: str,
        quantity: int,
        price: float,
        fee: float,
        method: str,
        setup_method: str = "",
        plan_setup_id: str = OTHER_SETUP_ID,
        other_setup_note: str = "",
        stop_price: float | None = None,
        rationale: str = "",
        invalidation: str = "",
        exit_reason: str = "",
        market_context: str = "",
        observation_snapshot: Mapping[str, Any] | None = None,
    ) -> None:
        return self.trades.update_trade(
            user_id,
            execution_id,
            traded_on=traded_on,
            traded_at=traded_at,
            side=side,
            quantity=quantity,
            price=price,
            fee=fee,
            method=method,
            setup_method=setup_method,
            plan_setup_id=plan_setup_id,
            other_setup_note=other_setup_note,
            stop_price=stop_price,
            rationale=rationale,
            invalidation=invalidation,
            exit_reason=exit_reason,
            market_context=market_context,
            observation_snapshot=observation_snapshot,
        )

    def trade_executions(
        self,
        user_id: str,
        *,
        symbol: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.trades.trade_executions(
            user_id, symbol=symbol, date_from=date_from, date_to=date_to
        )

    def validate_trade_batch(
        self, user_id: str, trades: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        return self.trades.validate_trade_batch(user_id, trades)

    def record_trade_batch(
        self,
        user_id: str,
        trades: Sequence[Mapping[str, Any]],
        *,
        idempotency_key: str,
        principal: ApiTokenPrincipal,
        request_id: str,
    ) -> dict[str, Any]:
        return self.trades.record_trade_batch(
            user_id,
            trades,
            idempotency_key=idempotency_key,
            principal=principal,
            request_id=request_id,
        )

    def update_trade_stop(
        self,
        user_id: str,
        execution_id: str,
        stop_price: float,
        *,
        expected_revision: int,
        principal: ApiTokenPrincipal,
        request_id: str,
    ) -> dict[str, Any]:
        return self.trades.update_trade_stop(
            user_id,
            execution_id,
            stop_price,
            expected_revision=expected_revision,
            principal=principal,
            request_id=request_id,
        )

    def complete_trade_time(
        self,
        user_id: str,
        execution_id: str,
        traded_at: str,
        *,
        expected_revision: int,
        principal: ApiTokenPrincipal,
        request_id: str,
    ) -> dict[str, Any]:
        return self.trades.complete_trade_time(
            user_id,
            execution_id,
            traded_at,
            expected_revision=expected_revision,
            principal=principal,
            request_id=request_id,
        )

    def chart_trade_overlay(
        self, user_id: str, symbol: str, end_date: str
    ) -> dict[str, list[dict[str, Any]]]:
        return self.trades.chart_trade_overlay(user_id, symbol, end_date)

    def chart_drawings(
        self, user_id: str, symbol: str, price_scale_id: str, interval: str = "day"
    ) -> list[dict[str, Any]]:
        return self.trades.chart_drawings(user_id, symbol, price_scale_id, interval)

    def save_chart_drawing(
        self,
        user_id: str,
        drawing_id: str,
        symbol: str,
        price_scale_id: str,
        tool: str,
        anchors: Sequence[Mapping[str, Any]],
        content_text: str = "",
        interval_scope: str = "both",
    ) -> dict[str, Any]:
        return self.trades.save_chart_drawing(
            user_id, drawing_id, symbol, price_scale_id, tool, anchors, content_text, interval_scope
        )

    def delete_chart_drawing(
        self, user_id: str, drawing_id: str, symbol: str, price_scale_id: str
    ) -> bool:
        return self.trades.delete_chart_drawing(user_id, drawing_id, symbol, price_scale_id)


def _normalize_api_trade(payload: Mapping[str, Any], index: int) -> dict[str, Any]:
    allowed = {
        "client_id",
        "source_ref",
        "symbol",
        "traded_on",
        "traded_at",
        "side",
        "quantity",
        "price",
        "fee",
        "setup_method",
        "plan_setup_id",
        "other_setup_note",
        "stop_price",
        "rationale",
        "invalidation",
        "exit_reason",
        "market_context",
        "observation_snapshot",
        "plan_version_id",
        "setup_code",
    }
    unknown = sorted(str(key) for key in payload if str(key) not in allowed)
    if unknown:
        raise ValueError("不支持的字段：" + ", ".join(unknown))
    symbol = str(payload.get("symbol") or "").upper().strip()
    if not symbol or "." not in symbol:
        raise ValueError("股票代码格式不正确")
    quantity_value = payload.get("quantity")
    price_value = payload.get("price")
    if quantity_value is None or price_value is None:
        raise ValueError("缺少数量或价格")
    try:
        quantity = _strict_positive_integer(quantity_value, field="数量")
        price = float(str(price_value))
        fee = float(str(payload.get("fee", 0) or 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("数量、价格或费用格式不正确") from exc
    stop_value = payload.get("stop_price")
    try:
        stop_price = float(str(stop_value)) if stop_value not in (None, "") else None
    except (TypeError, ValueError) as exc:
        raise ValueError("止损价格式不正确") from exc
    side = str(payload.get("side") or "").upper().strip()
    if side == "SELL" and stop_price is not None:
        raise ValueError("SELL 不能填写止损价")
    explicit_setup_method = str(payload.get("setup_method") or "")
    values = _validated_trade_values(
        traded_on=str(payload.get("traded_on") or ""),
        traded_at=str(payload.get("traded_at") or ""),
        side=side,
        quantity=quantity,
        price=price,
        fee=fee,
        method="MANUAL",
        setup_method=explicit_setup_method or DEFAULT_TRADE_SETUP,
        stop_price=stop_price,
    )
    client_id = str(payload.get("client_id") or "").strip()
    if len(client_id) > 80:
        raise ValueError("client_id 最长为 80 个字符")
    source_ref = str(payload.get("source_ref") or "").strip()
    if len(source_ref) > 128:
        raise ValueError("source_ref 最长为 128 个字符")
    snapshot = payload.get("observation_snapshot") or {}
    if not isinstance(snapshot, Mapping):
        raise ValueError("observation_snapshot 格式不正确")
    setup_code = str(payload.get("setup_code") or "").strip().upper() or None
    if setup_code is not None and not re.fullmatch(r"[A-Z][A-Z0-9_-]{0,11}", setup_code):
        raise ValueError("计划形态代码格式不正确")
    return {
        "index": index,
        "client_id": client_id,
        "source_ref": source_ref or None,
        "symbol": symbol,
        "traded_on": str(payload.get("traded_on") or ""),
        "traded_at": values["traded_at"],
        "side": values["side"],
        "quantity": quantity,
        "price": price,
        "fee": fee,
        "method": values["method"],
        "setup_method": values["setup_method"],
        "explicit_setup_method": explicit_setup_method,
        "stop_price": values["stop_price"],
        "plan_version_id": str(payload.get("plan_version_id") or "").strip() or None,
        "setup_code": setup_code,
        "plan_setup_id": str(payload.get("plan_setup_id") or OTHER_SETUP_ID).strip().upper(),
        "other_setup_note": str(payload.get("other_setup_note") or "").strip()[:300],
        "rationale": str(payload.get("rationale") or "").strip(),
        "invalidation": str(payload.get("invalidation") or "").strip(),
        "exit_reason": str(payload.get("exit_reason") or "").strip(),
        "market_context": str(payload.get("market_context") or "").strip(),
        "observation_snapshot": dict(snapshot),
    }


def _review_value(row: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(row)
    raw_tags = value.pop("tags_json", "[]")
    try:
        tags = json.loads(str(raw_tags or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        tags = []
    value["tags"] = (
        [str(tag) for tag in tags if isinstance(tag, str)] if isinstance(tags, list) else []
    )
    raw = value.pop("c1_record_json", "{}")
    try:
        record = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        record = {}
    if not isinstance(record, dict):
        record = {}
    if record:
        record["structure_state_label"] = C1_STRUCTURE_STATE_LABELS.get(
            str(record.get("structure_state") or ""), "未知状态"
        )
        record["entry_variant_label"] = C1_ENTRY_VARIANT_LABELS.get(
            str(record.get("entry_variant") or ""), "未指定"
        )
        try:
            height = float(record["upper_bound"]) - float(record["lower_bound"])
            atr14 = float(record["atr14"])
            record["range_height_atr"] = round(height / atr14, 2) if atr14 > 0 else None
        except (KeyError, TypeError, ValueError):
            record["range_height_atr"] = None
    value["c1_record"] = record
    return value


def normalize_observation_tags(tags: str | Sequence[str]) -> list[str]:
    values = [tags] if isinstance(tags, str) else list(tags)
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        for candidate in str(value).replace("，", ",").split(","):
            tag = candidate.strip().lstrip("#").strip()
            if not tag:
                continue
            if len(tag) > OBSERVATION_TAG_MAX_LENGTH:
                raise ValueError(f"单个观察标签不能超过 {OBSERVATION_TAG_MAX_LENGTH} 个字符")
            key = tag.casefold()
            if key in seen:
                continue
            seen.add(key)
            normalized.append(tag)
    if len(normalized) > OBSERVATION_TAG_MAX_COUNT:
        raise ValueError(f"每只股票最多保存 {OBSERVATION_TAG_MAX_COUNT} 个观察标签")
    return normalized


def _validated_c1_record(payload: Mapping[str, Any]) -> dict[str, Any]:
    structure_state = str(payload.get("structure_state") or "").strip().upper()
    entry_variant = str(payload.get("entry_variant") or "").strip().upper()
    if structure_state not in C1_STRUCTURE_STATES:
        raise ValueError("C1结构状态不正确")
    if entry_variant not in C1_ENTRY_VARIANTS:
        raise ValueError("C1入场类型必须为首次或二次")
    as_of_date = _strict_iso_date(payload.get("as_of_date"), field="证据日期")
    range_started_on = _strict_iso_date(payload.get("range_started_on"), field="区间起始日")
    if range_started_on > as_of_date:
        raise ValueError("区间起始日不能晚于证据日期")
    lower_bound = _strict_positive_float(payload.get("lower_bound"), field="区间下沿")
    upper_bound = _strict_positive_float(payload.get("upper_bound"), field="区间上沿")
    atr14 = _strict_positive_float(payload.get("atr14"), field="ATR(14)")
    if upper_bound <= lower_bound:
        raise ValueError("区间上沿必须高于区间下沿")
    try:
        support_test_count = int(str(payload.get("support_test_count") or ""))
    except (TypeError, ValueError) as exc:
        raise ValueError("历史下沿测试次数必须为整数") from exc
    if support_test_count < 2:
        raise ValueError("C1至少需要两个历史下沿测试点")
    note = str(payload.get("note") or "").strip()
    if len(note) > 500:
        raise ValueError("C1结构说明不能超过500字")
    return {
        "as_of_date": as_of_date,
        "range_started_on": range_started_on,
        "lower_bound": round(lower_bound, 6),
        "upper_bound": round(upper_bound, 6),
        "atr14": round(atr14, 6),
        "support_test_count": support_test_count,
        "structure_state": structure_state,
        "entry_variant": entry_variant,
        "note": note,
    }


def _validated_setup_observation(payload: Mapping[str, Any]) -> dict[str, Any]:
    status = str(payload.get("status") or "WATCH").strip().upper()
    if status not in {"WATCH", "READY", "INVALID"}:
        raise ValueError("形态证据状态不正确")
    as_of_date = _strict_iso_date(payload.get("as_of_date"), field="证据日期")
    structure_started_on = str(payload.get("structure_started_on") or "").strip()
    if structure_started_on:
        structure_started_on = _strict_iso_date(structure_started_on, field="结构起始日")
        if structure_started_on > as_of_date:
            raise ValueError("结构起始日不能晚于证据日期")
    signal_on = str(payload.get("signal_on") or "").strip()
    if signal_on:
        signal_on = _strict_iso_date(signal_on, field="信号日")
        if signal_on > as_of_date:
            raise ValueError("信号日不能晚于证据日期")

    def optional_price(key: str, label: str) -> float | None:
        raw = str(payload.get(key) or "").strip()
        return round(_strict_positive_float(raw, field=label), 6) if raw else None

    reference_low = optional_price("reference_low", "参考下沿")
    reference_high = optional_price("reference_high", "参考上沿")
    if reference_low is not None and reference_high is not None and reference_high <= reference_low:
        raise ValueError("参考上沿必须高于参考下沿")
    summary = str(payload.get("summary") or "").strip()
    if not summary:
        raise ValueError("请填写形态证据摘要")
    if len(summary) > 1000:
        raise ValueError("形态证据摘要不能超过1000字")
    return {
        "status": status,
        "as_of_date": as_of_date,
        "structure_started_on": structure_started_on,
        "signal_on": signal_on,
        "reference_low": reference_low,
        "reference_high": reference_high,
        "trigger_price": optional_price("trigger_price", "计划触发价"),
        "invalidation_price": optional_price("invalidation_price", "候选失效价"),
        "summary": summary,
    }


def _strict_iso_date(value: Any, *, field: str) -> str:
    normalized = str(value or "").strip()
    try:
        return date.fromisoformat(normalized).isoformat()
    except ValueError as exc:
        raise ValueError(f"{field}格式不正确") from exc


def _strict_positive_float(value: Any, *, field: str) -> float:
    try:
        normalized = float(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field}格式不正确") from exc
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError(f"{field}必须大于0")
    return normalized


def _trade_identity(row: Mapping[str, Any]) -> tuple[Any, ...]:
    source_ref = str(row.get("source_ref") or "").strip()
    if source_ref:
        return ("source_ref", source_ref)
    return (
        "execution_fallback",
        str(row["symbol"]).upper(),
        str(row["traded_on"]),
        str(row.get("traded_at") or ""),
        str(row["side"]).upper(),
        int(row["quantity"]),
        round(float(row["price"]), 6),
    )


def _trade_fingerprint(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        str(row["symbol"]).upper(),
        str(row["traded_on"]),
        str(row.get("traded_at") or ""),
        str(row["side"]).upper(),
        int(row["quantity"]),
        round(float(row["price"]), 6),
        round(float(row.get("fee") or 0), 6),
    )


def _strict_positive_integer(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field}必须为正整数")
    if isinstance(value, int):
        normalized = value
    elif isinstance(value, str) and value.strip().isdigit():
        normalized = int(value.strip())
    else:
        raise ValueError(f"{field}必须为正整数")
    if normalized <= 0 or normalized > 9_223_372_036_854_775_807:
        raise ValueError(f"{field}必须为正整数")
    return normalized


def _normalize_username(username: str) -> str:
    normalized = username.strip().lower()
    if len(normalized) < 3 or len(normalized) > 64:
        raise ValueError("用户名长度必须为 3 至 64 个字符")
    if not all(
        character.isalnum() or character in {"@", ".", "_", "-"} for character in normalized
    ):
        raise ValueError("用户名只能包含字母、数字及 @ . _ -")
    return normalized


def _access_log_cutoff(retention_days: int, as_of: date | None) -> str:
    if retention_days < 1:
        raise ValueError("访问日志保留天数必须大于 0")
    effective_date = as_of or datetime.now(timezone.utc).date()
    return (effective_date - timedelta(days=retention_days)).isoformat()


def _access_path_group(path: str | None) -> str:
    normalized = str(path or "")
    if normalized in {"/login", "/logout"}:
        return normalized
    for root, group in (
        ("/api/v1/trades", "/api/v1/trades"),
        ("/api/v1/plans", "/api/v1/plans"),
        ("/api/v1", "/api/v1/*"),
        ("/account", "/account/*"),
        ("/a", "/a/*"),
        ("/stock", "/stock/*"),
        ("/reading", "/reading/*"),
    ):
        if normalized == root or normalized.startswith(f"{root}/"):
            return group
    return "OTHER"


def _validate_password(password: str) -> None:
    if len(password) < 10 or len(password) > 256:
        raise ValueError("密码长度必须为 10 至 256 个字符")
    classes = (
        any(character.islower() for character in password),
        any(character.isupper() for character in password),
        any(character.isdigit() for character in password),
        any(not character.isalnum() for character in password),
    )
    if sum(classes) < 3:
        raise ValueError("密码必须包含大小写字母、数字和特殊字符中的至少三类")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _validated_trade_values(
    *,
    traded_on: str,
    traded_at: str,
    side: str,
    quantity: int,
    price: float,
    fee: float,
    method: str,
    setup_method: str,
    stop_price: float | None,
) -> dict[str, Any]:
    try:
        date.fromisoformat(traded_on)
        if traded_at.strip():
            datetime.strptime(traded_at.strip(), "%H:%M:%S")
    except ValueError as exc:
        raise ValueError("成交日期或时间格式不正确") from exc
    normalized_side = side.upper().strip()
    normalized_method = method.upper().strip()
    normalized_setup = setup_method.upper().strip()
    normalized_setup = TRADE_SETUP_ALIASES.get(normalized_setup, normalized_setup)
    if normalized_side not in {"BUY", "SELL"} or normalized_method not in TRADE_METHOD_LABELS:
        raise ValueError("方向或关联方法不正确")
    if normalized_setup not in TRADE_SETUP_LABELS:
        raise ValueError("交易方案不受支持")
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
        raise ValueError("数量必须为正整数")
    if not math.isfinite(price) or not math.isfinite(fee):
        raise ValueError("价格和费用必须为有限数值")
    if price <= 0 or fee < 0:
        raise ValueError("数量、价格和费用必须有效")
    normalized_stop = float(stop_price) if stop_price is not None else None
    if normalized_stop is not None and not math.isfinite(normalized_stop):
        raise ValueError("止损价必须为有限数值")
    if normalized_stop is not None and (normalized_stop <= 0 or normalized_stop >= price):
        raise ValueError("止损价必须大于 0 且低于买入成交价")
    if normalized_side == "SELL":
        normalized_stop = None
    return {
        "traded_at": traded_at.strip(),
        "side": normalized_side,
        "method": normalized_method,
        "setup_method": normalized_setup,
        "stop_price": normalized_stop,
    }


def _normalize_drawing(
    tool: str,
    price_scale_id: str,
    anchors: Sequence[Mapping[str, Any]],
    content_text: str = "",
    interval_scope: str = "both",
) -> tuple[list[dict[str, float | str]], str, str]:
    if tool not in {"trendline", "horizontal", "text"}:
        raise ValueError("unsupported drawing tool")
    if not price_scale_id:
        raise ValueError("missing price scale identifier")
    expected_count = 2 if tool == "trendline" else 1
    if len(anchors) != expected_count:
        raise ValueError(f"{tool} requires {expected_count} anchors")
    if not isinstance(content_text, str):
        raise ValueError("文字标记内容必须是文本")
    normalized_content = content_text.strip()
    if tool == "text" and not normalized_content:
        raise ValueError("文字标记内容不能为空")
    if len(normalized_content) > 200:
        raise ValueError("文字标记内容不能超过 200 个字符")
    if tool != "text" and normalized_content:
        raise ValueError("画线不支持文字内容")
    normalized_interval = str(interval_scope or "both").strip().lower()
    if normalized_interval not in {"day", "week", "both"}:
        raise ValueError("图表周期范围不受支持")
    if tool == "text" and normalized_interval == "both":
        raise ValueError("文字标记必须属于日线或周线")
    normalized: list[dict[str, float | str]] = []
    for anchor in anchors:
        anchor_date = str(anchor.get("date") or "")
        logical = anchor.get("logical")
        logical_from_end = anchor.get("logical_from_end")
        logical_from_start = anchor.get("logical_from_start")
        price = anchor.get("price")
        if not isinstance(price, (int, float)) or float(price) <= 0:
            raise ValueError("invalid drawing anchor")
        if len(anchor_date) == 10:
            normalized.append({"date": anchor_date, "price": round(float(price), 6)})
        elif isinstance(logical_from_end, (int, float)) and float(logical_from_end) > 0:
            normalized.append(
                {
                    "logical_from_end": round(float(logical_from_end), 4),
                    "price": round(float(price), 6),
                }
            )
        elif isinstance(logical_from_start, (int, float)) and float(logical_from_start) < 0:
            normalized.append(
                {
                    "logical_from_start": round(float(logical_from_start), 4),
                    "price": round(float(price), 6),
                }
            )
        elif isinstance(logical, (int, float)):
            normalized.append(
                {"logical": round(float(logical), 4), "price": round(float(price), 6)}
            )
        else:
            raise ValueError("invalid drawing anchor")
    return normalized, normalized_content, normalized_interval


if TYPE_CHECKING:
    from .reading_notes import ReadingNoteService
    from .trading_plans import TradingPlanService
