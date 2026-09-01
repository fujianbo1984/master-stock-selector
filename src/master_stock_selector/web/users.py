from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from time import time
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from ..watchlist.repository import (
    DEFAULT_TRADE_SETUP,
    TRADE_METHOD_LABELS,
    TRADE_SETUP_ALIASES,
    TRADE_SETUP_LABELS,
    _aggregate_completed_trades,
    _match_trade_executions,
    _open_quantity,
    _summarize_open_lots,
    _trade_statistics,
)
from .plan_setups import (
    PLAN_SETUP_BY_TRADE_SETUP,
    PLAN_SETUP_CODES,
    TRADE_SETUP_BY_PLAN_SETUP,
    plan_setup_choices_text,
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
USER_SCHEMA_VERSION = 12
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
                if str(item.get("setup_id") or "").upper() == requested
                and bool(item.get("enabled"))
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
        self.path = Path(path)
        self.password_hasher = PasswordHasher()
        self._trading_plans: TradingPlanService | None = None
        self._reading_notes: ReadingNoteService | None = None

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
        if read_only:
            connection = sqlite3.connect(f"file:{self.path.resolve()}?mode=ro", uri=True, timeout=5)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        if not read_only:
            connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def initialize(self) -> None:
        """Create or migrate a user database explicitly (tests/provisioning only)."""
        self.migrate_schema()

    def schema_status(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {
                "status": "MISSING",
                "compatible": False,
                "schema_version": None,
                "required_version": USER_SCHEMA_VERSION,
                "missing_tables": sorted(USER_REQUIRED_TABLES),
                "missing_columns": [
                    "user_trade_execution.source_ref",
                    "user_trade_execution.revision",
                    "user_trade_execution.plan_version_id",
                    "user_trade_execution.evaluation_cycle_id",
                    "user_trade_execution.setup_code",
                    "user_trade_execution.initial_stop_price",
                    "user_trade_execution.setup_kind",
                    "user_trade_execution.plan_setup_id",
                    "user_trade_execution.setup_code_snapshot",
                    "user_trade_execution.setup_name_snapshot",
                    "user_trade_execution.other_setup_note",
                    "user_chart_drawing.content_text",
                    "user_chart_drawing.interval_scope",
                    "user_plan_grill_session.published_version_id",
                    "user_plan_grill_session.head_version_id_at_start",
                    "user_reading_note.assets_json",
                    "user_reading_note_version.assets_json",
                    "user_stock_review.c1_record_json",
                    "user_stock_review.tags_json",
                ],
                "missing_indexes": sorted(USER_REQUIRED_INDEXES),
            }
        try:
            with self.connect(read_only=True) as connection:
                quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                trade_columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(user_trade_execution)")
                }
                grill_columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(user_plan_grill_session)")
                }
                drawing_columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(user_chart_drawing)")
                }
                reading_note_columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(user_reading_note)")
                }
                reading_version_columns = {
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA table_info(user_reading_note_version)"
                    )
                }
                indexes = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='index'"
                    )
                }
                review_row = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='user_stock_review'"
                ).fetchone()
                review_table_sql = str(review_row[0] if review_row is not None else "")
                review_columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(user_stock_review)")
                }
                trade_row = connection.execute(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type='table' AND name='user_trade_execution'"
                ).fetchone()
                trade_table_sql = str(trade_row[0] if trade_row is not None else "")
                drawing_row = connection.execute(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type='table' AND name='user_chart_drawing'"
                ).fetchone()
                drawing_table_sql = str(drawing_row[0] if drawing_row is not None else "")
                schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        except sqlite3.Error as exc:
            return {
                "status": "ERROR",
                "compatible": False,
                "schema_version": None,
                "required_version": USER_SCHEMA_VERSION,
                "error": type(exc).__name__,
            }
        missing_tables = sorted(USER_REQUIRED_TABLES - tables)
        missing_columns = sorted(
            f"user_trade_execution.{column}"
            for column in {
                "source_ref",
                "revision",
                "plan_version_id",
                "evaluation_cycle_id",
                "setup_code",
                "initial_stop_price",
                "setup_kind",
                "plan_setup_id",
                "setup_code_snapshot",
                "setup_name_snapshot",
                "other_setup_note",
            }
            - trade_columns
        )
        if "published_version_id" not in grill_columns:
            missing_columns.append("user_plan_grill_session.published_version_id")
        if "head_version_id_at_start" not in grill_columns:
            missing_columns.append("user_plan_grill_session.head_version_id_at_start")
        if "user_stock_review" in tables and "c1_record_json" not in review_columns:
            missing_columns.append("user_stock_review.c1_record_json")
        if "user_stock_review" in tables and "tags_json" not in review_columns:
            missing_columns.append("user_stock_review.tags_json")
        for column in {"content_text", "interval_scope"} - drawing_columns:
            missing_columns.append(f"user_chart_drawing.{column}")
        if "user_reading_note" in tables and "assets_json" not in reading_note_columns:
            missing_columns.append("user_reading_note.assets_json")
        if (
            "user_reading_note_version" in tables
            and "assets_json" not in reading_version_columns
        ):
            missing_columns.append("user_reading_note_version.assets_json")
        missing_indexes = sorted(USER_REQUIRED_INDEXES - indexes)
        legacy_observation_states = (
            "'DROPPED'" in review_table_sql or "'UNREVIEWED'" in review_table_sql
        )
        legacy_trade_setups = bool(trade_table_sql) and "'FAILED_TEST'" not in trade_table_sql
        legacy_chart_drawings = bool(drawing_table_sql) and "'text'" not in drawing_table_sql
        compatible = (
            quick_check == "ok"
            and not missing_tables
            and not missing_columns
            and not missing_indexes
            and not legacy_observation_states
            and not legacy_trade_setups
            and not legacy_chart_drawings
            and schema_version == USER_SCHEMA_VERSION
        )
        return {
            "status": "READY" if compatible else "MIGRATION_REQUIRED",
            "compatible": compatible,
            "quick_check": quick_check,
            "schema_version": schema_version,
            "required_version": USER_SCHEMA_VERSION,
            "missing_tables": missing_tables,
            "missing_columns": missing_columns,
            "missing_indexes": missing_indexes,
            "legacy_observation_states": legacy_observation_states,
            "legacy_trade_setups": legacy_trade_setups,
            "legacy_chart_drawings": legacy_chart_drawings,
        }

    def require_schema(self) -> None:
        status = self.schema_status()
        if not status["compatible"]:
            raise RuntimeError(
                "用户数据库结构不兼容；Web 启动不会自动迁移。"
                "请先运行 scripts/manage_users.sh schema-check，"
                "确认后显式执行 schema-migrate --apply。"
                f" 当前状态：{status['status']}"
            )

    def migrate_schema(self) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA secure_delete=ON")
            trade_table_exists = (
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='user_trade_execution'"
                ).fetchone()
                is not None
            )
            trade_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(user_trade_execution)")
            }
            trade_row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='user_trade_execution'"
            ).fetchone()
            trade_table_sql = str(trade_row[0] if trade_row is not None else "")
            review_row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='user_stock_review'"
            ).fetchone()
            review_table_sql = str(review_row[0] if review_row is not None else "")
            review_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(user_stock_review)")
            }
            drawing_row = connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='user_chart_drawing'"
            ).fetchone()
            drawing_table_sql = str(drawing_row[0] if drawing_row is not None else "")
            reading_note_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(user_reading_note)")
            }
            reading_version_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(user_reading_note_version)")
            }
            migration_statements: list[str] = []
            if review_columns and "c1_record_json" not in review_columns:
                migration_statements.append(
                    "ALTER TABLE user_stock_review "
                    "ADD COLUMN c1_record_json TEXT NOT NULL DEFAULT '{}';"
                )
            if review_columns and "tags_json" not in review_columns:
                migration_statements.append(
                    "ALTER TABLE user_stock_review "
                    "ADD COLUMN tags_json TEXT NOT NULL DEFAULT '[]';"
                )
            if reading_note_columns and "assets_json" not in reading_note_columns:
                migration_statements.append(
                    "ALTER TABLE user_reading_note "
                    "ADD COLUMN assets_json TEXT NOT NULL DEFAULT '{}';"
                )
            if reading_version_columns and "assets_json" not in reading_version_columns:
                migration_statements.append(
                    "ALTER TABLE user_reading_note_version "
                    "ADD COLUMN assets_json TEXT NOT NULL DEFAULT '{}';"
                )
            if trade_table_exists and "source_ref" not in trade_columns:
                migration_statements.append(
                    "ALTER TABLE user_trade_execution ADD COLUMN source_ref TEXT;"
                )
            if trade_table_exists and "revision" not in trade_columns:
                migration_statements.append(
                    "ALTER TABLE user_trade_execution "
                    "ADD COLUMN revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0);"
                )
            for column, definition in (
                ("plan_version_id", "TEXT"),
                ("evaluation_cycle_id", "TEXT"),
                ("setup_code", "TEXT"),
                (
                    "setup_kind",
                    "TEXT NOT NULL DEFAULT 'LEGACY' "
                    "CHECK (setup_kind IN ('PLAN', 'OTHER', 'LEGACY'))",
                ),
                ("plan_setup_id", "TEXT"),
                ("setup_code_snapshot", "TEXT NOT NULL DEFAULT ''"),
                ("setup_name_snapshot", "TEXT NOT NULL DEFAULT ''"),
                ("other_setup_note", "TEXT NOT NULL DEFAULT ''"),
            ):
                if trade_table_exists and column not in trade_columns:
                    migration_statements.append(
                        f"ALTER TABLE user_trade_execution ADD COLUMN {column} {definition};"
                    )
            if trade_table_exists and "setup_kind" not in trade_columns:
                migration_statements.extend(
                    [
                        """UPDATE user_trade_execution
                        SET setup_kind=CASE
                                WHEN side='BUY' AND plan_version_id IS NOT NULL
                                     AND setup_code IN ('A1','A3','B1','B2','C1') THEN 'PLAN'
                                WHEN side='BUY' THEN 'LEGACY'
                                ELSE 'OTHER'
                            END,
                            plan_setup_id=CASE
                                WHEN side='BUY' AND plan_version_id IS NOT NULL
                                     AND setup_code IN ('A1','A3','B1','B2','C1') THEN setup_code
                                ELSE NULL
                            END,
                            setup_code_snapshot=CASE
                                WHEN side='BUY' THEN COALESCE(setup_code, '') ELSE '' END,
                            setup_name_snapshot=CASE
                                WHEN side='BUY' AND plan_version_id IS NOT NULL THEN
                                    CASE setup_code
                                        WHEN 'A1' THEN '突破前入场'
                                        WHEN 'A3' THEN '首次回调'
                                        WHEN 'B1' THEN '简单回调'
                                        WHEN 'B2' THEN '复杂回调'
                                        WHEN 'C1' THEN '区间失败测试'
                                        ELSE setup_method
                                    END
                                ELSE CASE setup_method
                                    WHEN 'FAILED_TEST' THEN '失败测试'
                                    WHEN 'PULLBACK_SUPPORT' THEN '简单回调'
                                    WHEN 'LOWER_TIMEFRAME_BREAKOUT' THEN '低周期突破入场'
                                    WHEN 'COMPLEX_PULLBACK' THEN '复杂回调'
                                    WHEN 'ANTI' THEN 'Anti（趋势转换首次回调）'
                                    WHEN 'PRE_BREAKOUT_BASE' THEN '突破前基底入场'
                                    WHEN 'POST_BREAKOUT_FIRST_PULLBACK' THEN '突破后回调'
                                    WHEN 'FAILED_BREAKOUT' THEN '失败突破'
                                    ELSE setup_method
                                END
                            END;""",
                    ]
                )
            if trade_table_exists and "initial_stop_price" not in trade_columns:
                migration_statements.extend(
                    [
                        "ALTER TABLE user_trade_execution ADD COLUMN initial_stop_price REAL;",
                        "UPDATE user_trade_execution SET initial_stop_price=stop_price WHERE side='BUY';",
                    ]
                )
            session_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(user_plan_grill_session)")
            }
            if session_columns and "published_version_id" not in session_columns:
                migration_statements.append(
                    "ALTER TABLE user_plan_grill_session ADD COLUMN published_version_id TEXT;"
                )
            if session_columns and "head_version_id_at_start" not in session_columns:
                migration_statements.append(
                    "ALTER TABLE user_plan_grill_session ADD COLUMN head_version_id_at_start TEXT;"
                )
            if drawing_table_sql and "'text'" not in drawing_table_sql:
                migration_statements.extend(
                    [
                        "DROP INDEX IF EXISTS idx_user_chart_symbol;",
                        "ALTER TABLE user_chart_drawing RENAME TO user_chart_drawing_legacy;",
                        """CREATE TABLE user_chart_drawing (
                            drawing_id TEXT PRIMARY KEY,
                            user_id TEXT NOT NULL,
                            symbol TEXT NOT NULL,
                            price_scale_id TEXT NOT NULL,
                            tool TEXT NOT NULL CHECK (
                                tool IN ('trendline', 'horizontal', 'text')
                            ),
                            anchors_json TEXT NOT NULL,
                            content_text TEXT NOT NULL DEFAULT '',
                            interval_scope TEXT NOT NULL DEFAULT 'both'
                                CHECK (interval_scope IN ('day', 'week', 'both')),
                            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            FOREIGN KEY (user_id) REFERENCES user_account(user_id)
                                ON DELETE CASCADE
                        );""",
                        """INSERT INTO user_chart_drawing(
                            drawing_id, user_id, symbol, price_scale_id, tool,
                            anchors_json, content_text, interval_scope, created_at, updated_at
                        )
                        SELECT drawing_id, user_id, symbol, price_scale_id, tool,
                               anchors_json, '', 'both', created_at, updated_at
                        FROM user_chart_drawing_legacy;""",
                        "DROP TABLE user_chart_drawing_legacy;",
                    ]
                )
            if "'DROPPED'" in review_table_sql or "'UNREVIEWED'" in review_table_sql:
                migration_statements.extend(
                    [
                        "ALTER TABLE user_stock_review RENAME TO user_stock_review_legacy;",
                        """CREATE TABLE user_stock_review (
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
                            FOREIGN KEY (user_id) REFERENCES user_account(user_id)
                                ON DELETE CASCADE
                        );""",
                        """INSERT INTO user_stock_review(
                            user_id, symbol, manual_state, note, tags_json,
                            c1_record_json, reviewed_at
                        )
                        SELECT user_id, symbol,
                               CASE manual_state
                                   WHEN 'DROPPED' THEN 'ARCHIVED'
                                   ELSE manual_state
                               END,
                               note, tags_json, c1_record_json, reviewed_at
                        FROM user_stock_review_legacy
                        WHERE manual_state IN ('WATCH', 'FOCUS', 'DROPPED');""",
                        "DROP TABLE user_stock_review_legacy;",
                    ]
                )
            if trade_table_exists and "'FAILED_TEST'" not in trade_table_sql:
                migration_statements.extend(
                    [
                        "ALTER TABLE user_trade_execution RENAME TO user_trade_execution_legacy;",
                        """CREATE TABLE user_trade_execution (
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
                            method TEXT NOT NULL CHECK (
                                method IN ('WEINSTEIN', 'MINERVINI', 'MANUAL')
                            ),
                            setup_method TEXT NOT NULL DEFAULT 'PULLBACK_SUPPORT'
                                CHECK (setup_method IN (
                                    'FAILED_TEST', 'PULLBACK_SUPPORT',
                                    'LOWER_TIMEFRAME_BREAKOUT', 'COMPLEX_PULLBACK',
                                    'ANTI', 'PRE_BREAKOUT_BASE',
                                    'POST_BREAKOUT_FIRST_PULLBACK', 'FAILED_BREAKOUT'
                                )),
                            stop_price REAL CHECK (stop_price IS NULL OR stop_price > 0),
                            initial_stop_price REAL CHECK (
                                initial_stop_price IS NULL OR initial_stop_price > 0
                            ),
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
                            FOREIGN KEY (user_id) REFERENCES user_account(user_id)
                                ON DELETE CASCADE
                        );""",
                        """INSERT INTO user_trade_execution(
                            execution_id, user_id, traded_on, traded_at, source_ref,
                            symbol, side, quantity, price, fee, method, setup_method,
                            stop_price, initial_stop_price, rationale, invalidation, exit_reason,
                            market_context, observation_snapshot_json, created_at,
                            updated_at, revision, setup_kind, plan_setup_id,
                            setup_code_snapshot, setup_name_snapshot, other_setup_note
                        )
                        SELECT execution_id, user_id, traded_on, traded_at, source_ref,
                               symbol, side, quantity, price, fee, method,
                               CASE setup_method
                                   WHEN 'BREAKOUT' THEN 'POST_BREAKOUT_FIRST_PULLBACK'
                                   WHEN 'PULLBACK' THEN 'PULLBACK_SUPPORT'
                                   ELSE setup_method
                               END,
                               stop_price,
                               CASE WHEN side='BUY' THEN stop_price ELSE NULL END,
                               rationale, invalidation, exit_reason,
                               market_context, observation_snapshot_json, created_at,
                               updated_at, revision,
                               CASE
                                   WHEN side='BUY' AND plan_version_id IS NOT NULL
                                        AND setup_code IN ('A1','A3','B1','B2','C1') THEN 'PLAN'
                                   WHEN side='BUY' THEN 'LEGACY'
                                   ELSE 'OTHER'
                               END,
                               CASE
                                   WHEN side='BUY' AND plan_version_id IS NOT NULL
                                        AND setup_code IN ('A1','A3','B1','B2','C1') THEN setup_code
                                   ELSE NULL
                               END,
                               CASE WHEN side='BUY' THEN COALESCE(setup_code, '') ELSE '' END,
                               CASE setup_method
                                   WHEN 'BREAKOUT' THEN '突破后回调'
                                   WHEN 'PULLBACK' THEN '简单回调'
                                   ELSE setup_method
                               END,
                               ''
                        FROM user_trade_execution_legacy;""",
                        "DROP TABLE user_trade_execution_legacy;",
                    ]
                )
            migration_sql = "\n".join(
                [
                    "BEGIN IMMEDIATE;",
                    USER_SCHEMA_SQL,
                    *migration_statements,
                    USER_SCHEMA_SQL,
                    "DROP INDEX IF EXISTS idx_user_access_user_occurred;",
                    "DROP INDEX IF EXISTS idx_user_access_path_occurred;",
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_user_trade_source_ref "
                    "ON user_trade_execution(user_id, source_ref) "
                    "WHERE source_ref IS NOT NULL AND source_ref <> '';",
                    f"PRAGMA user_version={USER_SCHEMA_VERSION};",
                    "COMMIT;",
                ]
            )
            try:
                connection.executescript(migration_sql)
            except Exception:
                connection.rollback()
                raise
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        status = self.schema_status()
        if not status["compatible"]:
            raise RuntimeError(f"用户数据库迁移后仍不兼容：{status}")
        return status

    @staticmethod
    def _migrate_observation_states(connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='user_stock_review'"
        ).fetchone()
        table_sql = str(row["sql"] if row is not None else "")
        if "'DROPPED'" not in table_sql and "'UNREVIEWED'" not in table_sql:
            return
        columns = {
            str(column[1])
            for column in connection.execute("PRAGMA table_info(user_stock_review)")
        }
        c1_expression = "c1_record_json" if "c1_record_json" in columns else "'{}'"
        tags_expression = "tags_json" if "tags_json" in columns else "'[]'"
        connection.execute("ALTER TABLE user_stock_review RENAME TO user_stock_review_legacy")
        connection.execute(
            """CREATE TABLE user_stock_review (
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
            )"""
        )
        connection.execute(
            f"""INSERT INTO user_stock_review(
                user_id, symbol, manual_state, note, tags_json, c1_record_json, reviewed_at
            )
            SELECT user_id, symbol,
                   CASE manual_state WHEN 'DROPPED' THEN 'ARCHIVED' ELSE manual_state END,
                   note, {tags_expression}, {c1_expression}, reviewed_at
            FROM user_stock_review_legacy
            WHERE manual_state IN ('WATCH', 'FOCUS', 'DROPPED')
            """
        )
        connection.execute("DROP TABLE user_stock_review_legacy")

    def create_user(self, username: str, password: str, display_name: str = "") -> str:
        normalized = _normalize_username(username)
        _validate_password(password)
        user_id = uuid4().hex
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO user_account(
                    user_id, username, password_hash, display_name, status
                ) VALUES (?, ?, ?, ?, 'ACTIVE')
                """,
                (user_id, normalized, self.password_hasher.hash(password), display_name.strip()),
            )
        return user_id

    def list_users(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT user_id, username, display_name, status, created_at, last_login_at
                FROM user_account ORDER BY created_at, username
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def active_user_by_username(self, username: str) -> dict[str, Any] | None:
        """Resolve one active account without exposing password or session data."""
        with self.connect(read_only=True) as connection:
            row = connection.execute(
                """
                SELECT user_id, username, display_name
                FROM user_account
                WHERE username=? COLLATE NOCASE AND status='ACTIVE'
                """,
                (_normalize_username(username),),
            ).fetchone()
        return dict(row) if row is not None else None

    def readiness(self) -> dict[str, Any]:
        try:
            with self.connect(read_only=True) as connection:
                quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            return {
                "connected": quick_check == "ok",
                "quick_check": quick_check,
            }
        except sqlite3.Error as exc:
            return {"connected": False, "quick_check": "error", "error": type(exc).__name__}

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
        """Append a minimal page/API access event without query or browser metadata."""
        access_id = uuid4().hex
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO user_access_log(
                    access_id, user_id, client_ip, country_code, country_name,
                    region_name, city_name, location_source, method, path, status_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    access_id,
                    user_id,
                    client_ip[:45],
                    country_code[:8],
                    country_name[:80],
                    region_name[:80],
                    city_name[:80],
                    location_source[:32],
                    method[:12].upper(),
                    path[:500],
                    int(status_code),
                ),
            )
        return access_id

    def access_log_maintenance_status(
        self,
        *,
        retention_days: int = DEFAULT_ACCESS_LOG_RETENTION_DAYS,
        as_of: date | None = None,
    ) -> dict[str, Any]:
        """Preview raw access rows eligible for anonymous rollup and deletion."""
        cutoff = _access_log_cutoff(retention_days, as_of)
        with self.connect(read_only=True) as connection:
            row = connection.execute(
                """SELECT COUNT(*), COUNT(DISTINCT substr(occurred_at, 1, 10))
                FROM user_access_log
                WHERE occurred_at < ?""",
                (cutoff,),
            ).fetchone()
        return {
            "status": "dry-run",
            "retention_days": retention_days,
            "cutoff_date": cutoff,
            "eligible_rows": int(row[0]),
            "eligible_dates": int(row[1]),
        }

    def maintain_access_log(
        self,
        *,
        retention_days: int = DEFAULT_ACCESS_LOG_RETENTION_DAYS,
        as_of: date | None = None,
    ) -> dict[str, Any]:
        """Roll up and delete expired raw access rows, one UTC date per transaction."""
        cutoff = _access_log_cutoff(retention_days, as_of)
        rows_deleted = 0
        summary_rows = 0
        processed_dates: list[str] = []
        with self.connect() as connection:
            connection.execute("PRAGMA secure_delete=ON")
            connection.create_function(
                "access_path_group", 1, _access_path_group, deterministic=True
            )
            dates = [
                str(row[0])
                for row in connection.execute(
                    """SELECT DISTINCT substr(occurred_at, 1, 10)
                    FROM user_access_log
                    WHERE occurred_at < ?
                    ORDER BY 1""",
                    (cutoff,),
                )
            ]
            for summary_date in dates:
                next_date = (date.fromisoformat(summary_date) + timedelta(days=1)).isoformat()
                connection.execute("BEGIN IMMEDIATE")
                try:
                    connection.execute(
                        "DELETE FROM user_access_daily_summary WHERE summary_date=?",
                        (summary_date,),
                    )
                    connection.execute(
                        """INSERT INTO user_access_daily_summary(
                            summary_date, country_code, region_name, path_group,
                            method, status_class, request_count, unique_ip_count,
                            summarized_at
                        )
                        SELECT substr(occurred_at, 1, 10), country_code, region_name,
                               access_path_group(path), method,
                               CAST(status_code / 100 AS INTEGER) * 100,
                               COUNT(*), COUNT(DISTINCT client_ip), CURRENT_TIMESTAMP
                        FROM user_access_log
                        WHERE occurred_at >= ? AND occurred_at < ?
                        GROUP BY 1, 2, 3, 4, 5, 6""",
                        (summary_date, next_date),
                    )
                    summary_rows += int(
                        connection.execute(
                            "SELECT COUNT(*) FROM user_access_daily_summary WHERE summary_date=?",
                            (summary_date,),
                        ).fetchone()[0]
                    )
                    cursor = connection.execute(
                        "DELETE FROM user_access_log WHERE occurred_at >= ? AND occurred_at < ?",
                        (summary_date, next_date),
                    )
                    rows_deleted += int(cursor.rowcount)
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
                processed_dates.append(summary_date)
            connection.execute("PRAGMA optimize")
            quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        return {
            "status": "maintained",
            "retention_days": retention_days,
            "cutoff_date": cutoff,
            "processed_dates": processed_dates,
            "rows_deleted": rows_deleted,
            "summary_rows": summary_rows,
            "quick_check": quick_check,
        }

    def set_user_status(self, username: str, status: str) -> None:
        normalized_status = status.upper()
        if normalized_status not in {"ACTIVE", "DISABLED"}:
            raise ValueError("用户状态必须为 ACTIVE 或 DISABLED")
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE user_account SET status=?, updated_at=CURRENT_TIMESTAMP
                WHERE username=? COLLATE NOCASE
                """,
                (normalized_status, _normalize_username(username)),
            )
            if cursor.rowcount != 1:
                raise ValueError("用户不存在")
            if normalized_status == "DISABLED":
                connection.execute(
                    """
                    UPDATE user_session SET revoked_at_epoch=?
                    WHERE user_id=(SELECT user_id FROM user_account
                                   WHERE username=? COLLATE NOCASE)
                      AND revoked_at_epoch IS NULL
                    """,
                    (int(time()), _normalize_username(username)),
                )
                connection.execute(
                    """
                    UPDATE user_api_token SET revoked_at_epoch=?
                    WHERE user_id=(SELECT user_id FROM user_account
                                   WHERE username=? COLLATE NOCASE)
                      AND revoked_at_epoch IS NULL
                    """,
                    (int(time()), _normalize_username(username)),
                )

    def reset_password(self, username: str, password: str) -> None:
        _validate_password(password)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT user_id FROM user_account WHERE username=? COLLATE NOCASE",
                (_normalize_username(username),),
            ).fetchone()
            if row is None:
                raise ValueError("用户不存在")
            connection.execute(
                """
                UPDATE user_account SET password_hash=?, updated_at=CURRENT_TIMESTAMP
                WHERE user_id=?
                """,
                (self.password_hasher.hash(password), str(row["user_id"])),
            )
            connection.execute(
                """
                UPDATE user_session SET revoked_at_epoch=?
                WHERE user_id=? AND revoked_at_epoch IS NULL
                """,
                (int(time()), str(row["user_id"])),
            )
            connection.execute(
                """
                UPDATE user_api_token SET revoked_at_epoch=?
                WHERE user_id=? AND revoked_at_epoch IS NULL
                """,
                (int(time()), str(row["user_id"])),
            )

    def change_password(self, user_id: str, current_password: str, new_password: str) -> bool:
        """Change a password and revoke every existing session and API token."""
        _validate_password(new_password)
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT password_hash FROM user_account
                WHERE user_id=? AND status='ACTIVE'
                """,
                (user_id,),
            ).fetchone()
            if row is None:
                return False
            try:
                verified = self.password_hasher.verify(str(row["password_hash"]), current_password)
            except (VerifyMismatchError, InvalidHashError):
                return False
            if not verified:
                return False
            if current_password == new_password:
                raise ValueError("新密码不能与当前密码相同")
            connection.execute(
                """
                UPDATE user_account SET password_hash=?, updated_at=CURRENT_TIMESTAMP
                WHERE user_id=?
                """,
                (self.password_hasher.hash(new_password), user_id),
            )
            connection.execute(
                """
                UPDATE user_session SET revoked_at_epoch=?
                WHERE user_id=? AND revoked_at_epoch IS NULL
                """,
                (int(time()), user_id),
            )
            connection.execute(
                """
                UPDATE user_api_token SET revoked_at_epoch=?
                WHERE user_id=? AND revoked_at_epoch IS NULL
                """,
                (int(time()), user_id),
            )
        return True

    def authenticate(self, username: str, password: str) -> dict[str, str] | None:
        normalized = _normalize_username(username)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM user_account WHERE username=? COLLATE NOCASE",
                (normalized,),
            ).fetchone()
            if row is None or str(row["status"]) != "ACTIVE":
                return None
            try:
                verified = self.password_hasher.verify(str(row["password_hash"]), password)
            except (VerifyMismatchError, InvalidHashError):
                return None
            if not verified:
                return None
            if self.password_hasher.check_needs_rehash(str(row["password_hash"])):
                connection.execute(
                    "UPDATE user_account SET password_hash=? WHERE user_id=?",
                    (self.password_hasher.hash(password), str(row["user_id"])),
                )
            connection.execute(
                "UPDATE user_account SET last_login_at=CURRENT_TIMESTAMP WHERE user_id=?",
                (str(row["user_id"]),),
            )
        return {
            "user_id": str(row["user_id"]),
            "username": str(row["username"]),
            "display_name": str(row["display_name"] or ""),
        }

    def create_session(self, user_id: str) -> tuple[str, str]:
        raw_token = secrets.token_urlsafe(32)
        csrf_secret = secrets.token_urlsafe(32)
        now = int(time())
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM user_session WHERE expires_at_epoch < ? OR revoked_at_epoch IS NOT NULL",
                (now,),
            )
            connection.execute(
                """
                INSERT INTO user_session(
                    session_id_hash, user_id, csrf_secret,
                    created_at_epoch, last_seen_epoch, expires_at_epoch
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    _token_hash(raw_token),
                    user_id,
                    csrf_secret,
                    now,
                    now,
                    now + SESSION_IDLE_SECONDS,
                ),
            )
        return raw_token, csrf_secret

    def session_user(self, raw_token: str | None) -> AuthenticatedUser | None:
        if not raw_token:
            return None
        now = int(time())
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT account.user_id, account.username, account.display_name,
                       session.csrf_secret, session.last_seen_epoch
                FROM user_session AS session
                JOIN user_account AS account ON account.user_id=session.user_id
                WHERE session.session_id_hash=? AND session.revoked_at_epoch IS NULL
                  AND session.expires_at_epoch>=? AND account.status='ACTIVE'
                """,
                (_token_hash(raw_token), now),
            ).fetchone()
            if row is None:
                return None
            if now - int(row["last_seen_epoch"]) >= 3600:
                connection.execute(
                    "UPDATE user_session SET last_seen_epoch=? WHERE session_id_hash=?",
                    (now, _token_hash(raw_token)),
                )
        return AuthenticatedUser(
            user_id=str(row["user_id"]),
            username=str(row["username"]),
            display_name=str(row["display_name"] or ""),
            csrf_token=str(row["csrf_secret"]),
        )

    def revoke_session(self, raw_token: str | None) -> None:
        if not raw_token:
            return
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE user_session SET revoked_at_epoch=?
                WHERE session_id_hash=? AND revoked_at_epoch IS NULL
                """,
                (int(time()), _token_hash(raw_token)),
            )

    def create_api_token(
        self,
        user_id: str,
        name: str,
        *,
        expires_days: int = API_TOKEN_DEFAULT_EXPIRES_DAYS,
        scopes: Sequence[str] = tuple(sorted(API_TOKEN_SCOPES)),
    ) -> tuple[str, dict[str, Any]]:
        normalized_name = name.strip()
        normalized_scopes = tuple(sorted({scope.strip() for scope in scopes if scope.strip()}))
        if not normalized_name or len(normalized_name) > 80:
            raise ValueError("Token 名称长度必须为 1 至 80 个字符")
        if expires_days < 1 or expires_days > API_TOKEN_MAX_EXPIRES_DAYS:
            raise ValueError(
                f"Token 有效期必须为 1 至 {API_TOKEN_MAX_EXPIRES_DAYS} 天"
            )
        if not normalized_scopes or not set(normalized_scopes).issubset(API_TOKEN_SCOPES):
            raise ValueError("Token 权限范围不正确")
        token_id = uuid4().hex
        raw_token = API_TOKEN_PREFIX + secrets.token_urlsafe(32)
        now = int(time())
        expires_at = now + expires_days * 24 * 60 * 60
        prefix = raw_token[:12]
        with self.connect() as connection:
            account = connection.execute(
                "SELECT status FROM user_account WHERE user_id=?", (user_id,)
            ).fetchone()
            if account is None or str(account["status"]) != "ACTIVE":
                raise ValueError("用户不存在或已停用")
            connection.execute(
                """
                INSERT INTO user_api_token(
                    token_id, user_id, name, token_prefix, token_hash, scopes_json,
                    created_at_epoch, expires_at_epoch
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    token_id,
                    user_id,
                    normalized_name,
                    prefix,
                    _token_hash(raw_token),
                    json.dumps(normalized_scopes, ensure_ascii=False),
                    now,
                    expires_at,
                ),
            )
        return raw_token, {
            "token_id": token_id,
            "name": normalized_name,
            "token_prefix": prefix,
            "scopes": list(normalized_scopes),
            "created_at_epoch": now,
            "expires_at_epoch": expires_at,
        }

    def list_api_tokens(self, user_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT token_id, name, token_prefix, scopes_json, created_at_epoch,
                       expires_at_epoch, last_used_epoch, revoked_at_epoch
                FROM user_api_token WHERE user_id=?
                ORDER BY created_at_epoch DESC, token_id
                """,
                (user_id,),
            ).fetchall()
        result: list[dict[str, Any]] = []
        now = int(time())
        for row in rows:
            item = dict(row)
            item["scopes"] = json.loads(str(item.pop("scopes_json")))
            item["active"] = (
                item["revoked_at_epoch"] is None and int(item["expires_at_epoch"]) >= now
            )
            result.append(item)
        return result

    def revoke_api_token(self, user_id: str, token_id: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE user_api_token SET revoked_at_epoch=?
                WHERE user_id=? AND token_id=? AND revoked_at_epoch IS NULL
                """,
                (int(time()), user_id, token_id),
            )
        return cursor.rowcount == 1

    def api_token_user(
        self, raw_token: str | None, required_scope: str | None = None
    ) -> ApiTokenPrincipal | None:
        if not raw_token or not raw_token.startswith(API_TOKEN_PREFIX):
            return None
        now = int(time())
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT token.token_id, token.user_id, token.scopes_json,
                       token.last_used_epoch, account.username, account.display_name
                FROM user_api_token AS token
                JOIN user_account AS account ON account.user_id=token.user_id
                WHERE token.token_hash=? AND token.revoked_at_epoch IS NULL
                  AND token.expires_at_epoch>=? AND account.status='ACTIVE'
                """,
                (_token_hash(raw_token), now),
            ).fetchone()
            if row is None:
                return None
            scopes = tuple(str(scope) for scope in json.loads(str(row["scopes_json"])))
            if required_scope is not None and required_scope not in scopes:
                return None
            last_used = row["last_used_epoch"]
            if last_used is None or now - int(last_used) >= 60:
                connection.execute(
                    "UPDATE user_api_token SET last_used_epoch=? WHERE token_id=?",
                    (now, str(row["token_id"])),
                )
        return ApiTokenPrincipal(
            user_id=str(row["user_id"]),
            username=str(row["username"]),
            display_name=str(row["display_name"] or ""),
            token_id=str(row["token_id"]),
            scopes=scopes,
        )

    def audit_api_action(
        self,
        principal: ApiTokenPrincipal,
        *,
        action: str,
        request_id: str,
        outcome: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        with self.connect() as connection:
            self._insert_api_audit(
                connection,
                principal,
                action=action,
                request_id=request_id,
                outcome=outcome,
                details=details,
            )

    @staticmethod
    def _insert_api_audit(
        connection: sqlite3.Connection,
        principal: ApiTokenPrincipal,
        *,
        action: str,
        request_id: str,
        outcome: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO user_api_audit(
                audit_id, user_id, token_id, action, request_id, outcome, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid4().hex,
                principal.user_id,
                principal.token_id,
                action,
                request_id,
                outcome,
                json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
            ),
        )

    @staticmethod
    def csrf_valid(user: AuthenticatedUser, supplied: str | None) -> bool:
        return bool(supplied) and hmac.compare_digest(user.csrf_token, str(supplied))

    def reviews_for_symbols(
        self, user_id: str, symbols: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        normalized = sorted({symbol.upper() for symbol in symbols if symbol})
        if not normalized:
            return {}
        placeholders = ",".join("?" for _ in normalized)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT symbol, manual_state, note, tags_json, c1_record_json, reviewed_at
                FROM user_stock_review
                WHERE user_id=? AND symbol IN ({placeholders})
                """,
                (user_id, *normalized),
            ).fetchall()
        return {str(row["symbol"]): _review_value(row) for row in rows}

    def reviews_for_user(self, user_id: str) -> list[dict[str, Any]]:
        """Return the user's stocks that currently belong to their observation workspace."""
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT symbol, manual_state, note, tags_json, c1_record_json, reviewed_at
                FROM user_stock_review
                WHERE user_id=?
                ORDER BY reviewed_at DESC, symbol
                """,
                (user_id,),
            ).fetchall()
        return [_review_value(row) for row in rows]

    def review(self, user_id: str, symbol: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT symbol, manual_state, note, tags_json, c1_record_json, reviewed_at
                FROM user_stock_review WHERE user_id=? AND symbol=?
                """,
                (user_id, symbol.upper()),
            ).fetchone()
        if row is not None:
            return _review_value(row)
        return {
            "symbol": symbol.upper(),
            "manual_state": "UNREVIEWED",
            "note": "",
            "tags": [],
            "c1_record": {},
            "reviewed_at": "",
        }

    def save_review(
        self,
        user_id: str,
        symbol: str,
        manual_state: str,
        note: str,
        tags: str | Sequence[str] | None = None,
    ) -> None:
        state = manual_state.upper()
        if state == "UNREVIEWED":
            with self.connect() as connection:
                connection.execute(
                    "DELETE FROM user_stock_review WHERE user_id=? AND symbol=?",
                    (user_id, symbol.upper()),
                )
            return
        if state not in {"WATCH", "FOCUS", "ARCHIVED"}:
            raise ValueError(f"unsupported manual state: {manual_state}")
        normalized_tags = normalize_observation_tags(tags) if tags is not None else None
        with self.connect() as connection:
            if normalized_tags is None:
                connection.execute(
                    """
                    INSERT INTO user_stock_review(
                        user_id, symbol, manual_state, note, reviewed_at
                    ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(user_id, symbol) DO UPDATE SET
                        manual_state=excluded.manual_state,
                        note=excluded.note,
                        reviewed_at=CURRENT_TIMESTAMP
                    """,
                    (user_id, symbol.upper(), state, note.strip()),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO user_stock_review(
                        user_id, symbol, manual_state, note, tags_json, reviewed_at
                    ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(user_id, symbol) DO UPDATE SET
                        manual_state=excluded.manual_state,
                        note=excluded.note,
                        tags_json=excluded.tags_json,
                        reviewed_at=CURRENT_TIMESTAMP
                    """,
                    (
                        user_id,
                        symbol.upper(),
                        state,
                        note.strip(),
                        json.dumps(normalized_tags, ensure_ascii=False),
                    ),
                )

    def save_c1_record(
        self, user_id: str, symbol: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        record = _validated_c1_record(payload)
        encoded = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO user_stock_review(
                    user_id, symbol, manual_state, note, c1_record_json, reviewed_at
                ) VALUES (?, ?, 'WATCH', '', ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, symbol) DO UPDATE SET
                    c1_record_json=excluded.c1_record_json,
                    reviewed_at=CURRENT_TIMESTAMP
                """,
                (user_id, symbol.upper(), encoded),
            )
        return record

    def clear_c1_record(self, user_id: str, symbol: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE user_stock_review
                SET c1_record_json='{}', reviewed_at=CURRENT_TIMESTAMP
                WHERE user_id=? AND symbol=?""",
                (user_id, symbol.upper()),
            )

    def trade_setup_options(
        self, user_id: str, *, plan_version_id: str | None = None
    ) -> list[dict[str, str]]:
        plan = (
            self.trading_plans.show(user_id, plan_version_id)
            if plan_version_id
            else self.trading_plans.current(user_id)
        )
        options = [{"value": OTHER_SETUP_ID, "label": OTHER_SETUP_LABEL}]
        if plan is not None:
            options.extend(
                {
                    "value": str(item["setup_id"]),
                    "label": f"{item['code']} · {item['name']}",
                }
                for item in plan.get("setup_catalog", [])
                if item.get("enabled")
            )
        return options

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
        compatibility_setup = setup_method or DEFAULT_TRADE_SETUP
        values = _validated_trade_values(
            traded_on=traded_on,
            traded_at=traded_at,
            side=side,
            quantity=quantity,
            price=price,
            fee=fee,
            method=method,
            setup_method=compatibility_setup,
            stop_price=stop_price,
        )
        normalized_symbol = symbol.upper().strip()
        if not normalized_symbol or "." not in normalized_symbol:
            raise ValueError("股票代码格式不正确")
        execution_id = uuid4().hex
        with self.connect() as connection:
            plan = (
                self.trading_plans.show(user_id, plan_version_id)
                if plan_version_id
                else self.trading_plans.current(user_id)
            )
            if values["side"] == "SELL":
                plan = None
            if plan_version_id and plan is None:
                raise ValueError("计划版本不存在或不属于当前用户")
            assignment = _setup_assignment(
                plan,
                plan_setup_id=plan_setup_id,
                setup_code=setup_code,
                setup_method=setup_method,
                other_setup_note=other_setup_note,
            )
            values["setup_method"] = assignment["setup_method"]
            frozen_snapshot = dict(observation_snapshot or {})
            if values["side"] == "BUY" and assignment["plan_setup_id"] == "C1":
                review_row = connection.execute(
                    """SELECT c1_record_json FROM user_stock_review
                    WHERE user_id=? AND symbol=?""",
                    (user_id, normalized_symbol),
                ).fetchone()
                if review_row is not None:
                    try:
                        c1_record = json.loads(str(review_row["c1_record_json"] or "{}"))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        c1_record = {}
                    if isinstance(c1_record, dict) and c1_record:
                        frozen_snapshot["c1_record"] = c1_record
            existing_rows = self._trade_rows(connection, user_id, normalized_symbol)
            candidate = {
                "execution_id": execution_id,
                "traded_on": traded_on,
                "traded_at": values["traded_at"],
                "symbol": normalized_symbol,
                "side": values["side"],
                "quantity": quantity,
                "price": price,
                "fee": fee,
                "method": values["method"],
                "setup_method": values["setup_method"],
                "stop_price": values["stop_price"],
            }
            if values["side"] == "SELL":
                try:
                    _match_trade_executions([*existing_rows, candidate])
                except ValueError as exc:
                    if not permit_unmatched_sell:
                        available = _open_quantity(existing_rows)
                        raise ValueError(f"卖出数量超过可复盘持仓：可用 {available} 股") from exc
            connection.execute(
                """
                INSERT INTO user_trade_execution(
                    execution_id, user_id, traded_on, traded_at, symbol, side,
                    quantity, price, fee, method, setup_method, stop_price,
                    initial_stop_price,
                    rationale, invalidation, exit_reason, market_context,
                    observation_snapshot_json, plan_version_id, evaluation_cycle_id, setup_code
                    , setup_kind, plan_setup_id, setup_code_snapshot,
                    setup_name_snapshot, other_setup_note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    execution_id,
                    user_id,
                    traded_on,
                    values["traded_at"],
                    normalized_symbol,
                    values["side"],
                    quantity,
                    price,
                    fee,
                    values["method"],
                    values["setup_method"],
                    values["stop_price"],
                    values["stop_price"] if values["side"] == "BUY" else None,
                    rationale.strip(),
                    invalidation.strip(),
                    exit_reason.strip(),
                    market_context.strip(),
                    json.dumps(frozen_snapshot, ensure_ascii=False, sort_keys=True),
                    plan["version_id"] if plan else None,
                    plan["evaluation_cycle_id"] if plan else None,
                    assignment["setup_code"] if values["side"] == "BUY" else None,
                    assignment["setup_kind"] if values["side"] == "BUY" else "OTHER",
                    assignment["plan_setup_id"] if values["side"] == "BUY" else None,
                    assignment["setup_code_snapshot"] if values["side"] == "BUY" else "",
                    assignment["setup_name_snapshot"] if values["side"] == "BUY" else "",
                    assignment["other_setup_note"] if values["side"] == "BUY" else "",
                ),
            )
        return execution_id

    def trade_review(self, user_id: str, names: Mapping[str, str]) -> dict[str, Any]:
        with self.connect() as connection:
            rows = self._trade_rows(connection, user_id)
            version_rows = connection.execute(
                """SELECT version.version_id, version.version_number
                FROM user_trading_plan_version AS version
                JOIN user_trading_plan AS plan ON plan.plan_id=version.plan_id
                WHERE plan.user_id=?""",
                (user_id,),
            ).fetchall()
        version_labels = {
            str(row["version_id"]): str(row["version_number"]) for row in version_rows
        }
        for row in rows:
            row["snapshot"] = json.loads(str(row.pop("observation_snapshot_json") or "{}"))
        closed, open_lots, unmatched_sells = _match_trade_executions(
            rows, permit_unmatched_sells=True
        )
        completed_lots = _aggregate_completed_trades(closed, open_lots)
        open_positions = _summarize_open_lots(open_lots)
        for collection in (rows, completed_lots, open_positions, unmatched_sells):
            for item in collection:
                symbol = str(item["symbol"])
                item["stock_name"] = names.get(symbol, "名称待补")
                _decorate_setup(item)
        by_plan_setup: dict[str, dict[str, Any]] = {}
        for item in completed_lots:
            if item.get("setup_kind") != "PLAN" or not item.get("plan_setup_id"):
                continue
            cycle_id = str(item.get("evaluation_cycle_id") or "UNBOUND")
            setup_id = str(item["plan_setup_id"])
            setup_code = item.get("setup_code_snapshot") or item.get("setup_code") or setup_id
            key = f"{cycle_id}:{setup_id}"
            by_plan_setup.setdefault(
                key,
                {
                    "evaluation_cycle_id": item.get("evaluation_cycle_id"),
                    "plan_version_ids": set(),
                    "setup_code": setup_code,
                    "setup_name": item.get("setup_name_snapshot") or setup_code,
                    "trades": [],
                },
            )
            group = by_plan_setup[key]
            if item.get("plan_version_id"):
                group["plan_version_ids"].add(str(item["plan_version_id"]))
            group["trades"].append(item)
        for value in by_plan_setup.values():
            version_ids = sorted(value.pop("plan_version_ids"))
            value["plan_version_ids"] = version_ids
            value["plan_versions"] = [version_labels.get(item, item) for item in version_ids]
            trades = value.pop("trades")
            value["summary"] = _trade_statistics(trades)
            value["by_entry_variant"] = {}
            if str(value.get("setup_code") or "") == "C1":
                for variant, label in C1_ENTRY_VARIANT_LABELS.items():
                    matching = [
                        item
                        for item in trades
                        if str(
                            (item.get("snapshot", {}).get("c1_record") or {}).get(
                                "entry_variant"
                            )
                            or ""
                        )
                        == variant
                    ]
                    if matching:
                        value["by_entry_variant"][variant] = {
                            "label": label,
                            "summary": _trade_statistics(matching),
                        }
            value["sample_state"] = (
                "样本不足：少于 30 个可计算R的完整买入批次。"
                if value["summary"].get("r_count", 0) < 30
                else "样本达到本计划的 30 个可计算R完整买入批次初步评价门槛。"
            )
        completed_lot_summary = _trade_statistics(completed_lots)
        realized_summary = {
            "net_pnl": sum(
                int(item.get("pnl_cents", round(float(item["pnl"]) * 100)))
                for item in closed
            )
            / 100,
            "matched_quantity": sum(int(item["quantity"]) for item in closed),
            "fifo_fragment_count": len(closed),
            "sell_execution_count": len(
                {str(item["sell_execution_id"]) for item in closed}
            ),
        }
        return {
            "executions": list(reversed(rows)),
            "completed_lots": list(reversed(completed_lots)),
            "closed_fragments": list(reversed(closed)),
            "open_positions": open_positions,
            "unmatched_sells": list(reversed(unmatched_sells)),
            "realized_summary": realized_summary,
            "completed_lot_summary": completed_lot_summary,
            "completed_lots_by_setup": {
                label: _trade_statistics(
                    [item for item in completed_lots if item["setup_label"] == label]
                )
                for label in dict.fromkeys(item["setup_label"] for item in completed_lots)
            },
            "other_count": sum(
                1 for item in completed_lots if item.get("setup_kind") == "OTHER"
            ),
            "legacy_count": sum(
                1 for item in completed_lots if item.get("setup_kind") == "LEGACY"
            ),
            "by_plan_setup": by_plan_setup,
            "completed_lot_sample_state": (
                "样本不足：少于 20 个完整买入批次，仅展示描述统计。"
                if len(completed_lots) < 20
                else "完整买入批次达到基础复盘门槛；仍不构成策略有效性或买卖建议。"
            ),
        }

    def trade_symbols(self, user_id: str) -> set[str]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT symbol FROM user_trade_execution WHERE user_id=?",
                (user_id,),
            ).fetchall()
        return {str(row["symbol"]) for row in rows}

    def trade_execution(self, user_id: str, execution_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM user_trade_execution
                WHERE user_id=? AND execution_id=?
                """,
                (user_id, execution_id),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["snapshot"] = json.loads(str(item.pop("observation_snapshot_json") or "{}"))
        _decorate_setup(item)
        return item

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
        values = _validated_trade_values(
            traded_on=traded_on,
            traded_at=traded_at,
            side=side,
            quantity=quantity,
            price=price,
            fee=fee,
            method=method,
            setup_method=setup_method or DEFAULT_TRADE_SETUP,
            stop_price=stop_price,
        )
        with self.connect() as connection:
            current = connection.execute(
                """
                SELECT * FROM user_trade_execution
                WHERE user_id=? AND execution_id=?
                """,
                (user_id, execution_id),
            ).fetchone()
            if current is None:
                raise ValueError("成交记录不存在")
            if values["side"] == "SELL":
                plan_version_id = None
                evaluation_cycle_id = None
                assignment = _setup_assignment(None)
            elif str(current["side"]) == "BUY":
                plan_version_id = current["plan_version_id"]
                evaluation_cycle_id = current["evaluation_cycle_id"]
                plan = (
                    self.trading_plans.show(user_id, str(plan_version_id))
                    if plan_version_id
                    else None
                )
                if plan_setup_id == "LEGACY" and str(current["setup_kind"]) == "LEGACY":
                    assignment = {
                        "setup_kind": "LEGACY",
                        "plan_setup_id": None,
                        "setup_code": current["setup_code"],
                        "setup_code_snapshot": current["setup_code_snapshot"],
                        "setup_name_snapshot": current["setup_name_snapshot"],
                        "other_setup_note": "",
                        "setup_method": current["setup_method"],
                    }
                else:
                    requested_setup = str(plan_setup_id or "").strip().upper()
                    if (
                        plan is None
                        and requested_setup not in {"", OTHER_SETUP_ID}
                    ) or (plan is None and setup_method):
                        plan = self.trading_plans.current(user_id)
                        plan_version_id = plan["version_id"] if plan else None
                        evaluation_cycle_id = plan["evaluation_cycle_id"] if plan else None
                    assignment = _setup_assignment(
                        plan,
                        plan_setup_id=plan_setup_id,
                        setup_method=setup_method,
                        other_setup_note=other_setup_note,
                    )
            else:
                plan = self.trading_plans.current(user_id)
                plan_version_id = plan["version_id"] if plan else None
                evaluation_cycle_id = plan["evaluation_cycle_id"] if plan else None
                assignment = _setup_assignment(
                    plan,
                    plan_setup_id=plan_setup_id,
                    setup_method=setup_method,
                    other_setup_note=other_setup_note,
                )
            values["setup_method"] = assignment["setup_method"]
            rows = self._trade_rows(connection, user_id)
            for row in rows:
                if str(row["execution_id"]) == execution_id:
                    row.update(
                        {
                            "traded_on": traded_on,
                            "traded_at": values["traded_at"],
                            "side": values["side"],
                            "quantity": quantity,
                            "price": price,
                            "fee": fee,
                            "method": values["method"],
                            "setup_method": values["setup_method"],
                            "stop_price": values["stop_price"],
                            "plan_version_id": plan_version_id,
                            "evaluation_cycle_id": evaluation_cycle_id,
                            "setup_code": assignment["setup_code"],
                            "setup_kind": assignment["setup_kind"],
                            "plan_setup_id": assignment["plan_setup_id"],
                            "setup_code_snapshot": assignment["setup_code_snapshot"],
                            "setup_name_snapshot": assignment["setup_name_snapshot"],
                            "other_setup_note": assignment["other_setup_note"],
                        }
                    )
                    break
            _match_trade_executions(rows)
            connection.execute(
                """
                UPDATE user_trade_execution SET
                    traded_on=?, traded_at=?, side=?, quantity=?, price=?, fee=?,
                    method=?, setup_method=?, stop_price=?, rationale=?, invalidation=?,
                    exit_reason=?, market_context=?, observation_snapshot_json=?,
                    plan_version_id=?, evaluation_cycle_id=?, setup_code=?,
                    setup_kind=?, plan_setup_id=?, setup_code_snapshot=?,
                    setup_name_snapshot=?, other_setup_note=?,
                    revision=revision + 1, updated_at=CURRENT_TIMESTAMP
                WHERE user_id=? AND execution_id=?
                """,
                (
                    traded_on,
                    values["traded_at"],
                    values["side"],
                    quantity,
                    price,
                    fee,
                    values["method"],
                    values["setup_method"],
                    values["stop_price"],
                    rationale.strip(),
                    invalidation.strip(),
                    exit_reason.strip(),
                    market_context.strip(),
                    json.dumps(observation_snapshot or {}, ensure_ascii=False, sort_keys=True),
                    plan_version_id,
                    evaluation_cycle_id,
                    assignment["setup_code"],
                    assignment["setup_kind"],
                    assignment["plan_setup_id"],
                    assignment["setup_code_snapshot"],
                    assignment["setup_name_snapshot"],
                    assignment["other_setup_note"],
                    user_id,
                    execution_id,
                ),
            )

    def trade_executions(
        self,
        user_id: str,
        *,
        symbol: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict[str, Any]]:
        normalized_symbol = symbol.upper().strip() if symbol else None
        with self.connect() as connection:
            rows = self._trade_rows(connection, user_id, normalized_symbol, date_to)
        if date_from:
            date.fromisoformat(date_from)
            rows = [row for row in rows if str(row["traded_on"]) >= date_from]
        if date_to:
            date.fromisoformat(date_to)
        for row in rows:
            row["snapshot"] = json.loads(str(row.pop("observation_snapshot_json") or "{}"))
            _decorate_setup(row)
        return list(reversed(rows))

    def validate_trade_batch(
        self, user_id: str, trades: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        with self.connect() as connection:
            report, _ = self._prepare_trade_batch(connection, user_id, trades)
        for item in report["results"]:
            if item["status"] == "READY":
                item.pop("execution_id", None)
        return report

    def record_trade_batch(
        self,
        user_id: str,
        trades: Sequence[Mapping[str, Any]],
        *,
        idempotency_key: str,
        principal: ApiTokenPrincipal,
        request_id: str,
    ) -> dict[str, Any]:
        if principal.user_id != user_id:
            raise ValueError("Token 与交易用户不匹配")
        key = idempotency_key.strip()
        if not key or len(key) > 128:
            raise ValueError("Idempotency-Key 长度必须为 1 至 128 个字符")
        request_payload = [{str(k): v for k, v in trade.items()} for trade in trades]
        request_hash = _token_hash(
            json.dumps(
                request_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                """
                SELECT request_hash, response_json FROM user_trade_batch
                WHERE user_id=? AND idempotency_key=?
                """,
                (user_id, key),
            ).fetchone()
            if previous is not None:
                if not hmac.compare_digest(str(previous["request_hash"]), request_hash):
                    raise ValueError("同一 Idempotency-Key 不能用于不同交易批次")
                response = json.loads(str(previous["response_json"]))
                response["replayed"] = True
                self._insert_api_audit(
                    connection,
                    principal,
                    action="trades.batch",
                    request_id=request_id,
                    outcome="COMMITTED",
                    details={
                        "batch_id": response["batch_id"],
                        "created": response["created"],
                        "duplicates": response["duplicates"],
                        "replayed": True,
                    },
                )
                return response
            report, candidates = self._prepare_trade_batch(connection, user_id, trades)
            if report["rejected"]:
                raise TradeBatchValidationError(report)
            for candidate in candidates:
                connection.execute(
                    """
                    INSERT INTO user_trade_execution(
                        execution_id, user_id, traded_on, traded_at, source_ref, symbol, side,
                        quantity, price, fee, method, setup_method, stop_price,
                        initial_stop_price,
                        rationale, invalidation, exit_reason, market_context,
                        observation_snapshot_json, plan_version_id, evaluation_cycle_id, setup_code,
                        setup_kind, plan_setup_id, setup_code_snapshot,
                        setup_name_snapshot, other_setup_note
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        candidate["execution_id"],
                        user_id,
                        candidate["traded_on"],
                        candidate["traded_at"],
                        candidate["source_ref"],
                        candidate["symbol"],
                        candidate["side"],
                        candidate["quantity"],
                        candidate["price"],
                        candidate["fee"],
                        candidate["method"],
                        candidate["setup_method"],
                        candidate["stop_price"],
                        candidate["stop_price"] if candidate["side"] == "BUY" else None,
                        candidate["rationale"],
                        candidate["invalidation"],
                        candidate["exit_reason"],
                        candidate["market_context"],
                        json.dumps(
                            candidate.get("observation_snapshot") or {},
                            ensure_ascii=False,
                            sort_keys=True,
                            allow_nan=False,
                        ),
                        candidate.get("plan_version_id"),
                        candidate.get("evaluation_cycle_id"),
                        candidate.get("setup_code"),
                        candidate.get("setup_kind", "OTHER"),
                        candidate.get("plan_setup_id"),
                        candidate.get("setup_code_snapshot", ""),
                        candidate.get("setup_name_snapshot", ""),
                        candidate.get("other_setup_note", ""),
                    ),
                )
            results = []
            for item in report["results"]:
                result = dict(item)
                if result["status"] == "READY":
                    result["status"] = "CREATED"
                results.append(result)
            response = {
                "batch_id": uuid4().hex,
                "status": "COMMITTED",
                "created": report["ready"],
                "duplicates": report["duplicates"],
                "rejected": 0,
                "replayed": False,
                "results": results,
            }
            connection.execute(
                """
                INSERT INTO user_trade_batch(
                    batch_id, user_id, idempotency_key, request_hash, response_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    response["batch_id"],
                    user_id,
                    key,
                    request_hash,
                    json.dumps(response, ensure_ascii=False, sort_keys=True),
                ),
            )
            self._insert_api_audit(
                connection,
                principal,
                action="trades.batch",
                request_id=request_id,
                outcome="COMMITTED",
                details={
                    "batch_id": response["batch_id"],
                    "created": response["created"],
                    "duplicates": response["duplicates"],
                    "replayed": False,
                },
            )
        return response

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
        if principal.user_id != user_id:
            raise ValueError("Token 与交易用户不匹配")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM user_trade_execution
                WHERE user_id=? AND execution_id=?
                """,
                (user_id, execution_id),
            ).fetchone()
            if row is None:
                raise ValueError("成交记录不存在")
            current = dict(row)
            if str(current["side"]) != "BUY":
                raise ValueError("止损价只能更新既有 BUY")
            normalized_stop = float(stop_price)
            if not math.isfinite(normalized_stop):
                raise ValueError("止损价必须为有限数值")
            if normalized_stop <= 0 or normalized_stop >= float(current["price"]):
                raise ValueError("止损价必须大于 0 且低于买入成交价")
            normalized_revision = _strict_positive_integer(
                expected_revision, field="expected_revision"
            )
            cursor = connection.execute(
                """
                UPDATE user_trade_execution
                SET stop_price=?, revision=revision + 1, updated_at=CURRENT_TIMESTAMP
                WHERE user_id=? AND execution_id=? AND revision=?
                """,
                (normalized_stop, user_id, execution_id, normalized_revision),
            )
            if cursor.rowcount != 1:
                raise TradeRevisionConflict("成交记录已变化，请重新读取后再更新")
            updated = connection.execute(
                """
                SELECT * FROM user_trade_execution
                WHERE user_id=? AND execution_id=?
                """,
                (user_id, execution_id),
            ).fetchone()
            assert updated is not None
            self._insert_api_audit(
                connection,
                principal,
                action="trades.stop",
                request_id=request_id,
                outcome="UPDATED",
                details={
                    "execution_id": execution_id,
                    "revision": int(updated["revision"]),
                },
            )
        assert updated is not None
        item = dict(updated)
        item["snapshot"] = json.loads(str(item.pop("observation_snapshot_json") or "{}"))
        return item

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
        """Fill a missing execution time without turning the Agent API into a general editor."""
        if principal.user_id != user_id:
            raise ValueError("Token 与交易用户不匹配")
        normalized_time = str(traded_at or "").strip()
        try:
            datetime.strptime(normalized_time, "%H:%M:%S")
        except ValueError as exc:
            raise ValueError("成交时间必须为 HH:MM:SS") from exc
        normalized_revision = _strict_positive_integer(
            expected_revision, field="expected_revision"
        )
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current_row = connection.execute(
                """
                SELECT * FROM user_trade_execution
                WHERE user_id=? AND execution_id=?
                """,
                (user_id, execution_id),
            ).fetchone()
            if current_row is None:
                raise ValueError("成交记录不存在")
            current = dict(current_row)
            if int(current.get("revision") or 1) != normalized_revision:
                raise TradeRevisionConflict("成交记录已变化，请重新读取后再更新")
            if str(current.get("traded_at") or "").strip():
                raise ValueError("成交时间已存在；set-time 只能补全空时间")
            rows = self._trade_rows(connection, user_id)
            for row in rows:
                if str(row["execution_id"]) == execution_id:
                    row["traded_at"] = normalized_time
                    break
            ordered = sorted(
                rows,
                key=lambda row: (
                    str(row["traded_on"]),
                    str(row.get("traded_at") or ""),
                    str(row.get("created_at") or ""),
                    str(row["execution_id"]),
                ),
            )
            _match_trade_executions(ordered)
            cursor = connection.execute(
                """
                UPDATE user_trade_execution
                SET traded_at=?, revision=revision + 1, updated_at=CURRENT_TIMESTAMP
                WHERE user_id=? AND execution_id=? AND revision=? AND traded_at=''
                """,
                (normalized_time, user_id, execution_id, normalized_revision),
            )
            if cursor.rowcount != 1:
                raise TradeRevisionConflict("成交记录已变化，请重新读取后再更新")
            updated = connection.execute(
                """
                SELECT * FROM user_trade_execution
                WHERE user_id=? AND execution_id=?
                """,
                (user_id, execution_id),
            ).fetchone()
            assert updated is not None
            self._insert_api_audit(
                connection,
                principal,
                action="trades.time",
                request_id=request_id,
                outcome="UPDATED",
                details={
                    "execution_id": execution_id,
                    "traded_at": normalized_time,
                    "revision": int(updated["revision"]),
                },
            )
        item = dict(updated)
        item["snapshot"] = json.loads(str(item.pop("observation_snapshot_json") or "{}"))
        return item

    def _prepare_trade_batch(
        self,
        connection: sqlite3.Connection,
        user_id: str,
        trades: Sequence[Mapping[str, Any]],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if not trades or len(trades) > 200:
            raise ValueError("每个交易批次必须包含 1 至 200 笔记录")
        existing = self._trade_rows(connection, user_id)
        working = list(existing)
        known: dict[tuple[Any, ...], Mapping[str, Any]] = {
            _trade_identity(row): row for row in existing
        }
        results: list[dict[str, Any] | None] = [None] * len(trades)
        normalized: list[dict[str, Any]] = []
        for index, payload in enumerate(trades):
            try:
                item = _normalize_api_trade(payload, index)
            except (TypeError, ValueError) as exc:
                results[index] = {
                    "index": index,
                    "client_id": str(payload.get("client_id") or ""),
                    "status": "REJECTED",
                    "reason": str(exc),
                }
            else:
                normalized.append(item)
        normalized.sort(key=lambda item: (item["traded_on"], item["traded_at"], int(item["index"])))
        candidates: list[dict[str, Any]] = []
        for item in normalized:
            index = int(item["index"])
            identity = _trade_identity(item)
            duplicate = known.get(identity)
            base_result = {
                "index": index,
                "client_id": item["client_id"],
                "symbol": item["symbol"],
                "traded_on": item["traded_on"],
                "traded_at": item["traded_at"],
                "side": item["side"],
                "quantity": item["quantity"],
                "price": item["price"],
            }
            if duplicate is not None:
                if item["source_ref"] and _trade_fingerprint(duplicate) != _trade_fingerprint(item):
                    results[index] = {
                        **base_result,
                        "status": "REJECTED",
                        "reason": "source_ref 已用于另一笔不同的成交",
                    }
                    continue
                results[index] = {
                    **base_result,
                    "status": "DUPLICATE",
                    "execution_id": str(duplicate["execution_id"]),
                    "revision": int(duplicate.get("revision") or 1),
                }
                continue
            if item["side"] == "BUY":
                requested = item.get("plan_version_id")
                active = self.trading_plans.current(user_id)
                if requested:
                    plan = self.trading_plans.show(user_id, str(requested))
                    if plan is None:
                        results[index] = {
                            **base_result,
                            "status": "REJECTED",
                            "reason": "计划版本不存在或不属于当前用户",
                        }
                        continue
                else:
                    plan = active
                if plan is not None:
                    item["plan_version_id"] = plan["version_id"]
                    item["evaluation_cycle_id"] = plan["evaluation_cycle_id"]
                assignment = _setup_assignment(
                    plan,
                    plan_setup_id=str(item.get("plan_setup_id") or OTHER_SETUP_ID),
                    setup_code=item.get("setup_code"),
                    setup_method=str(item.get("explicit_setup_method") or ""),
                    other_setup_note=str(item.get("other_setup_note") or ""),
                )
                item.update(assignment)
            else:
                item["plan_version_id"] = None
                item["evaluation_cycle_id"] = None
                item.update(_setup_assignment(None))
            execution_id = uuid4().hex
            candidate = {
                **item,
                "execution_id": execution_id,
                "revision": 1,
                "created_at": f"batch-{index:06d}",
            }
            try:
                ordered = sorted(
                    [*working, candidate],
                    key=lambda row: (
                        str(row["traded_on"]),
                        str(row.get("traded_at") or ""),
                        str(row.get("created_at") or ""),
                        str(row["execution_id"]),
                    ),
                )
                _match_trade_executions(ordered)
            except ValueError:
                available = _open_quantity(
                    [row for row in working if str(row["symbol"]) == item["symbol"]]
                )
                results[index] = {
                    **base_result,
                    "status": "REJECTED",
                    "reason": f"卖出数量超过可复盘持仓：可用 {available} 股",
                }
                continue
            working.append(candidate)
            known[identity] = candidate
            candidates.append(candidate)
            results[index] = {
                **base_result,
                "status": "READY",
                "execution_id": execution_id,
                "revision": 1,
            }
        final_results = [item for item in results if item is not None]
        return {
            "status": "VALID"
            if all(item["status"] != "REJECTED" for item in final_results)
            else "INVALID",
            "ready": sum(item["status"] == "READY" for item in final_results),
            "duplicates": sum(item["status"] == "DUPLICATE" for item in final_results),
            "rejected": sum(item["status"] == "REJECTED" for item in final_results),
            "results": final_results,
        }, candidates

    def chart_trade_overlay(
        self, user_id: str, symbol: str, end_date: str
    ) -> dict[str, list[dict[str, Any]]]:
        with self.connect() as connection:
            rows = self._trade_rows(connection, user_id, symbol.upper(), end_date)
        _, open_lots, _ = _match_trade_executions(rows, permit_unmatched_sells=True)
        return {
            "executions": [
                {
                    key: row[key]
                    for key in (
                        "execution_id",
                        "traded_on",
                        "side",
                        "quantity",
                        "price",
                        "stop_price",
                    )
                }
                for row in rows
            ],
            "open_stops": [
                {
                    "buy_execution_id": lot["buy_execution_id"],
                    "buy_date": lot["buy_date"],
                    "stop_price": lot["stop_price"],
                }
                for lot in open_lots
                if lot.get("stop_price") is not None
            ],
        }

    def chart_drawings(
        self, user_id: str, symbol: str, price_scale_id: str, interval: str = "day"
    ) -> list[dict[str, Any]]:
        if interval not in {"day", "week"}:
            raise ValueError("unsupported chart interval")
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT drawing_id, tool, anchors_json, content_text, interval_scope,
                       created_at, updated_at
                FROM user_chart_drawing
                WHERE user_id=? AND symbol=? AND price_scale_id=?
                  AND tool IN ('trendline', 'horizontal', 'text')
                  AND interval_scope IN ('both', ?)
                ORDER BY created_at, drawing_id
                """,
                (user_id, symbol.upper(), price_scale_id, interval),
            ).fetchall()
        return [{**dict(row), "anchors": json.loads(str(row["anchors_json"]))} for row in rows]

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
        normalized, normalized_content, normalized_interval = _normalize_drawing(
            tool, price_scale_id, anchors, content_text, interval_scope
        )
        with self.connect() as connection:
            existing = connection.execute(
                """SELECT user_id, symbol, price_scale_id, tool
                FROM user_chart_drawing WHERE drawing_id=?""",
                (drawing_id,),
            ).fetchone()
            if existing is not None and str(existing["user_id"]) != user_id:
                raise ValueError("画线记录不属于当前用户")
            if existing is not None and (
                str(existing["symbol"]) != symbol.upper()
                or str(existing["price_scale_id"]) != price_scale_id
                or str(existing["tool"]) != tool
            ):
                raise ValueError("画线记录与当前图表不匹配")
            connection.execute(
                """
                INSERT INTO user_chart_drawing(
                    drawing_id, user_id, symbol, price_scale_id, tool, anchors_json,
                    content_text, interval_scope
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(drawing_id) DO UPDATE SET
                    anchors_json=excluded.anchors_json,
                    content_text=excluded.content_text,
                    interval_scope=excluded.interval_scope,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    drawing_id,
                    user_id,
                    symbol.upper(),
                    price_scale_id,
                    tool,
                    json.dumps(normalized, ensure_ascii=False, separators=(",", ":")),
                    normalized_content,
                    normalized_interval,
                ),
            )
        return {
            "drawing_id": drawing_id,
            "tool": tool,
            "anchors": normalized,
            "content_text": normalized_content,
            "interval_scope": normalized_interval,
        }

    def delete_chart_drawing(
        self, user_id: str, drawing_id: str, symbol: str, price_scale_id: str
    ) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM user_chart_drawing
                WHERE user_id=? AND drawing_id=? AND symbol=? AND price_scale_id=?
                """,
                (user_id, drawing_id, symbol.upper(), price_scale_id),
            )
        return cursor.rowcount == 1

    @staticmethod
    def _trade_rows(
        connection: sqlite3.Connection,
        user_id: str,
        symbol: str | None = None,
        end_date: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["user_id=?"]
        parameters: list[Any] = [user_id]
        if symbol is not None:
            clauses.append("symbol=?")
            parameters.append(symbol)
        if end_date is not None:
            clauses.append("traded_on<=?")
            parameters.append(end_date)
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM user_trade_execution WHERE "
                + " AND ".join(clauses)
                + " ORDER BY traded_on, traded_at, created_at, execution_id",
                tuple(parameters),
            ).fetchall()
        ]


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
    if setup_code is not None and setup_code not in PLAN_SETUP_CODES:
        raise ValueError(f"计划形态必须为 {plan_setup_choices_text()}")
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
        [str(tag) for tag in tags if isinstance(tag, str)]
        if isinstance(tags, list)
        else []
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
                raise ValueError(
                    f"单个观察标签不能超过 {OBSERVATION_TAG_MAX_LENGTH} 个字符"
                )
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
