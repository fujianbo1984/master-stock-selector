from __future__ import annotations

import json
import sqlite3
import subprocess
import sys

import pytest

from master_stock_selector.web.users import UserRepository
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


def test_user_can_change_password_and_all_existing_sessions_are_revoked(tmp_path):
    repository = UserRepository(tmp_path / "users.sqlite3")
    repository.initialize()
    user_id = repository.create_user("alice", "Secure-password-123")
    first_token, _ = repository.create_session(user_id)
    second_token, _ = repository.create_session(user_id)
    api_token, _ = repository.create_api_token(user_id, "password-change")

    assert not repository.change_password(
        user_id, "wrong-password", "New-secure-password-456"
    )
    assert repository.session_user(first_token) is not None
    assert repository.authenticate("alice", "Secure-password-123") is not None

    assert repository.change_password(
        user_id, "Secure-password-123", "New-secure-password-456"
    )
    assert repository.session_user(first_token) is None
    assert repository.session_user(second_token) is None
    assert repository.api_token_user(api_token) is None
    assert repository.authenticate("alice", "Secure-password-123") is None
    assert repository.authenticate("alice", "New-secure-password-456") is not None

    with pytest.raises(ValueError, match="新密码不能与当前密码相同"):
        repository.change_password(
            user_id, "New-secure-password-456", "New-secure-password-456"
        )


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


def test_user_trade_journal_accepts_all_eight_setup_options(tmp_path):
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

    assert {row["setup_method"]: row["setup_label"] for row in review["executions"]} == setups
    assert list(review["by_setup"]) == list(setups)


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
    assert not repository.delete_chart_drawing(
        bob, "line-1", "000001.SZ", "qfq-scale-v1"
    )
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
