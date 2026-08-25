from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from master_stock_selector.web.users import API_TOKEN_SCOPES, UserRepository
from master_stock_selector.web.users_cli import migrate_legacy_private_data, migrate_user_schema


def test_accounts_sessions_and_sqlite_security_settings(tmp_path):
    path = tmp_path / "users.sqlite3"
    repository = UserRepository(path)
    repository.initialize()
    user_id = repository.create_user("alice", "Secure-password-123", "Alice")

    assert path.stat().st_mode & 0o777 == 0o600
    with repository.connect() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        stored = connection.execute(
            "SELECT password_hash FROM user_account WHERE user_id=?", (user_id,)
        ).fetchone()[0]
    assert stored.startswith("$argon2id$")
    assert repository.authenticate("alice", "wrong-password") is None
    account = repository.authenticate("ALICE", "Secure-password-123")
    assert account is not None

    token, csrf = repository.create_session(user_id)
    api_token, _ = repository.create_api_token(user_id, "disable-account")
    user = repository.session_user(token)
    assert user is not None
    assert user.user_id == user_id
    assert repository.csrf_valid(user, csrf)
    assert not repository.csrf_valid(user, "wrong")

    repository.set_user_status("alice", "DISABLED")
    assert repository.session_user(token) is None
    assert repository.api_token_user(api_token) is None
    assert repository.authenticate("alice", "Secure-password-123") is None


def test_new_api_token_defaults_to_all_scopes_and_365_days(tmp_path):
    repository = UserRepository(tmp_path / "users.sqlite3")
    repository.initialize()
    user_id = repository.create_user("alice", "Secure-password-123")

    _, metadata = repository.create_api_token(user_id, "default-all")

    assert metadata["scopes"] == sorted(API_TOKEN_SCOPES)
    assert metadata["expires_at_epoch"] - metadata["created_at_epoch"] == 365 * 24 * 60 * 60
    with pytest.raises(ValueError, match="1 至 365 天"):
        repository.create_api_token(user_id, "too-long", expires_days=366)


def test_user_can_change_password_and_all_existing_sessions_are_revoked(tmp_path):
    repository = UserRepository(tmp_path / "users.sqlite3")
    repository.initialize()
    user_id = repository.create_user("alice", "Secure-password-123")
    first_token, _ = repository.create_session(user_id)
    second_token, _ = repository.create_session(user_id)
    api_token, _ = repository.create_api_token(user_id, "password-change")

    assert not repository.change_password(user_id, "wrong-password", "New-secure-password-456")
    assert repository.session_user(first_token) is not None
    assert repository.authenticate("alice", "Secure-password-123") is not None

    assert repository.change_password(user_id, "Secure-password-123", "New-secure-password-456")
    assert repository.session_user(first_token) is None
    assert repository.session_user(second_token) is None
    assert repository.api_token_user(api_token) is None
    assert repository.authenticate("alice", "Secure-password-123") is None
    assert repository.authenticate("alice", "New-secure-password-456") is not None

    with pytest.raises(ValueError, match="新密码不能与当前密码相同"):
        repository.change_password(user_id, "New-secure-password-456", "New-secure-password-456")


def test_admin_password_reset_revokes_sessions_and_api_tokens(tmp_path):
    repository = UserRepository(tmp_path / "users.sqlite3")
    repository.initialize()
    user_id = repository.create_user("alice", "Secure-password-123")
    session, _ = repository.create_session(user_id)
    api_token, _ = repository.create_api_token(user_id, "password-reset")

    repository.reset_password("alice", "Reset-password-456")

    assert repository.session_user(session) is None
    assert repository.api_token_user(api_token) is None
    assert repository.authenticate("alice", "Reset-password-456") is not None


