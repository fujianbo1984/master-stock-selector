"""Build compact SQLite deployment candidates without mutating source databases."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .collector import COLLECTION_SCHEMA_SQL
from .repository import WATCHLIST_SCHEMA_SQL


def optimize_databases(
    *,
    market_source: Path,
    market_target: Path,
    watchlist_source: Path,
    watchlist_target: Path,
) -> dict[str, Any]:
    """Copy and normalize both public databases into new deployment candidates."""
    for source in (market_source, watchlist_source):
        if not source.is_file():
            raise FileNotFoundError(f"source database does not exist: {source}")
    for target in (market_target, watchlist_target):
        if target.exists():
            raise FileExistsError(f"target database already exists: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)

    _backup(market_source, market_target)
    try:
        market_counts = _optimize_market(market_target)
        _backup(watchlist_source, watchlist_target)
        watchlist_counts = _optimize_watchlist(watchlist_target)
        _quick_check(market_target)
        _quick_check(watchlist_target)
    except BaseException:
        # Candidate files are never authoritative.  Remove a partial build so
        # a later run cannot accidentally mistake it for a validated database.
        market_target.unlink(missing_ok=True)
        watchlist_target.unlink(missing_ok=True)
        raise
    return {
        "market": {
            "source": str(market_source.resolve()),
            "target": str(market_target.resolve()),
            "source_bytes": market_source.stat().st_size,
            "target_bytes": market_target.stat().st_size,
            **market_counts,
        },
        "watchlist": {
            "source": str(watchlist_source.resolve()),
            "target": str(watchlist_target.resolve()),
            "source_bytes": watchlist_source.stat().st_size,
            "target_bytes": watchlist_target.stat().st_size,
            **watchlist_counts,
        },
    }


def validate_database_equivalence(
    *,
    market_source: Path,
    market_target: Path,
    watchlist_source: Path,
    watchlist_target: Path,
) -> dict[str, Any]:
    """Prove that compact histories reproduce every legacy snapshot date."""
    for path in (market_source, market_target, watchlist_source, watchlist_target):
        if not path.is_file():
            raise FileNotFoundError(f"database does not exist: {path}")
        _quick_check(path)
    market_dates = _validate_market_history(market_source, market_target)
    identity_dates = _validate_watchlist_identity(watchlist_source, watchlist_target)
    membership_dates = _validate_watchlist_membership(watchlist_source, watchlist_target)
    core_tables = (
        "stock_method_daily_fact",
        "stock_method_transition",
        "index_weinstein_weekly_fact",
        "index_minervini_stage2_daily_fact",
        "industry_observation_daily_fact",
        "watchlist_run_receipt",
    )
    core_counts: dict[str, int] = {}
    with sqlite3.connect(f"file:{watchlist_source.resolve()}?mode=ro", uri=True) as source:
        with sqlite3.connect(f"file:{watchlist_target.resolve()}?mode=ro", uri=True) as target:
            source_tables = _tables(source)
            target_tables = _tables(target)
            for table in core_tables:
                if table not in source_tables and table not in target_tables:
                    continue
                source_count = (
                    int(source.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                    if table in source_tables else 0
                )
                target_count = (
                    int(target.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                    if table in target_tables else 0
                )
                if source_count != target_count:
                    raise ValueError(
                        f"core table count mismatch for {table}: {source_count} != {target_count}"
                    )
                core_counts[table] = target_count
    return {
        "status": "EQUIVALENT",
        "market_snapshot_dates": market_dates,
        "watchlist_identity_dates": identity_dates,
        "watchlist_membership_dates": membership_dates,
        "core_table_counts": core_counts,
    }


def _validate_market_history(source_path: Path, target_path: Path) -> int:
    with sqlite3.connect(f"file:{target_path.resolve()}?mode=ro", uri=True) as target:
        target.row_factory = sqlite3.Row
        changes = _rows_by_date(
            target.execute("SELECT * FROM security_master_history ORDER BY valid_from, symbol"),
            "valid_from",
        )
    current: dict[str, tuple[Any, ...]] = {}
    checked = 0
    with sqlite3.connect(f"file:{source_path.resolve()}?mode=ro", uri=True) as source:
        source.row_factory = sqlite3.Row
        rows = source.execute(
            """
            SELECT as_of_date, members_json FROM security_master_snapshots
            WHERE market='ashare' ORDER BY as_of_date, created_at
            """
        )
        for row in rows:
            as_of_date = str(row["as_of_date"])
            for change in changes.get(as_of_date, []):
                current[str(change["symbol"])] = _market_history_state(change)
            expected = {
                str(member.get("ts_code") or member.get("symbol") or "").upper():
                _market_member_state(member)
                for member in json.loads(str(row["members_json"] or "[]"))
            }
            if expected != current:
                raise ValueError(f"market security history mismatch on {as_of_date}")
            checked += 1
    return checked


def _validate_watchlist_identity(source_path: Path, target_path: Path) -> int:
    with sqlite3.connect(f"file:{target_path.resolve()}?mode=ro", uri=True) as target:
        target.row_factory = sqlite3.Row
        changes = _rows_by_date(
            target.execute("SELECT * FROM security_identity_history ORDER BY valid_from, symbol"),
            "valid_from",
        )
    current: dict[str, tuple[Any, ...]] = {}
    checked = 0
    with sqlite3.connect(f"file:{source_path.resolve()}?mode=ro", uri=True) as source:
        source.row_factory = sqlite3.Row
        dates = [
            str(row[0]) for row in source.execute(
                "SELECT DISTINCT snapshot_date FROM security_identity_snapshot ORDER BY snapshot_date"
            )
        ]
        for as_of_date in dates:
            for change in changes.get(as_of_date, []):
                current[str(change["symbol"])] = _identity_state(change)
            expected = {
                str(row["symbol"]): _identity_state(row)
                for row in source.execute(
                    "SELECT * FROM security_identity_snapshot WHERE snapshot_date=?",
                    (as_of_date,),
                )
            }
            if expected != current:
                raise ValueError(f"watchlist identity history mismatch on {as_of_date}")
            checked += 1
    return checked


def _validate_watchlist_membership(source_path: Path, target_path: Path) -> int:
    with sqlite3.connect(f"file:{target_path.resolve()}?mode=ro", uri=True) as target:
        target.row_factory = sqlite3.Row
        history = [
            dict(row) for row in target.execute(
                "SELECT * FROM security_industry_membership_history"
            )
        ]
    checked = 0
    with sqlite3.connect(f"file:{source_path.resolve()}?mode=ro", uri=True) as source:
        source.row_factory = sqlite3.Row
        dates = [
            str(row[0]) for row in source.execute(
                """
                SELECT DISTINCT snapshot_date
                FROM security_industry_membership_snapshot ORDER BY snapshot_date
                """
            )
        ]
        for as_of_date in dates:
            expected = {
                _membership_state(row)
                for row in source.execute(
                    "SELECT * FROM security_industry_membership_snapshot WHERE snapshot_date=?",
                    (as_of_date,),
                )
            }
            actual = {
                _membership_state(row)
                for row in history
                if str(row["valid_from"]) <= as_of_date
                and (not str(row["valid_to"] or "") or as_of_date <= str(row["valid_to"]))
            }
            if expected != actual:
                raise ValueError(f"watchlist industry history mismatch on {as_of_date}")
            checked += 1
    return checked


def _rows_by_date(rows: Iterable[sqlite3.Row], column: str) -> dict[str, list[sqlite3.Row]]:
    result: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        result.setdefault(str(row[column]), []).append(row)
    return result


def _market_member_state(row: Any) -> tuple[Any, ...]:
    return (
        str(row.get("symbol") or str(row.get("ts_code") or "").split(".", 1)[0]),
        str(row.get("name") or ""), str(row.get("industry") or ""),
        str(row.get("market") or ""), str(row.get("list_date") or ""),
        str(row.get("delist_date") or ""), str(row.get("provider_list_status") or ""),
        str(row.get("listing_status_as_of") or ""), int(bool(row.get("is_st"))),
        str(row.get("st_status") or ""), str(row.get("st_type") or ""),
        int(bool(row.get("is_suspended"))), str(row.get("suspend_reason") or ""),
        str(row.get("trading_status") or ""),
    )


def _market_history_state(row: sqlite3.Row) -> tuple[Any, ...]:
    return (
        str(row["code"] or ""), str(row["name"] or ""), str(row["industry"] or ""),
        str(row["board"] or ""), str(row["list_date"] or ""),
        str(row["delist_date"] or ""), str(row["provider_list_status"] or ""),
        str(row["listing_status_as_of"] or ""), int(row["is_st"] or 0),
        str(row["st_status"] or ""), str(row["st_type"] or ""),
        int(row["is_suspended"] or 0), str(row["suspend_reason"] or ""),
        str(row["trading_status"] or ""),
    )


def _identity_state(row: Any) -> tuple[Any, ...]:
    return (
        str(row["name"] or ""), str(row["industry"] or ""),
        str(row["list_date"] or ""), int(row["is_st"] or 0),
        int(row["is_suspended"] or 0), str(row["listing_status"] or ""),
        str(row["trading_status"] or ""),
    )


def _membership_state(row: Any) -> tuple[str, ...]:
    return tuple(
        str(row[column] or "")
        for column in (
            "symbol", "taxonomy", "industry_level", "industry_code", "industry_name",
            "valid_from", "valid_to", "assignment_state", "source", "source_digest", "origin",
        )
    )


def _backup(source: Path, target: Path) -> None:
    with sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True) as source_db:
        with sqlite3.connect(target) as target_db:
            source_db.backup(target_db)


def _optimize_market(path: Path) -> dict[str, int]:
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        connection.executescript(COLLECTION_SCHEMA_SQL)
        tables = _tables(connection)
        if "security_master_snapshots" in tables:
            rows = connection.execute(
                """
                SELECT as_of_date, provider, source_version, members_json, fetched_at
                FROM security_master_snapshots
                WHERE market = 'ashare'
                ORDER BY as_of_date, created_at
                """
            )
            _migrate_market_history(connection, rows)
            connection.execute("DROP TABLE security_master_snapshots")
        history_count = int(
            connection.execute("SELECT COUNT(*) FROM security_master_history").fetchone()[0]
        )
        connection.commit()
        connection.execute("VACUUM")
    return {"security_history_count": history_count}


def _migrate_market_history(
    connection: sqlite3.Connection, rows: Iterable[sqlite3.Row]
) -> None:
    latest: dict[str, tuple[Any, ...]] = {}
    pending: list[tuple[Any, ...]] = []
    for snapshot in rows:
        as_of_date = str(snapshot["as_of_date"])
        members = json.loads(str(snapshot["members_json"] or "[]"))
        for member in sorted(members, key=lambda item: str(item.get("ts_code") or "")):
            symbol = str(member.get("ts_code") or member.get("symbol") or "").upper()
            if not symbol:
                continue
            state: tuple[Any, ...] = (
                str(member.get("symbol") or symbol.split(".", 1)[0]),
                str(member.get("name") or ""), str(member.get("industry") or ""),
                str(member.get("market") or ""), str(member.get("list_date") or ""),
                str(member.get("delist_date") or ""),
                str(member.get("provider_list_status") or ""),
                str(member.get("listing_status_as_of") or ""), int(bool(member.get("is_st"))),
                str(member.get("st_status") or ""), str(member.get("st_type") or ""),
                int(bool(member.get("is_suspended"))),
                str(member.get("suspend_reason") or ""),
                str(member.get("trading_status") or ""),
            )
            if latest.get(symbol) == state:
                continue
            digest = _digest({"symbol": symbol, "valid_from": as_of_date, "state": state})
            pending.append(
                (
                    as_of_date, symbol, *state, str(snapshot["provider"] or ""),
                    str(snapshot["source_version"] or ""), digest,
                    str(snapshot["fetched_at"] or ""),
                )
            )
            latest[symbol] = state
            if len(pending) >= 10_000:
                _insert_market_history(connection, pending)
                pending.clear()
    _insert_market_history(connection, pending)


def _insert_market_history(
    connection: sqlite3.Connection, rows: list[tuple[Any, ...]]
) -> None:
    if not rows:
        return
    connection.executemany(
        """
        INSERT INTO security_master_history (
            valid_from, symbol, code, name, industry, board, list_date, delist_date,
            provider_list_status, listing_status_as_of, is_st, st_status, st_type,
            is_suspended, suspend_reason, trading_status, provider, source_version,
            source_digest, fetched_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        rows,
    )


