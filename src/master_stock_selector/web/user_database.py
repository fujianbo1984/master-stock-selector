from __future__ import annotations

import sqlite3
from typing import Any

from .user_store import UserStore
from .users import (
    USER_REQUIRED_INDEXES,
    USER_REQUIRED_TABLES,
    USER_SCHEMA_SQL,
    USER_SCHEMA_VERSION,
)


class UserDatabase(UserStore):
    """Connection, compatibility checks, and explicit schema migration."""

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
                    for row in connection.execute("PRAGMA table_info(user_reading_note_version)")
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
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='user_chart_drawing'"
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
        if "user_reading_note_version" in tables and "assets_json" not in reading_version_columns:
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
                str(row[1]) for row in connection.execute("PRAGMA table_info(user_stock_review)")
            }
            drawing_row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='user_chart_drawing'"
            ).fetchone()
            drawing_table_sql = str(drawing_row[0] if drawing_row is not None else "")
            reading_note_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(user_reading_note)")
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
                    "ALTER TABLE user_stock_review ADD COLUMN tags_json TEXT NOT NULL DEFAULT '[]';"
                )
            if review_columns and "c1_record_json" in review_columns:
                migration_statements.append(
                    """INSERT OR IGNORE INTO user_setup_observation(
                        user_id, symbol, setup_id, plan_version_id,
                        setup_code_snapshot, setup_name_snapshot, status, evidence_json,
                        updated_at
                    )
                    SELECT user_id, symbol, 'C1', NULL, 'C1', '区间失败测试',
                           CASE
                               WHEN json_extract(c1_record_json, '$.structure_state')='READY'
                                   THEN 'READY'
                               WHEN json_extract(c1_record_json, '$.structure_state')='INVALID'
                                   THEN 'INVALID'
                               ELSE 'WATCH'
                           END,
                           c1_record_json, reviewed_at
                    FROM user_stock_review
                    WHERE c1_record_json NOT IN ('', '{}');"""
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
            str(column[1]) for column in connection.execute("PRAGMA table_info(user_stock_review)")
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