def test_access_log_schema_migrates_from_v3_and_preserves_existing_rows(tmp_path: Path) -> None:
    path = tmp_path / "users.sqlite3"
    repository = UserRepository(path)
    repository.initialize()
    user_id = repository.create_user("owner", "Secure-password-123")
    with repository.connect() as connection:
        connection.execute("DROP TABLE user_access_log")
        connection.execute("PRAGMA user_version=3")

    status = repository.schema_status()
    assert status["status"] == "MIGRATION_REQUIRED"
    assert status["missing_tables"] == ["user_access_log"]

    backup = tmp_path / "users.before-schema-v4.sqlite3"
    migrated = migrate_user_schema(repository, apply=True, backup=backup)

    assert migrated["existing_row_counts_preserved"] is True
    assert repository.schema_status()["compatible"] is True
    with repository.connect(read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM user_account").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM user_access_log").fetchone()[0] == 0
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 10
    assert user_id


def test_frozen_full_v4_schema_migrates_to_v9_without_data_loss(tmp_path: Path) -> None:
    path = tmp_path / "users-v4.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        (Path(__file__).parent / "fixtures" / "user_schema_v4.sql").read_text()
    )
    connection.executescript(
        """
        INSERT INTO user_account(user_id,username,password_hash,status)
        VALUES('u1','owner','legacy-hash','ACTIVE');
        INSERT INTO user_session(
            session_id_hash,user_id,csrf_secret,created_at_epoch,last_seen_epoch,expires_at_epoch
        ) VALUES('s1','u1','csrf',1,1,9999999999);
        INSERT INTO user_api_token(
            token_id,user_id,name,token_prefix,token_hash,scopes_json,
            created_at_epoch,expires_at_epoch
        ) VALUES('t1','u1','agent','mst_old','hash','["trades:read"]',1,9999999999);
        INSERT INTO user_trade_batch(
            batch_id,user_id,idempotency_key,request_hash,response_json
        ) VALUES('b1','u1','key','request','{}');
        INSERT INTO user_api_audit(
            audit_id,user_id,token_id,action,request_id,outcome
        ) VALUES('a1','u1','t1','read','request-1','OK');
        INSERT INTO user_stock_review(user_id,symbol,manual_state,note)
        VALUES('u1','000001.SZ','FOCUS','legacy note');
        INSERT INTO user_trade_execution(
            execution_id,user_id,traded_on,symbol,side,quantity,price,method,
            setup_method,stop_price
        ) VALUES('e1','u1','2026-08-01','000001.SZ','BUY',100,10,'MANUAL',
                 'PULLBACK_SUPPORT',9);
        INSERT INTO user_chart_drawing(
            drawing_id,user_id,symbol,price_scale_id,tool,anchors_json
        ) VALUES('d1','u1','000001.SZ','price','horizontal','[]');
        INSERT INTO user_access_log(
            access_id,user_id,client_ip,location_source,method,path,status_code
        ) VALUES('l1','u1','127.0.0.1','test','GET','/a/daily',200);
        """
    )
    connection.close()

    repository = UserRepository(path)
    with sqlite3.connect(path) as read_connection:
        before_version = read_connection.execute("PRAGMA user_version").fetchone()[0]
    with pytest.raises(RuntimeError, match="不会自动迁移"):
        repository.require_schema()
    with sqlite3.connect(path) as read_connection:
        assert read_connection.execute("PRAGMA user_version").fetchone()[0] == before_version

    backup = tmp_path / "users-v4.backup.sqlite3"
    migrated = migrate_user_schema(repository, apply=True, backup=backup)
    assert migrated["existing_row_counts_preserved"] is True
    assert repository.schema_status()["compatible"] is True
    with repository.connect(read_only=True) as migrated_connection:
        trade = migrated_connection.execute(
            """SELECT setup_method,plan_version_id,evaluation_cycle_id,setup_code,
            initial_stop_price,setup_kind,plan_setup_id,setup_name_snapshot
            FROM user_trade_execution WHERE execution_id='e1'"""
        ).fetchone()
        assert tuple(trade) == (
            "PULLBACK_SUPPORT", None, None, None, 9.0,
            "LEGACY", None, "简单回调",
        )
        for table in (
            "user_account",
            "user_session",
            "user_api_token",
            "user_trade_batch",
            "user_api_audit",
            "user_stock_review",
            "user_trade_execution",
            "user_chart_drawing",
            "user_access_log",
        ):
            assert migrated_connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1
        unique_indexes = {
            str(row[1]): bool(row[2])
            for row in migrated_connection.execute("PRAGMA index_list(user_plan_grill_event)")
        }
        assert unique_indexes["idx_user_plan_event_session_revision"] is True
        assert migrated_connection.execute(
            "SELECT COUNT(*) FROM user_trading_plan_share"
        ).fetchone()[0] == 0
        share_indexes = {
            str(row[1])
            for row in migrated_connection.execute(
                "PRAGMA index_list(user_trading_plan_share)"
            )
        }
        assert "idx_user_plan_share_plan_status" in share_indexes
        assert "idx_user_plan_share_active" in share_indexes
        access_indexes = {
            str(row[1])
            for row in migrated_connection.execute("PRAGMA index_list(user_access_log)")
        }
        assert "idx_user_access_occurred" in access_indexes
        assert "idx_user_access_user_occurred" not in access_indexes
        assert "idx_user_access_path_occurred" not in access_indexes
        assert migrated_connection.execute(
            "SELECT COUNT(*) FROM user_access_daily_summary"
        ).fetchone()[0] == 0
        drawing = migrated_connection.execute(
            "SELECT tool, content_text, interval_scope FROM user_chart_drawing WHERE drawing_id='d1'"
        ).fetchone()
        assert tuple(drawing) == ("horizontal", "", "both")