def _optimize_watchlist(path: Path) -> dict[str, int]:
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        connection.executescript(WATCHLIST_SCHEMA_SQL)
        tables = _tables(connection)
        if "security_identity_snapshot" in tables:
            _migrate_identity_history(connection)
        if "industry_dimension_snapshot" in tables:
            connection.execute(
                """
                INSERT OR IGNORE INTO industry_dimension_history (
                    valid_from, valid_to, taxonomy, industry_level, industry_code,
                    industry_name, parent_industry_code, source, source_version,
                    source_digest, origin
                )
                SELECT MIN(snapshot_date), '', taxonomy, industry_level, industry_code,
                       industry_name, parent_industry_code, source, source_version,
                       source_digest, origin
                FROM industry_dimension_snapshot
                GROUP BY taxonomy, industry_level, industry_code, industry_name,
                         parent_industry_code, source, source_version, source_digest, origin
                """
            )
        if "security_industry_membership_snapshot" in tables:
            connection.execute(
                """
                INSERT OR IGNORE INTO security_industry_membership_history (
                    symbol, taxonomy, industry_level, industry_code, industry_name,
                    valid_from, valid_to, assignment_state, source, source_digest, origin
                )
                SELECT symbol, taxonomy, industry_level, industry_code, industry_name,
                       valid_from, valid_to, assignment_state, source, source_digest, origin
                FROM security_industry_membership_snapshot
                """
            )
        for table in (
            "security_identity_snapshot",
            "industry_dimension_snapshot",
            "security_industry_membership_snapshot",
        ):
            if table in tables:
                connection.execute(f"DROP TABLE {table}")
        counts = {
            "identity_history_count": int(connection.execute(
                "SELECT COUNT(*) FROM security_identity_history"
            ).fetchone()[0]),
            "industry_dimension_history_count": int(connection.execute(
                "SELECT COUNT(*) FROM industry_dimension_history"
            ).fetchone()[0]),
            "industry_membership_history_count": int(connection.execute(
                "SELECT COUNT(*) FROM security_industry_membership_history"
            ).fetchone()[0]),
        }
        connection.commit()
        connection.execute("VACUUM")
    return counts


