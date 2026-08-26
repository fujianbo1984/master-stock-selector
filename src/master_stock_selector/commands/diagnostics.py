from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..web.users import UserRepository

SHANGHAI = ZoneInfo("Asia/Shanghai")
DEGRADED_EXIT = 3


def handle(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.command == "doctor":
        return _doctor(args)
    if args.command == "runs":
        return _runs(args, parser)
    parser.error("unknown diagnostics command")
    return 2


def _doctor(args: argparse.Namespace) -> int:
    expected_date = _validated_date(str(args.expected_date or ""), "--expected-date")
    max_age_days = _positive_int(args.max_age_days, "--max-age-days")
    market_path = Path(args.market_database)
    watchlist_path = Path(args.watchlist_database)
    user_path = Path(args.user_database)

    market = _database_status(
        market_path,
        required_tables={"daily_bars"},
        deep=bool(args.deep),
    )
    watchlist = _database_status(
        watchlist_path,
        required_tables={"stock_method_daily_fact", "watchlist_run_receipt"},
        deep=bool(args.deep),
    )
    users = _user_database_status(user_path)

    latest_market_date = (
        _latest_value(
            market_path,
            "SELECT MAX(trade_date) FROM daily_bars "
            "WHERE market='ashare' AND adj_type='qfq'",
        )
        if market["connected"] and not market["missing_tables"]
        else ""
    )
    latest_fact_date = (
        _latest_value(
            watchlist_path,
            "SELECT MAX(as_of_date) FROM stock_method_daily_fact",
        )
        if watchlist["connected"] and not watchlist["missing_tables"]
        else ""
    )
    latest_collection = _optional_receipt_rows(market_path, "collection", 1)
    latest_selection = _optional_receipt_rows(watchlist_path, "watchlist", 1)
    freshness = evaluate_freshness(
        market_date=latest_market_date,
        fact_date=latest_fact_date,
        latest_collection=latest_collection[0] if latest_collection else None,
        expected_date=expected_date,
        max_age_days=max_age_days,
    )

    checks = {
        "market_database": bool(
            market["connected"]
            and not market["missing_tables"]
            and latest_market_date
            and (not args.deep or market["quick_check"] == "ok")
        ),
        "watchlist_database": bool(
            watchlist["connected"]
            and not watchlist["missing_tables"]
            and latest_fact_date
            and (not args.deep or watchlist["quick_check"] == "ok")
        ),
        "user_database": bool(users.get("compatible")),
        "collection_receipt": bool(
            latest_collection
            and latest_collection[0].get("status") == "SUCCESS"
            and latest_collection[0].get("trade_date") == latest_market_date
        ),
        "watchlist_receipt": bool(
            latest_selection
            and latest_selection[0].get("status") == "SUCCESS"
            and latest_selection[0].get("as_of_date") == latest_fact_date
        ),
        "data_freshness": freshness["status"] == "FRESH",
    }
    status = "READY" if all(checks.values()) else "DEGRADED"
    payload = {
        "status": status,
        "checks": checks,
        "data": {
            "market_date": latest_market_date,
            "watchlist_date": latest_fact_date,
            "freshness": freshness,
        },
        "databases": {
            "market": market,
            "watchlist": watchlist,
            "users": users,
        },
        "latest_runs": {
            "collection": latest_collection[0] if latest_collection else None,
            "watchlist": latest_selection[0] if latest_selection else None,
        },
        "configuration": {
            "tushare_token_configured": bool(os.environ.get("TUSHARE_TOKEN", "").strip()),
        },
    }
    _print(payload)
    return 0 if status == "READY" else DEGRADED_EXIT


def _runs(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    market_path = Path(args.market_database)
    watchlist_path = Path(args.watchlist_database)
    action = str(args.runs_action)
    if action == "list":
        limit = _positive_int(args.limit, "--limit")
        payload: dict[str, Any] = {
            "status": "OK",
            "collections": _receipt_rows(market_path, "collection", limit),
            "watchlist_runs": _receipt_rows(watchlist_path, "watchlist", limit),
        }
        _print(payload)
        return 0
    if action == "show":
        run_id = str(args.run_id).strip()
        if not run_id:
            parser.error("run_id 不能为空")
        row = _receipt_by_id(market_path, "collection", run_id)
        kind = "collection"
        if row is None:
            row = _receipt_by_id(watchlist_path, "watchlist", run_id)
            kind = "watchlist"
        if row is None:
            raise ValueError(f"运行凭证不存在：{run_id}")
        _print({"status": "OK", "kind": kind, "run": row})
        return 0
    if action == "latest":
        expected_date = _validated_date(str(args.expected_date or ""), "--expected-date")
        max_age_days = _positive_int(args.max_age_days, "--max-age-days")
        collections = _receipt_rows(market_path, "collection", 1)
        selections = _receipt_rows(watchlist_path, "watchlist", 1)
        market_date = _latest_value(
            market_path,
            "SELECT MAX(trade_date) FROM daily_bars "
            "WHERE market='ashare' AND adj_type='qfq'",
        )
        fact_date = _latest_value(
            watchlist_path,
            "SELECT MAX(as_of_date) FROM stock_method_daily_fact",
        )
        freshness = evaluate_freshness(
            market_date=market_date,
            fact_date=fact_date,
            latest_collection=collections[0] if collections else None,
            expected_date=expected_date,
            max_age_days=max_age_days,
        )
        payload = {
            "status": "OK" if freshness["status"] == "FRESH" else "DEGRADED",
            "market_date": market_date,
            "watchlist_date": fact_date,
            "freshness": freshness,
            "collection": collections[0] if collections else None,
            "watchlist_run": selections[0] if selections else None,
        }
        _print(payload)
        if bool(args.fail_if_stale) and freshness["status"] != "FRESH":
            return DEGRADED_EXIT
        return 0
    parser.error("unknown runs action")
    return 2


def evaluate_freshness(
    *,
    market_date: str,
    fact_date: str,
    latest_collection: dict[str, Any] | None,
    expected_date: str = "",
    max_age_days: int = 4,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    today = current.date()
    if not market_date or not fact_date:
        return {
            "status": "UNKNOWN",
            "reason": "MISSING_DATA_DATE",
            "expected_date": expected_date,
            "source": "explicit" if expected_date else "unavailable",
        }
    if market_date != fact_date:
        return {
            "status": "OUT_OF_SYNC",
            "reason": "MARKET_AND_WATCHLIST_DATES_DIFFER",
            "expected_date": expected_date,
            "source": "explicit" if expected_date else "date_alignment",
        }

    source = "explicit"
    effective_expected = expected_date
    if not effective_expected:
        open_dates = _receipt_open_dates(latest_collection)
        current_week = today.isocalendar()[:2]
        same_week_dates = [
            value
            for value in open_dates
            if date.fromisoformat(value).isocalendar()[:2] == current_week
        ]
        eligible = [value for value in same_week_dates if value <= today.isoformat()]
        if (
            today.isoformat() in eligible
            and current.timetz().replace(tzinfo=None) < time(17, 30)
        ):
            eligible.remove(today.isoformat())
        if eligible:
            effective_expected = max(eligible)
            source = "collection_trade_calendar"
        else:
            age_days = (today - date.fromisoformat(market_date)).days
            status = "FRESH" if age_days <= max_age_days else "STALE"
            return {
                "status": status,
                "reason": (
                    "RECENT_BY_CALENDAR_AGE"
                    if status == "FRESH"
                    else "CALENDAR_AGE_EXCEEDED"
                ),
                "expected_date": "",
                "source": "calendar_age_fallback",
                "age_days": age_days,
                "max_age_days": max_age_days,
            }

    status = "FRESH" if market_date >= effective_expected else "STALE"
    return {
        "status": status,
        "reason": "DATE_REACHED" if status == "FRESH" else "EXPECTED_DATE_NOT_REACHED",
        "expected_date": effective_expected,
        "source": source,
        "age_days": (today - date.fromisoformat(market_date)).days,
        "max_age_days": max_age_days,
    }


def _receipt_open_dates(receipt: dict[str, Any] | None) -> list[str]:
    if not receipt:
        return []
    quality = receipt.get("quality")
    if not isinstance(quality, dict):
        return []
    values: list[str] = []
    for raw in quality.get("week_open_dates", []):
        value = str(raw)
        try:
            date.fromisoformat(value)
        except ValueError:
            continue
        values.append(value)
    return sorted(set(values))


def _database_status(path: Path, *, required_tables: set[str], deep: bool) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(path.resolve()),
        "exists": path.is_file(),
        "connected": False,
        "missing_tables": sorted(required_tables),
        "quick_check": "not_run",
    }
    if not path.is_file():
        return result
    try:
        with _connect_read_only(path) as connection:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            result["connected"] = True
            result["missing_tables"] = sorted(required_tables - tables)
            result["bytes"] = path.stat().st_size
            if deep:
                result["quick_check"] = str(
                    connection.execute("PRAGMA quick_check").fetchone()[0]
                )
    except (OSError, sqlite3.Error) as exc:
        result["error"] = str(exc)
    return result


def _user_database_status(path: Path) -> dict[str, Any]:
    status = UserRepository(path).schema_status()
    return {"path": str(path.resolve()), "exists": path.is_file(), **status}


def _optional_receipt_rows(path: Path, kind: str, limit: int) -> list[dict[str, Any]]:
    try:
        return _receipt_rows(path, kind, limit)
    except (FileNotFoundError, ValueError, sqlite3.Error):
        return []


def _receipt_rows(path: Path, kind: str, limit: int) -> list[dict[str, Any]]:
    table = (
        "watchlist_market_collection_receipt"
        if kind == "collection"
        else "watchlist_run_receipt"
    )
    if not path.is_file():
        raise FileNotFoundError(f"database does not exist: {path}")
    with _connect_read_only(path) as connection:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if exists is None:
            raise ValueError(f"database is missing required table: {table}")
        rows = connection.execute(
            f'SELECT * FROM "{table}" ORDER BY started_at DESC, run_id DESC LIMIT ?',
            (limit,),
        ).fetchall()
    return [_decoded_receipt(dict(row), kind) for row in rows]


def _receipt_by_id(path: Path, kind: str, run_id: str) -> dict[str, Any] | None:
    table = (
        "watchlist_market_collection_receipt"
        if kind == "collection"
        else "watchlist_run_receipt"
    )
    if not path.is_file():
        return None
    with _connect_read_only(path) as connection:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if exists is None:
            return None
        row = connection.execute(
            f'SELECT * FROM "{table}" WHERE run_id=?', (run_id,)
        ).fetchone()
    return _decoded_receipt(dict(row), kind) if row is not None else None


def _decoded_receipt(row: dict[str, Any], kind: str) -> dict[str, Any]:
    json_field = "quality_json" if kind == "collection" else "counts_json"
    output_field = "quality" if kind == "collection" else "counts"
    raw = row.pop(json_field, "{}")
    try:
        row[output_field] = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        row[output_field] = {"decode_error": True}
    return row


def _latest_value(path: Path, query: str) -> str:
    if not path.is_file():
        raise FileNotFoundError(f"database does not exist: {path}")
    with _connect_read_only(path) as connection:
        row = connection.execute(query).fetchone()
    return str(row[0] or "") if row is not None else ""


def _connect_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    return connection


def _validated_date(value: str, option: str) -> str:
    if not value:
        return ""
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError(f"{option} 必须是 YYYY-MM-DD") from exc


def _positive_int(value: int | str, option: str) -> int:
    normalized = int(value)
    if normalized < 1:
        raise ValueError(f"{option} 必须大于 0")
    return normalized


def _print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
