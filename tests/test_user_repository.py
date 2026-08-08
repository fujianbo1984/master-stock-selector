from __future__ import annotations

import json
import sqlite3
import subprocess
import sys

import pytest

from master_stock_selector.web.users import UserRepository
from master_stock_selector.web.users_cli import migrate_legacy_private_data


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
    user = repository.session_user(token)
    assert user is not None
    assert user.user_id == user_id
    assert repository.csrf_valid(user, csrf)
    assert not repository.csrf_valid(user, "wrong")

    repository.set_user_status("alice", "DISABLED")
    assert repository.session_user(token) is None
    assert repository.authenticate("alice", "Secure-password-123") is None


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

    repository.initialize()

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
