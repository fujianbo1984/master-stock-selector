from __future__ import annotations

import hashlib
import hmac
import json
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


class UserRepository:
    """Account and private-workspace storage; user_id is the tenant boundary."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.password_hasher = PasswordHasher()

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA secure_delete=ON")
            connection.executescript(USER_SCHEMA_SQL)
            self._migrate_observation_states(connection)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @staticmethod
    def _migrate_observation_states(connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='user_stock_review'"
        ).fetchone()
        table_sql = str(row["sql"] if row is not None else "")
        if "'DROPPED'" not in table_sql and "'UNREVIEWED'" not in table_sql:
            return
        connection.executescript(
            """
            ALTER TABLE user_stock_review RENAME TO user_stock_review_legacy;
            CREATE TABLE user_stock_review (
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
            INSERT INTO user_stock_review(user_id, symbol, manual_state, note, reviewed_at)
            SELECT user_id, symbol,
                   CASE manual_state WHEN 'DROPPED' THEN 'ARCHIVED' ELSE manual_state END,
                   note, reviewed_at
            FROM user_stock_review_legacy
            WHERE manual_state IN ('WATCH', 'FOCUS', 'DROPPED');
            DROP TABLE user_stock_review_legacy;
            """
        )

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
            with self.connect() as connection:
                quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
                accounts = int(
                    connection.execute("SELECT COUNT(*) FROM user_account").fetchone()[0]
                )
            return {
                "connected": quick_check == "ok",
                "quick_check": quick_check,
                "account_count": accounts,
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

    def change_password(self, user_id: str, current_password: str, new_password: str) -> bool:
        """Change an authenticated user's password and revoke every existing session."""
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
                    updated_at=CURRENT_TIMESTAMP
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
    if quantity <= 0 or price <= 0 or fee < 0:
        raise ValueError("数量、价格和费用必须有效")
    normalized_stop = float(stop_price) if stop_price is not None else None
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