def test_access_log_maintenance_rolls_up_expired_days_atomically_and_is_idempotent(
    tmp_path: Path,
) -> None:
    repository = UserRepository(tmp_path / "users.sqlite3")
    repository.initialize()
    with repository.connect() as connection:
        connection.executemany(
            """INSERT INTO user_access_log(
                access_id, occurred_at, client_ip, country_code, region_name,
                city_name, location_source, method, path, status_code
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    "old-1", "2026-05-01 01:00:00", "1.1.1.1", "CN", "北京市",
                    "北京市", "TEST", "GET", "/stock/000001.SZ", 200,
                ),
                (
                    "old-2", "2026-05-01 02:00:00", "2.2.2.2", "CN", "北京市",
                    "北京市", "TEST", "GET", "/stock/000002.SZ", 200,
                ),
                (
                    "old-3", "2026-05-01 03:00:00", "1.1.1.1", "US", "",
                    "", "TEST", "POST", "/api/v1/private/value", 404,
                ),
                (
                    "keep-1", "2026-05-25 01:00:00", "3.3.3.3", "CN", "上海市",
                    "上海市", "TEST", "GET", "/a/daily", 200,
                ),
            ],
        )

    preview = repository.access_log_maintenance_status(
        retention_days=90, as_of=date(2026, 8, 23)
    )
    assert preview["eligible_rows"] == 3
    assert preview["eligible_dates"] == 1
    with repository.connect(read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM user_access_log").fetchone()[0] == 4

    result = repository.maintain_access_log(
        retention_days=90, as_of=date(2026, 8, 23)
    )
    assert result["processed_dates"] == ["2026-05-01"]
    assert result["rows_deleted"] == 3
    assert result["summary_rows"] == 2
    assert result["quick_check"] == "ok"
    with repository.connect(read_only=True) as connection:
        assert connection.execute("SELECT access_id FROM user_access_log").fetchone()[0] == "keep-1"
        rows = connection.execute(
            """SELECT country_code, region_name, path_group, method, status_class,
                      request_count, unique_ip_count
               FROM user_access_daily_summary ORDER BY country_code"""
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            ("CN", "北京市", "/stock/*", "GET", 200, 2, 2),
            ("US", "", "/api/v1/*", "POST", 400, 1, 1),
        ]
        summary_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(user_access_daily_summary)")
        }
        assert {"client_ip", "user_id", "city_name"}.isdisjoint(summary_columns)

    repeated = repository.maintain_access_log(
        retention_days=90, as_of=date(2026, 8, 23)
    )
    assert repeated["processed_dates"] == []
    assert repeated["rows_deleted"] == 0
    with repository.connect(read_only=True) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM user_access_daily_summary"
        ).fetchone()[0] == 2


def test_access_log_maintenance_cli_is_dry_run_unless_apply_is_explicit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "users.sqlite3"
    repository = UserRepository(path)
    repository.initialize()
    with repository.connect() as connection:
        connection.execute(
            """INSERT INTO user_access_log(
                access_id, occurred_at, client_ip, location_source, method, path, status_code
            ) VALUES ('old', '2026-01-01 00:00:00', '1.1.1.1', 'TEST', 'GET', '/', 200)"""
        )
    base_command = [
        sys.executable,
        "-m",
        "master_stock_selector.web.users_cli",
        "--database",
        str(path),
        "access-log-maintain",
        "--retention-days",
        "90",
        "--as-of",
        "2026-08-23",
    ]

    preview = subprocess.run(base_command, check=True, capture_output=True, text=True)
    assert json.loads(preview.stdout)["status"] == "dry-run"
    with repository.connect(read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM user_access_log").fetchone()[0] == 1

    applied = subprocess.run(
        [*base_command, "--apply"], check=True, capture_output=True, text=True
    )
    assert json.loads(applied.stdout)["rows_deleted"] == 1
    with repository.connect(read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM user_access_log").fetchone()[0] == 0


def test_explicit_user_schema_migration_backs_up_and_preserves_rows(tmp_path):
    path = tmp_path / "users.sqlite3"
    repository = UserRepository(path)
    repository.initialize()
    user_id = repository.create_user("owner", "Secure-password-123")
    repository.record_trade(
        user_id,
        traded_on="2026-08-01",
        traded_at="09:30:00",
        symbol="000001.SZ",
        side="BUY",
        quantity=100,
        price=10.0,
        fee=1.0,
        method="MANUAL",
    )
    with repository.connect() as connection:
        connection.execute("DROP INDEX idx_user_trade_source_ref")
        connection.execute("DROP TABLE user_api_audit")
        connection.execute("DROP TABLE user_trade_batch")
        connection.execute("DROP TABLE user_api_token")
        connection.execute("ALTER TABLE user_trade_execution DROP COLUMN source_ref")
        connection.execute("ALTER TABLE user_trade_execution DROP COLUMN revision")
        connection.execute("PRAGMA user_version=1")

    assert repository.schema_status()["status"] == "MIGRATION_REQUIRED"
    dry_run = migrate_user_schema(repository, apply=False, backup=None)
    assert dry_run["status"] == "dry-run"
    backup = tmp_path / "users.before-schema-v2.sqlite3"
    migrated = migrate_user_schema(repository, apply=True, backup=backup)

    assert migrated["status"] == "migrated"
    assert migrated["backup"] == {"path": str(backup.resolve()), "quick_check": "ok"}
    assert backup.stat().st_mode & 0o777 == 0o600
    assert migrated["existing_row_counts_preserved"] is True
    assert repository.schema_status()["compatible"] is True
    with repository.connect(read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM user_account").fetchone()[0] == 1
        trade = connection.execute(
            "SELECT source_ref, revision FROM user_trade_execution"
        ).fetchone()
    assert tuple(trade) == (None, 1)


def test_setup_migration_maps_legacy_breakout_and_pullback_without_losing_rows(tmp_path):
    path = tmp_path / "users.sqlite3"
    repository = UserRepository(path)
    repository.initialize()
    user_id = repository.create_user("owner", "Secure-password-123")
    with repository.connect() as connection:
        connection.executescript(
            """
            ALTER TABLE user_trade_execution RENAME TO user_trade_execution_v3;
            CREATE TABLE user_trade_execution (
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
                method TEXT NOT NULL,
                setup_method TEXT NOT NULL DEFAULT 'PULLBACK'
                    CHECK (setup_method IN ('BREAKOUT', 'PULLBACK')),
                stop_price REAL,
                rationale TEXT NOT NULL DEFAULT '',
                invalidation TEXT NOT NULL DEFAULT '',
                exit_reason TEXT NOT NULL DEFAULT '',
                market_context TEXT NOT NULL DEFAULT '',
                observation_snapshot_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                revision INTEGER NOT NULL DEFAULT 1
            );
            DROP TABLE user_trade_execution_v3;
            PRAGMA user_version=2;
            """
        )
        connection.executemany(
            """INSERT INTO user_trade_execution(
                execution_id, user_id, traded_on, symbol, side, quantity,
                price, method, setup_method
            ) VALUES (?, ?, '2026-08-01', ?, 'BUY', 100, 10, 'MANUAL', ?)""",
            [
                ("legacy-breakout", user_id, "000001.SZ", "BREAKOUT"),
                ("legacy-pullback", user_id, "000002.SZ", "PULLBACK"),
            ],
        )

    status = repository.schema_status()
    assert status["legacy_trade_setups"] is True
    backup = tmp_path / "users.before-schema-v3.sqlite3"
    migrated = migrate_user_schema(repository, apply=True, backup=backup)

    assert migrated["existing_row_counts_preserved"] is True
    assert repository.schema_status()["compatible"] is True
    with repository.connect(read_only=True) as connection:
        setups = dict(
            connection.execute(
                "SELECT execution_id, setup_method FROM user_trade_execution"
            ).fetchall()
        )
    assert setups == {
        "legacy-breakout": "POST_BREAKOUT_FIRST_PULLBACK",
        "legacy-pullback": "PULLBACK_SUPPORT",
    }


def test_legacy_setup_inputs_are_preserved_but_not_treated_as_plan_setups(tmp_path):
    repository = UserRepository(tmp_path / "users.sqlite3")
    repository.initialize()
    user_id = repository.create_user("owner", "Secure-password-123")
    setups = {
        "FAILED_TEST": "失败测试",
        "PULLBACK_SUPPORT": "简单回调",
        "LOWER_TIMEFRAME_BREAKOUT": "低周期突破入场",
        "COMPLEX_PULLBACK": "复杂回调",
        "ANTI": "Anti（趋势转换首次回调）",
        "PRE_BREAKOUT_BASE": "突破前基底入场",
        "POST_BREAKOUT_FIRST_PULLBACK": "突破后回调",
        "FAILED_BREAKOUT": "失败突破",
    }
    for index, setup in enumerate(setups, start=1):
        repository.record_trade(
            user_id,
            traded_on="2026-08-01",
            symbol=f"{index:06d}.SZ",
            side="BUY",
            quantity=100,
            price=10,
            fee=0,
            method="MANUAL",
            setup_method=setup,
        )

    review = repository.trade_review(user_id, {})

    assert {
        row["setup_method"]: row["setup_label"] for row in review["executions"]
    } == {key: f"历史：{label}" for key, label in setups.items()}
    assert all(row["setup_kind"] == "LEGACY" for row in review["executions"])
    assert review["by_plan_setup"] == {}


def test_new_trade_without_plan_defaults_to_other(tmp_path):
    repository = UserRepository(tmp_path / "users.sqlite3")
    repository.initialize()
    user_id = repository.create_user("owner", "Secure-password-123")

    execution_id = repository.record_trade(
        user_id,
        traded_on="2026-08-01",
        symbol="000001.SZ",
        side="BUY",
        quantity=100,
        price=10,
        fee=0,
        method="MANUAL",
    )

    trade = repository.trade_execution(user_id, execution_id)
    assert trade is not None
    assert trade["setup_kind"] == "OTHER"
    assert trade["plan_setup_id"] is None
    assert trade["setup_label"] == "其他（未归入交易计划）"


def test_private_rows_are_scoped_by_user_id(tmp_path):
    repository = UserRepository(tmp_path / "users.sqlite3")
    repository.initialize()
    alice = repository.create_user("alice", "Secure-password-123")
    bob = repository.create_user("bob", "Secure-password-123")

    repository.save_review(alice, "000001.SZ", "FOCUS", "Alice note")
    execution_id = repository.record_trade(
        alice,
        traded_on="2026-08-01",
        symbol="000001.SZ",
        side="BUY",
        quantity=100,
        price=10.0,
        fee=1.0,
        method="MANUAL",
        stop_price=9.0,
    )
    repository.save_chart_drawing(
        alice,
        "line-1",
        "000001.SZ",
        "qfq-scale-v1",
        "horizontal",
        [{"date": "2026-08-01", "price": 9.0}],
    )

    assert repository.review(alice, "000001.SZ")["note"] == "Alice note"
    assert repository.review(alice, "000001.SZ")["manual_state"] == "FOCUS"
    assert repository.review(bob, "000001.SZ")["note"] == ""
    assert repository.trade_execution(alice, execution_id) is not None
    assert repository.trade_execution(bob, execution_id) is None
    assert repository.chart_drawings(bob, "000001.SZ", "qfq-scale-v1") == []
    assert not repository.delete_chart_drawing(bob, "line-1", "000001.SZ", "qfq-scale-v1")
    with pytest.raises(ValueError, match="成交记录不存在"):
        repository.update_trade(
            bob,
            execution_id,
            traded_on="2026-08-01",
            traded_at="",
            side="BUY",
            quantity=100,
            price=10.0,
            fee=1.0,
            method="MANUAL",
            stop_price=9.0,
        )


def test_chart_text_drawings_are_validated_editable_and_scoped_by_interval(tmp_path):
    repository = UserRepository(tmp_path / "users.sqlite3")
    repository.initialize()
    user_id = repository.create_user("owner", "Secure-password-123")

    saved = repository.save_chart_drawing(
        user_id,
        "text-1",
        "000001.SZ",
        "qfq-scale-v1",
        "text",
        [{"date": "2026-08-01", "price": 10.5}],
        "  突破后首次缩量回踩  ",
        "day",
    )
    assert saved["content_text"] == "突破后首次缩量回踩"
    assert saved["interval_scope"] == "day"
    assert repository.chart_drawings(user_id, "000001.SZ", "qfq-scale-v1", "week") == []
    assert repository.chart_drawings(user_id, "000001.SZ", "qfq-scale-v1", "day")[0][
        "content_text"
    ] == "突破后首次缩量回踩"

    edited = repository.save_chart_drawing(
        user_id,
        "text-1",
        "000001.SZ",
        "qfq-scale-v1",
        "text",
        [{"date": "2026-08-01", "price": 10.5}],
        "前高转为支撑",
        "day",
    )
    assert edited["content_text"] == "前高转为支撑"
    with pytest.raises(ValueError, match="不能为空"):
        repository.save_chart_drawing(
            user_id, "empty", "000001.SZ", "qfq-scale-v1", "text",
            [{"date": "2026-08-01", "price": 10.5}], "", "day",
        )
    with pytest.raises(ValueError, match="200"):
        repository.save_chart_drawing(
            user_id, "long", "000001.SZ", "qfq-scale-v1", "text",
            [{"date": "2026-08-01", "price": 10.5}], "过" * 201, "day",
        )


def test_observation_states_are_compact_and_legacy_rows_migrate_in_place(tmp_path):
    path = tmp_path / "users.sqlite3"
    repository = UserRepository(path)
    repository.initialize()
    user_id = repository.create_user("owner", "Secure-password-123")
    with repository.connect() as connection:
        connection.execute("DROP TABLE user_stock_review")
        connection.executescript(
            """
            CREATE TABLE user_stock_review(
                user_id TEXT NOT NULL, symbol TEXT NOT NULL,
                manual_state TEXT NOT NULL CHECK (
                    manual_state IN ('UNREVIEWED', 'WATCH', 'FOCUS', 'DROPPED')
                ), note TEXT NOT NULL DEFAULT '', reviewed_at TEXT NOT NULL,
                PRIMARY KEY(user_id, symbol)
            );
            """
        )
        connection.execute(
            "INSERT INTO user_stock_review VALUES (?, '000001.SZ', 'DROPPED', 'legacy', '2026-08-01')",
            (user_id,),
        )
        connection.execute(
            "INSERT INTO user_stock_review VALUES (?, '000002.SZ', 'UNREVIEWED', '', '2026-08-01')",
            (user_id,),
        )

    assert repository.schema_status()["status"] == "MIGRATION_REQUIRED"
    backup = tmp_path / "users.before-observation-migration.sqlite3"
    result = migrate_user_schema(repository, apply=True, backup=backup)

    assert result["status"] == "migrated"
    assert result["backup"] == {"path": str(backup.resolve()), "quick_check": "ok"}
    assert repository.review(user_id, "000001.SZ")["manual_state"] == "ARCHIVED"
    assert repository.review(user_id, "000002.SZ")["manual_state"] == "UNREVIEWED"
    assert [row["symbol"] for row in repository.reviews_for_user(user_id)] == ["000001.SZ"]
    repository.save_review(user_id, "000001.SZ", "UNREVIEWED", "")
    assert repository.reviews_for_user(user_id) == []
    with repository.connect() as connection:
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='user_stock_review'"
        ).fetchone()[0]
    assert "ARCHIVED" in table_sql
    assert "DROPPED" not in table_sql


def test_legacy_private_data_migration_requires_apply_and_preserves_source(tmp_path):
    source = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.executescript(
            """
            CREATE TABLE manual_watch_review(
                symbol TEXT PRIMARY KEY, manual_state TEXT, note TEXT, reviewed_at TEXT
            );
            CREATE TABLE trade_execution(
                execution_id TEXT PRIMARY KEY, traded_on TEXT, traded_at TEXT,
                symbol TEXT, side TEXT, quantity INTEGER, price REAL, fee REAL,
                method TEXT, setup_method TEXT, stop_price REAL, rationale TEXT,
                invalidation TEXT, exit_reason TEXT, market_context TEXT,
                observation_snapshot_json TEXT, created_at TEXT
            );
            CREATE TABLE chart_drawing(
                drawing_id TEXT PRIMARY KEY, symbol TEXT, price_scale_id TEXT,
                tool TEXT, anchors_json TEXT, created_at TEXT, updated_at TEXT
            );
            INSERT INTO manual_watch_review VALUES(
                '000001.SZ', 'FOCUS', 'legacy note', '2026-08-01 10:00:00'
            );
            INSERT INTO trade_execution VALUES(
                'trade-1', '2026-08-01', '', '000001.SZ', 'BUY', 100, 10, 0,
                'MANUAL', 'PULLBACK', 9, '', '', '', '', '{}', '2026-08-01 10:00:00'
            );
            INSERT INTO chart_drawing VALUES(
                'line-1', '000001.SZ', 'qfq-scale-v1', 'horizontal',
                '[{"date":"2026-08-01","price":9}]',
                '2026-08-01 10:00:00', '2026-08-01 10:00:00'
            );
            """
        )
    destination = UserRepository(tmp_path / "users.sqlite3")
    destination.initialize()
    user_id = destination.create_user("owner", "Secure-password-123")

    dry_run = migrate_legacy_private_data(
        source=source, destination=destination, username="owner", apply=False
    )
    assert dry_run["status"] == "dry-run"
    assert destination.review(user_id, "000001.SZ")["note"] == ""

    migrated = migrate_legacy_private_data(
        source=source, destination=destination, username="owner", apply=True
    )
    assert migrated["status"] == "migrated"
    assert migrated["target_quick_check"] == "ok"
    assert destination.review(user_id, "000001.SZ")["note"] == "legacy note"
    assert destination.trade_execution(user_id, "trade-1") is not None
    assert len(destination.chart_drawings(user_id, "000001.SZ", "qfq-scale-v1")) == 1
    with sqlite3.connect(source) as connection:
        assert connection.execute("SELECT COUNT(*) FROM trade_execution").fetchone()[0] == 1


def test_backup_script_uses_sqlite_backup_api_and_verifies_result(tmp_path):
    source = tmp_path / "users.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE sample(value TEXT)")
        connection.execute("INSERT INTO sample VALUES ('ok')")
    destination = tmp_path / "backups"

    result = subprocess.run(
        [
            sys.executable,
            "scripts/backup_sqlite.py",
            "--source",
            str(source),
            "--destination",
            str(destination),
            "--retention-days",
            "7",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    backup = destination / payload["backups"][0]["backup"].rsplit("/", 1)[1]
    assert payload["status"] == "ok"
    assert backup.is_file()
    assert backup.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(backup) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT value FROM sample").fetchone()[0] == "ok"
