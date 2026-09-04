from __future__ import annotations

import hmac
import json
import secrets
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from time import time
from typing import Any
from uuid import uuid4

from argon2.exceptions import InvalidHashError, VerifyMismatchError

from .user_store import UserStore
from .users import (
    API_TOKEN_DEFAULT_EXPIRES_DAYS,
    API_TOKEN_MAX_EXPIRES_DAYS,
    API_TOKEN_PREFIX,
    API_TOKEN_SCOPES,
    DEFAULT_ACCESS_LOG_RETENTION_DAYS,
    SESSION_IDLE_SECONDS,
    ApiTokenPrincipal,
    AuthenticatedUser,
    _access_log_cutoff,
    _access_path_group,
    _normalize_username,
    _token_hash,
    _validate_password,
)


class UserIdentityStore(UserStore):
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
            raise ValueError(f"Token 有效期必须为 1 至 {API_TOKEN_MAX_EXPIRES_DAYS} 天")
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
