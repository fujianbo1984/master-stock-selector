from __future__ import annotations

import argparse
import getpass
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

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
    migrate = subparsers.add_parser("migrate-legacy")
    migrate.add_argument("--source", required=True)
    migrate.add_argument("--username", required=True)
    migrate.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repository = UserRepository(Path(args.database))
    repository.initialize()
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
            (user_id, *(row[column] for column in columns))
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
