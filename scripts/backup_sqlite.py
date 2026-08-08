from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def main() -> int:
    parser = argparse.ArgumentParser(description="创建一致性的 MasterStock SQLite 备份")
    parser.add_argument("--source", action="append", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--retention-days", type=int, default=7)
    args = parser.parse_args()
    if args.retention_days < 1:
        parser.error("retention-days 必须大于 0")
    destination = Path(args.destination)
    destination.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    results = [backup(Path(source), destination, timestamp) for source in args.source]
    removed = prune(destination, datetime.now(timezone.utc) - timedelta(days=args.retention_days))
    print(
        json.dumps(
            {"status": "ok", "backups": results, "removed_expired": removed},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def backup(source: Path, destination: Path, timestamp: str) -> dict[str, Any]:
    if not source.is_file():
        raise FileNotFoundError(source)
    target = destination / f"{source.stem}-{timestamp}.sqlite3"
    source_connection = sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True)
    target_connection = sqlite3.connect(target)
    try:
        source_check = str(source_connection.execute("PRAGMA quick_check").fetchone()[0])
        if source_check != "ok":
            raise RuntimeError(f"源数据库 quick_check 失败：{source}: {source_check}")
        source_connection.backup(target_connection)
        target_check = str(target_connection.execute("PRAGMA quick_check").fetchone()[0])
        if target_check != "ok":
            raise RuntimeError(f"备份数据库 quick_check 失败：{target}: {target_check}")
    finally:
        target_connection.close()
        source_connection.close()
    target.chmod(0o600)
    return {
        "source": str(source.resolve()),
        "backup": str(target.resolve()),
        "bytes": target.stat().st_size,
        "quick_check": "ok",
    }


def prune(destination: Path, cutoff: datetime) -> list[str]:
    removed: list[str] = []
    for path in destination.glob("*-????????T??????Z.sqlite3"):
        try:
            stamp = path.stem.rsplit("-", 1)[1]
            created = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except (IndexError, ValueError):
            continue
        if created >= cutoff:
            continue
        path.unlink()
        removed.append(str(path))
    return removed


if __name__ == "__main__":
    raise SystemExit(main())