def _migrate_identity_history(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        """
        SELECT snapshot_date, symbol, name, industry, list_date, is_st,
               is_suspended, listing_status, trading_status, origin
        FROM security_identity_snapshot ORDER BY snapshot_date, symbol
        """
    )
    latest: dict[str, tuple[Any, ...]] = {}
    pending: list[tuple[Any, ...]] = []
    for row in rows:
        symbol = str(row["symbol"])
        state: tuple[Any, ...] = (
            str(row["name"] or ""), str(row["industry"] or ""),
            str(row["list_date"] or ""), int(row["is_st"] or 0),
            int(row["is_suspended"] or 0), str(row["listing_status"] or ""),
            str(row["trading_status"] or ""),
        )
        if latest.get(symbol) == state:
            continue
        valid_from = str(row["snapshot_date"])
        pending.append(
            (
                valid_from, symbol, *state,
                _digest({"symbol": symbol, "valid_from": valid_from, "state": state}),
                str(row["origin"]),
            )
        )
        latest[symbol] = state
        if len(pending) >= 10_000:
            _insert_identity_history(connection, pending)
            pending.clear()
    _insert_identity_history(connection, pending)


def _insert_identity_history(
    connection: sqlite3.Connection, rows: list[tuple[Any, ...]]
) -> None:
    if not rows:
        return
    connection.executemany(
        """
        INSERT INTO security_identity_history (
            valid_from, symbol, name, industry, list_date, is_st, is_suspended,
            listing_status, trading_status, source_digest, origin
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        rows,
    )


def _quick_check(path: Path) -> None:
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as connection:
        result = str(connection.execute("PRAGMA quick_check").fetchone()[0])
    if result != "ok":
        raise RuntimeError(f"SQLite quick_check failed for {path}: {result}")


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _digest(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
