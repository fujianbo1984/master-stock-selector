from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from time import time
from typing import Any
from uuid import uuid4

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from ..watchlist.repository import (
    TRADE_METHOD_LABELS,
    TRADE_SETUP_LABELS,
    _match_trade_executions,
    _open_quantity,
    _summarize_open_lots,
    _trade_statistics,
)

SESSION_COOKIE = "masterstock_session"
SESSION_IDLE_SECONDS = 7 * 24 * 60 * 60
API_TOKEN_PREFIX = "mst_"
API_TOKEN_SCOPES = frozenset({"trades:read", "trades:write"})
USER_SCHEMA_VERSION = 2
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
    }
)

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
    setup_method TEXT NOT NULL DEFAULT 'PULLBACK'
        CHECK (setup_method IN ('BREAKOUT', 'PULLBACK')),
    stop_price REAL CHECK (stop_price IS NULL OR stop_price > 0),
    rationale TEXT NOT NULL DEFAULT '',
    invalidation TEXT NOT NULL DEFAULT '',
    exit_reason TEXT NOT NULL DEFAULT '',
    market_context TEXT NOT NULL DEFAULT '',
    observation_snapshot_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    FOREIGN KEY (user_id) REFERENCES user_account(user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_user_trade_date
ON user_trade_execution(user_id, traded_on, created_at);
CREATE INDEX IF NOT EXISTS idx_user_trade_symbol
ON user_trade_execution(user_id, symbol, traded_on, created_at);
CREATE TABLE IF NOT EXISTS user_chart_drawing (
    drawing_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    price_scale_id TEXT NOT NULL,
    tool TEXT NOT NULL CHECK (tool IN ('trendline', 'horizontal')),
    anchors_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES user_account(user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_user_chart_symbol
ON user_chart_drawing(user_id, symbol, price_scale_id, created_at);
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

    def connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            connection = sqlite3.connect(
                f"file:{self.path.resolve()}?mode=ro", uri=True, timeout=5
            )
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
                ],
                "missing_indexes": ["idx_user_trade_source_ref"],
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
                indexes = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='index'"
                    )
                }
                review_row = connection.execute(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type='table' AND name='user_stock_review'"
                ).fetchone()
                review_table_sql = str(review_row[0] if review_row is not None else "")
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
            for column in {"source_ref", "revision"} - trade_columns
        )
        missing_indexes = sorted({"idx_user_trade_source_ref"} - indexes)
        legacy_observation_states = (
            "'DROPPED'" in review_table_sql or "'UNREVIEWED'" in review_table_sql
        )
        compatible = (
            quick_check == "ok"
            and not missing_tables
            and not missing_columns
            and not missing_indexes
            and not legacy_observation_states
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
            trade_table_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='user_trade_execution'"
            ).fetchone() is not None
            trade_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(user_trade_execution)")
            }
            review_row = connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='user_stock_review'"
            ).fetchone()
            review_table_sql = str(review_row[0] if review_row is not None else "")
            migration_statements: list[str] = []
            if trade_table_exists and "source_ref" not in trade_columns:
                migration_statements.append(
                    "ALTER TABLE user_trade_execution ADD COLUMN source_ref TEXT;"
                )
            if trade_table_exists and "revision" not in trade_columns:
                migration_statements.append(
                    "ALTER TABLE user_trade_execution "
                    "ADD COLUMN revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0);"
                )
            if "'DROPPED'" in review_table_sql or "'UNREVIEWED'" in review_table_sql:
                migration_statements.extend(
                    [
                        "ALTER TABLE user_stock_review "
                        "RENAME TO user_stock_review_legacy;",
                        """CREATE TABLE user_stock_review (
                            user_id TEXT NOT NULL,
                            symbol TEXT NOT NULL,
                            manual_state TEXT NOT NULL CHECK (
                                manual_state IN ('WATCH', 'FOCUS', 'ARCHIVED')
                            ),
                            note TEXT NOT NULL DEFAULT '',
                            reviewed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            PRIMARY KEY (user_id, symbol),
                            FOREIGN KEY (user_id) REFERENCES user_account(user_id)
                                ON DELETE CASCADE
                        );""",
                        """INSERT INTO user_stock_review(
                            user_id, symbol, manual_state, note, reviewed_at
                        )
                        SELECT user_id, symbol,
                               CASE manual_state
                                   WHEN 'DROPPED' THEN 'ARCHIVED'
                                   ELSE manual_state
                               END,
                               note, reviewed_at
                        FROM user_stock_review_legacy
                        WHERE manual_state IN ('WATCH', 'FOCUS', 'DROPPED');""",
                        "DROP TABLE user_stock_review_legacy;",
                    ]
                )
            migration_sql = "\n".join(
                [
                    "BEGIN IMMEDIATE;",
                    USER_SCHEMA_SQL,
                    *migration_statements,
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
        connection.execute("ALTER TABLE user_stock_review RENAME TO user_stock_review_legacy")
        connection.execute(
            """CREATE TABLE user_stock_review (
                user_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                manual_state TEXT NOT NULL CHECK (
                    manual_state IN ('WATCH', 'FOCUS', 'ARCHIVED')
                ),
                note TEXT NOT NULL DEFAULT '',
                reviewed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, symbol),
                FOREIGN KEY (user_id) REFERENCES user_account(user_id) ON DELETE CASCADE
            )"""
        )
        connection.execute(
            """INSERT INTO user_stock_review(user_id, symbol, manual_state, note, reviewed_at)
            SELECT user_id, symbol,
                   CASE manual_state WHEN 'DROPPED' THEN 'ARCHIVED' ELSE manual_state END,
                   note, reviewed_at
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
                verified = self.password_hasher.verify(
                    str(row["password_hash"]), current_password
                )
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
        expires_days: int = 30,
        scopes: Sequence[str] = ("trades:read", "trades:write"),
    ) -> tuple[str, dict[str, Any]]:
        normalized_name = name.strip()
        normalized_scopes = tuple(sorted({scope.strip() for scope in scopes if scope.strip()}))
        if not normalized_name or len(normalized_name) > 80:
            raise ValueError("Token 名称长度必须为 1 至 80 个字符")
        if expires_days < 1 or expires_days > 90:
            raise ValueError("Token 有效期必须为 1 至 90 天")
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
                SELECT symbol, manual_state, note, reviewed_at
                FROM user_stock_review
                WHERE user_id=? AND symbol IN ({placeholders})
                """,
                (user_id, *normalized),
            ).fetchall()
        return {str(row["symbol"]): dict(row) for row in rows}

    def reviews_for_user(self, user_id: str) -> list[dict[str, Any]]:
        """Return the user's stocks that currently belong to their observation workspace."""
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT symbol, manual_state, note, reviewed_at
                FROM user_stock_review
                WHERE user_id=?
                ORDER BY reviewed_at DESC, symbol
                """,
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def review(self, user_id: str, symbol: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT symbol, manual_state, note, reviewed_at
                FROM user_stock_review WHERE user_id=? AND symbol=?
                """,
                (user_id, symbol.upper()),
            ).fetchone()
        if row is not None:
            return dict(row)
        return {
            "symbol": symbol.upper(),
            "manual_state": "UNREVIEWED",
            "note": "",
            "reviewed_at": "",
        }

    def save_review(self, user_id: str, symbol: str, manual_state: str, note: str) -> None:
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
        with self.connect() as connection:
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
        setup_method: str = "PULLBACK",
        stop_price: float | None = None,
        rationale: str = "",
        invalidation: str = "",
        exit_reason: str = "",
        market_context: str = "",
        observation_snapshot: Mapping[str, Any] | None = None,
        permit_unmatched_sell: bool = False,
    ) -> str:
        values = _validated_trade_values(
            traded_on=traded_on,
            traded_at=traded_at,
            side=side,
            quantity=quantity,
            price=price,
            fee=fee,
            method=method,
            setup_method=setup_method,
            stop_price=stop_price,
        )
        normalized_symbol = symbol.upper().strip()
        if not normalized_symbol or "." not in normalized_symbol:
            raise ValueError("股票代码格式不正确")
        execution_id = uuid4().hex
        with self.connect() as connection:
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
                        raise ValueError(
                            f"卖出数量超过可复盘持仓：可用 {available} 股"
                        ) from exc
            connection.execute(
                """
                INSERT INTO user_trade_execution(
                    execution_id, user_id, traded_on, traded_at, symbol, side,
                    quantity, price, fee, method, setup_method, stop_price,
                    rationale, invalidation, exit_reason, market_context,
                    observation_snapshot_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    rationale.strip(),
                    invalidation.strip(),
                    exit_reason.strip(),
                    market_context.strip(),
                    json.dumps(observation_snapshot or {}, ensure_ascii=False, sort_keys=True),
                ),
            )
        return execution_id

    def trade_review(self, user_id: str, names: Mapping[str, str]) -> dict[str, Any]:
        with self.connect() as connection:
            rows = self._trade_rows(connection, user_id)
        for row in rows:
            row["snapshot"] = json.loads(str(row.pop("observation_snapshot_json") or "{}"))
        closed, open_lots, unmatched_sells = _match_trade_executions(
            rows, permit_unmatched_sells=True
        )
        open_positions = _summarize_open_lots(open_lots)
        for collection in (rows, closed, open_positions, unmatched_sells):
            for item in collection:
                symbol = str(item["symbol"])
                item["stock_name"] = names.get(symbol, "名称待补")
                item["setup_label"] = TRADE_SETUP_LABELS.get(
                    str(item.get("setup_method") or "PULLBACK"), "回调"
                )
        return {
            "executions": list(reversed(rows)),
            "closed": list(reversed(closed)),
            "open_positions": open_positions,
            "unmatched_sells": list(reversed(unmatched_sells)),
            "summary": _trade_statistics(closed),
            "by_setup": {
                setup: _trade_statistics(
                    [item for item in closed if item["setup_method"] == setup]
                )
                for setup in ("BREAKOUT", "PULLBACK")
            },
            "sample_state": (
                "样本不足：少于 20 笔已完成交易，仅展示描述统计。"
                if len(closed) < 20
                else "样本达到基础复盘门槛；仍不构成策略有效性或买卖建议。"
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
        item["setup_label"] = TRADE_SETUP_LABELS.get(
            str(item.get("setup_method") or "PULLBACK"), "回调"
        )
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
        setup_method: str = "PULLBACK",
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
            setup_method=setup_method,
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
                        rationale, invalidation, exit_reason, market_context,
                        observation_snapshot_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        normalized.sort(
            key=lambda item: (item["traded_on"], item["traded_at"], int(item["index"]))
        )
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
            "status": "VALID" if all(item["status"] != "REJECTED" for item in final_results)
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
        self, user_id: str, symbol: str, price_scale_id: str
    ) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT drawing_id, tool, anchors_json, created_at, updated_at
                FROM user_chart_drawing
                WHERE user_id=? AND symbol=? AND price_scale_id=?
                  AND tool IN ('trendline', 'horizontal')
                ORDER BY created_at, drawing_id
                """,
                (user_id, symbol.upper(), price_scale_id),
            ).fetchall()
        return [
            {**dict(row), "anchors": json.loads(str(row["anchors_json"]))} for row in rows
        ]

    def save_chart_drawing(
        self,
        user_id: str,
        drawing_id: str,
        symbol: str,
        price_scale_id: str,
        tool: str,
        anchors: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        normalized = _normalize_drawing(tool, price_scale_id, anchors)
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT user_id FROM user_chart_drawing WHERE drawing_id=?",
                (drawing_id,),
            ).fetchone()
            if existing is not None and str(existing["user_id"]) != user_id:
                raise ValueError("画线记录不属于当前用户")
            connection.execute(
                """
                INSERT INTO user_chart_drawing(
                    drawing_id, user_id, symbol, price_scale_id, tool, anchors_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(drawing_id) DO UPDATE SET
                    anchors_json=excluded.anchors_json,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    drawing_id,
                    user_id,
                    symbol.upper(),
                    price_scale_id,
                    tool,
                    json.dumps(normalized, ensure_ascii=False, separators=(",", ":")),
                ),
            )
        return {"drawing_id": drawing_id, "tool": tool, "anchors": normalized}

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
        "stop_price",
        "rationale",
        "invalidation",
        "exit_reason",
        "market_context",
        "observation_snapshot",
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
    values = _validated_trade_values(
        traded_on=str(payload.get("traded_on") or ""),
        traded_at=str(payload.get("traded_at") or ""),
        side=side,
        quantity=quantity,
        price=price,
        fee=fee,
        method="MANUAL",
        setup_method=str(payload.get("setup_method") or "PULLBACK"),
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
        "stop_price": values["stop_price"],
        "rationale": str(payload.get("rationale") or "").strip(),
        "invalidation": str(payload.get("invalidation") or "").strip(),
        "exit_reason": str(payload.get("exit_reason") or "").strip(),
        "market_context": str(payload.get("market_context") or "").strip(),
        "observation_snapshot": dict(snapshot),
    }


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
    if not all(character.isalnum() or character in {"@", ".", "_", "-"} for character in normalized):
        raise ValueError("用户名只能包含字母、数字及 @ . _ -")
    return normalized


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
    if normalized_side not in {"BUY", "SELL"} or normalized_method not in TRADE_METHOD_LABELS:
        raise ValueError("方向或关联方法不正确")
    if normalized_setup not in TRADE_SETUP_LABELS:
        raise ValueError("交易方法必须为突破或回调")
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
) -> list[dict[str, float | str]]:
    if tool not in {"trendline", "horizontal"}:
        raise ValueError("unsupported drawing tool")
    if not price_scale_id:
        raise ValueError("missing price scale identifier")
    expected_count = 2 if tool == "trendline" else 1
    if len(anchors) != expected_count:
        raise ValueError(f"{tool} requires {expected_count} anchors")
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
    return normalized
