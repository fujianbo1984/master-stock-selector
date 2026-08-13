from __future__ import annotations

import argparse
import getpass
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from ..watchlist.repository import TRADE_SETUP_ALIASES
from .users import UserRepository


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="管理大师选股个人账号与旧私有数据迁移")
    parser.add_argument(
        "--database",
        default=os.environ.get("MASTERSTOCK_USER_DATABASE", "data/users.sqlite3"),
    )
    subparsers = parser.add_subparsers(dest="action", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("username")
    create.add_argument("--display-name", default="")
    subparsers.add_parser("list")
    for action in ("enable", "disable", "reset-password"):
        command = subparsers.add_parser(action)
        command.add_argument("username")
    subparsers.add_parser("schema-check")
    schema_migrate = subparsers.add_parser("schema-migrate")
    schema_migrate.add_argument("--apply", action="store_true")
    schema_migrate.add_argument("--backup", default="")
    migrate = subparsers.add_parser("migrate-legacy")
    migrate.add_argument("--source", required=True)
    migrate.add_argument("--username", required=True)
    migrate.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repository = UserRepository(Path(args.database))
    if args.action == "schema-check":
        status = repository.schema_status()
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0 if status["compatible"] else 2
    if args.action == "schema-migrate":
        result = migrate_user_schema(
            repository,
            apply=bool(args.apply),
            backup=Path(args.backup) if args.backup else None,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    repository.require_schema()
    if args.action == "create":
        password = _confirmed_password()
        user_id = repository.create_user(args.username, password, args.display_name)
        print(json.dumps({"status": "created", "user_id": user_id, "username": args.username}))
        return 0
    if args.action == "list":
        print(json.dumps(repository.list_users(), ensure_ascii=False, indent=2))
        return 0
    if args.action == "enable":
        repository.set_user_status(args.username, "ACTIVE")
        print(json.dumps({"status": "enabled", "username": args.username}))
        return 0
    if args.action == "disable":
        repository.set_user_status(args.username, "DISABLED")
        print(json.dumps({"status": "disabled", "username": args.username}))
        return 0
    if args.action == "reset-password":
        repository.reset_password(args.username, _confirmed_password())
        print(json.dumps({"status": "password-reset", "username": args.username}))
        return 0
    if args.action == "migrate-legacy":
        result = migrate_legacy_private_data(
            source=Path(args.source),
            destination=repository,
            username=args.username,
            apply=bool(args.apply),
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    raise AssertionError(f"unhandled action: {args.action}")


def migrate_user_schema(
    repository: UserRepository,
    *,
    apply: bool,
    backup: Path | None,
) -> dict[str, Any]:
    before = _database_inventory(repository.path) if repository.path.is_file() else None
    status = repository.schema_status()
    if not apply:
        return {
            "status": "ready" if status["compatible"] else "dry-run",
            "schema": status,
            "inventory": before,
            "next": (
                "无需迁移"
                if status["compatible"]
                else (
                    "使用 --apply 创建新用户库"
                    if status["status"] == "MISSING"
                    else "对现有数据库执行时必须指定 --apply 和未存在的 --backup 路径"
                )
            ),
        }
    if status["compatible"]:
        return {"status": "already-ready", "schema": status, "inventory": before}
    backup_result: dict[str, Any] | None = None
    if repository.path.is_file():
        if backup is None:
            raise ValueError("迁移现有用户库必须指定 --backup 路径")
        backup_result = _backup_database(repository.path, backup)
    after_schema = repository.migrate_schema()
    after = _database_inventory(repository.path)
    changed_counts = {
        table: {"before": count, "after": after["table_counts"].get(table)}
        for table, count in (before or {}).get("table_counts", {}).items()
        if table != "user_stock_review" or not status.get("legacy_observation_states")
        if after["table_counts"].get(table) != count
    }
    if status.get("legacy_observation_states"):
        before_states = (before or {}).get("observation_state_counts", {})
        expected_reviews = sum(
            int(before_states.get(state, 0))
            for state in ("WATCH", "FOCUS", "DROPPED", "ARCHIVED")
        )
        actual_reviews = int(after["table_counts"].get("user_stock_review", 0))
        if actual_reviews != expected_reviews:
            changed_counts["user_stock_review"] = {
                "expected_after": expected_reviews,
                "actual_after": actual_reviews,
            }
    if changed_counts:
        raise RuntimeError(
            "迁移改变了既有表行数；请停止 Web 并使用备份回滚："
            + json.dumps(changed_counts, ensure_ascii=False, sort_keys=True)
        )
    return {
        "status": "migrated",
        "backup": backup_result,
        "schema": after_schema,
        "before": before,
        "after": after,
        "existing_row_counts_preserved": True,
    }


def _database_inventory(path: Path) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    try:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        if quick_check != "ok":
            raise ValueError(f"用户数据库 quick_check 失败：{quick_check}")
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        indexes = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        counts = {
            table: int(
                connection.execute(
                    f'SELECT COUNT(*) FROM "{table.replace(chr(34), chr(34) * 2)}"'
                ).fetchone()[0]
            )
            for table in tables
        }
        observation_state_counts = (
            {
                str(row[0]): int(row[1])
                for row in connection.execute(
                    "SELECT manual_state, COUNT(*) FROM user_stock_review "
                    "GROUP BY manual_state"
                )
            }
            if "user_stock_review" in tables
            else {}
        )
        return {
            "quick_check": quick_check,
            "tables": tables,
            "indexes": indexes,
            "table_counts": counts,
            "observation_state_counts": observation_state_counts,
        }
    finally:
        connection.close()


def _backup_database(source: Path, backup: Path) -> dict[str, Any]:
    if backup.exists():
        raise ValueError(f"备份目标已存在，拒绝覆盖：{backup}")
    backup.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True)
    target_connection = sqlite3.connect(backup)
    try:
        source_connection.backup(target_connection)
        quick_check = str(target_connection.execute("PRAGMA quick_check").fetchone()[0])
    finally:
        target_connection.close()
        source_connection.close()
    if quick_check != "ok":
        raise ValueError(f"用户库备份 quick_check 失败：{quick_check}")
    return {"path": str(backup.resolve()), "quick_check": quick_check}


def migrate_legacy_private_data(
    *,
    source: Path,
    destination: UserRepository,
    username: str,
    apply: bool,
) -> dict[str, Any]:
    if not source.is_file():
        raise ValueError(f"旧数据库不存在：{source}")
    users = destination.list_users()
    account = next(
        (item for item in users if str(item["username"]).lower() == username.lower()), None
    )
    if account is None:
        raise ValueError("目标用户不存在")
    user_id = str(account["user_id"])
    source_connection = sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True)
    source_connection.row_factory = sqlite3.Row
    try:
        source_check = str(source_connection.execute("PRAGMA quick_check").fetchone()[0])
        if source_check != "ok":
            raise ValueError(f"旧数据库 quick_check 失败：{source_check}")
        tables = {
            str(row[0])
            for row in source_connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        counts = {
            table: int(source_connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            if table in tables
            else 0
            for table in ("manual_watch_review", "trade_execution", "chart_drawing")
        }
        if not apply:
            return {
                "status": "dry-run",
                "source_quick_check": source_check,
                "target_user_id": user_id,
                "counts": counts,
                "next": "重新执行并增加 --apply 后才会写入",
            }
        with destination.connect() as target:
            target.execute("BEGIN IMMEDIATE")
            if counts["manual_watch_review"]:
                target.executemany(
                    """
                    INSERT INTO user_stock_review(
                        user_id, symbol, manual_state, note, reviewed_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, symbol) DO UPDATE SET
                        manual_state=excluded.manual_state,
                        note=excluded.note,
                        reviewed_at=excluded.reviewed_at
                    """,
                    [
                        (
                            user_id,
                            str(row["symbol"]),
                            (
                                "ARCHIVED"
                                if str(row["manual_state"]) == "DROPPED"
                                else str(row["manual_state"])
                            ),
                            str(row["note"]),
                            str(row["reviewed_at"]),
                        )
                        for row in source_connection.execute(
                            "SELECT * FROM manual_watch_review"
                        )
                        if str(row["manual_state"]) != "UNREVIEWED"
                    ],
                )
            _copy_legacy_rows(
                source_connection,
                target,
                source_table="trade_execution",
                target_table="user_trade_execution",
                user_id=user_id,
            )
            _copy_legacy_rows(
                source_connection,
                target,
                source_table="chart_drawing",
                target_table="user_chart_drawing",
                user_id=user_id,
            )
        with destination.connect() as target:
            target_check = str(target.execute("PRAGMA quick_check").fetchone()[0])
            migrated = {
                "user_stock_review": int(
                    target.execute(
                        "SELECT COUNT(*) FROM user_stock_review WHERE user_id=?", (user_id,)
                    ).fetchone()[0]
                ),
                "user_trade_execution": int(
                    target.execute(
                        "SELECT COUNT(*) FROM user_trade_execution WHERE user_id=?", (user_id,)
                    ).fetchone()[0]
                ),
                "user_chart_drawing": int(
                    target.execute(
                        "SELECT COUNT(*) FROM user_chart_drawing WHERE user_id=?", (user_id,)
                    ).fetchone()[0]
                ),
            }
        if target_check != "ok":
            raise ValueError(f"目标数据库 quick_check 失败：{target_check}")
        return {
            "status": "migrated",
            "source_quick_check": source_check,
            "target_quick_check": target_check,
            "target_user_id": user_id,
            "source_counts": counts,
            "target_counts": migrated,
        }
    finally:
        source_connection.close()


def _copy_legacy_rows(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    *,
    source_table: str,
    target_table: str,
    user_id: str,
) -> None:
    source_tables = {
        str(row[0])
        for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if source_table not in source_tables:
        return
    source_columns = [
        str(row[1]) for row in source.execute(f"PRAGMA table_info({source_table})")
    ]
    target_columns = {
        str(row[1]) for row in target.execute(f"PRAGMA table_info({target_table})")
    }
    columns = [column for column in source_columns if column in target_columns]
    placeholders = ",".join("?" for _ in range(len(columns) + 1))
    target.executemany(
        f"INSERT INTO {target_table}(user_id,{','.join(columns)}) VALUES ({placeholders})",
        [
            (
                user_id,
                *(
                    TRADE_SETUP_ALIASES.get(str(row[column]), str(row[column]))
                    if target_table == "user_trade_execution" and column == "setup_method"
                    else row[column]
                    for column in columns
                ),
            )
            for row in source.execute(f"SELECT {','.join(columns)} FROM {source_table}")
        ],
    )


def _confirmed_password() -> str:
    first = getpass.getpass("新密码：")
    second = getpass.getpass("再次输入：")
    if first != second:
        raise ValueError("两次密码输入不一致")
    return first


if __name__ == "__main__":
    raise SystemExit(main())
