"""Build compact SQLite deployment candidates without mutating source databases."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .collector import COLLECTION_SCHEMA_SQL
from .methods import completed_week_end_map
from .repository import WATCHLIST_SCHEMA_SQL


def optimize_databases(
    *,
    market_source: Path,
    market_target: Path,
    watchlist_source: Path,
    watchlist_target: Path,
    market_retention_days: int | None = None,
    watchlist_retention_days: int | None = None,
) -> dict[str, Any]:
    """Copy and normalize both public databases into new deployment candidates."""
    for source in (market_source, watchlist_source):
        if not source.is_file():
            raise FileNotFoundError(f"source database does not exist: {source}")
    for target in (market_target, watchlist_target):
        if target.exists():
            raise FileExistsError(f"target database already exists: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
    _validate_retention_days("market", market_retention_days, minimum=252)
    _validate_retention_days("watchlist", watchlist_retention_days, minimum=30)

    _backup(market_source, market_target)
    try:
        market_counts = _optimize_market(market_target, market_retention_days)
        _backup(watchlist_source, watchlist_target)
        watchlist_counts = _optimize_watchlist(
            watchlist_target,
            watchlist_retention_days,
            str(market_counts.get("weinstein_baseline_date") or ""),
        )
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
    market_retention_days: int | None = None,
    watchlist_retention_days: int | None = None,
) -> dict[str, Any]:
    """Prove that compact histories reproduce every legacy snapshot date."""
    for path in (market_source, market_target, watchlist_source, watchlist_target):
        if not path.is_file():
            raise FileNotFoundError(f"database does not exist: {path}")
        _quick_check(path)
    _validate_retention_days("market", market_retention_days, minimum=252)
    _validate_retention_days("watchlist", watchlist_retention_days, minimum=30)
    market_dates = _validate_market_history(market_source, market_target)
    identity_dates = _validate_watchlist_identity(watchlist_source, watchlist_target)
    membership_dates = _validate_watchlist_membership(watchlist_source, watchlist_target)
    if market_retention_days is not None or watchlist_retention_days is not None:
        return _validate_retained_candidate(
            market_source=market_source,
            market_target=market_target,
            watchlist_source=watchlist_source,
            watchlist_target=watchlist_target,
            market_retention_days=market_retention_days,
            watchlist_retention_days=watchlist_retention_days,
            market_history_dates=market_dates,
            identity_dates=identity_dates,
            membership_dates=membership_dates,
        )
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


def _validate_retained_candidate(
    *,
    market_source: Path,
    market_target: Path,
    watchlist_source: Path,
    watchlist_target: Path,
    market_retention_days: int | None,
    watchlist_retention_days: int | None,
    market_history_dates: int,
    identity_dates: int,
    membership_dates: int,
) -> dict[str, Any]:
    market_stats: dict[str, Any] = {}
    with sqlite3.connect(f"file:{market_target.resolve()}?mode=ro", uri=True) as target:
        target_dates = [
            str(row[0])
            for row in target.execute(
                """
                SELECT DISTINCT trade_date FROM daily_bars
                WHERE market='ashare' AND adj_type='qfq' ORDER BY trade_date
                """
            )
        ]
        if market_retention_days is not None and len(target_dates) != market_retention_days:
            raise ValueError(
                f"market retained date count mismatch: {len(target_dates)} "
                f"!= {market_retention_days}"
            )
        market_stats = {
            "retained_dates": len(target_dates),
            "cutoff": target_dates[0] if target_dates else "",
            "latest_date": target_dates[-1] if target_dates else "",
        }
    with sqlite3.connect(f"file:{market_source.resolve()}?mode=ro", uri=True) as source:
        latest = str(
            source.execute(
                """
                SELECT MAX(trade_date) FROM daily_bars
                WHERE market='ashare' AND adj_type='qfq'
                """
            ).fetchone()[0]
            or ""
        )
    if market_stats["latest_date"] != latest:
        raise ValueError("market candidate does not retain the latest source date")

    core_tables = {
        "stock_method_daily_fact": "as_of_date",
        "stock_method_transition": "as_of_date",
        "industry_observation_daily_fact": "as_of_date",
        "index_weinstein_weekly_fact": "effective_date",
        "index_minervini_stage2_daily_fact": "as_of_date",
    }
    core_counts: dict[str, int] = {}
    with sqlite3.connect(f"file:{watchlist_target.resolve()}?mode=ro", uri=True) as target:
        target_dates = [
            str(row[0])
            for row in target.execute(
                """
                SELECT DISTINCT as_of_date FROM stock_method_daily_fact
                ORDER BY as_of_date
                """
            )
        ]
        if (
            watchlist_retention_days is not None
            and len(target_dates) != watchlist_retention_days
        ):
            raise ValueError(
                f"watchlist retained date count mismatch: {len(target_dates)} "
                f"!= {watchlist_retention_days}"
            )
        cutoff = target_dates[0] if target_dates else ""
        latest_date = target_dates[-1] if target_dates else ""
        for table in core_tables:
            core_counts[table] = int(
                target.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
        lifecycle_baselines = int(
            target.execute(
                "SELECT COUNT(*) FROM stock_method_lifecycle_baseline"
            ).fetchone()[0]
        )
        stage_baselines = int(
            target.execute(
                "SELECT COUNT(*) FROM weinstein_stage_baseline"
            ).fetchone()[0]
        )
    with sqlite3.connect(f"file:{watchlist_source.resolve()}?mode=ro", uri=True) as source:
        source_latest = str(
            source.execute(
                "SELECT MAX(as_of_date) FROM stock_method_daily_fact"
            ).fetchone()[0]
            or ""
        )
        for table, date_column in core_tables.items():
            expected = int(
                source.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {date_column} >= ?",
                    (cutoff,),
                ).fetchone()[0]
            )
            if core_counts[table] != expected:
                raise ValueError(
                    f"retained row count mismatch for {table}: "
                    f"{core_counts[table]} != {expected}"
                )
    if latest_date != source_latest:
        raise ValueError("watchlist candidate does not retain the latest source date")
    if watchlist_retention_days is not None and lifecycle_baselines <= 0:
        raise ValueError("watchlist candidate is missing lifecycle baselines")
    if market_retention_days is not None and stage_baselines <= 0:
        raise ValueError("watchlist candidate is missing Weinstein stage baselines")
    return {
        "status": "RETENTION_EQUIVALENT",
        "market_snapshot_dates": market_history_dates,
        "watchlist_identity_dates": identity_dates,
        "watchlist_membership_dates": membership_dates,
        "market": market_stats,
        "watchlist": {
            "retained_dates": len(target_dates),
            "cutoff": cutoff,
            "latest_date": latest_date,
            "lifecycle_baselines": lifecycle_baselines,
            "weinstein_stage_baselines": stage_baselines,
        },
        "core_table_counts": core_counts,
    }


def _validate_market_history(source_path: Path, target_path: Path) -> int:
    with sqlite3.connect(f"file:{source_path.resolve()}?mode=ro", uri=True) as source:
        if "security_master_snapshots" not in _tables(source):
            return _validate_identical_history_table(
                source_path,
                target_path,
                "security_master_history",
                "valid_from, symbol",
            )
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
    with sqlite3.connect(f"file:{source_path.resolve()}?mode=ro", uri=True) as source:
        if "security_identity_snapshot" not in _tables(source):
            return _validate_identical_history_table(
                source_path,
                target_path,
                "security_identity_history",
                "valid_from, symbol",
            )
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
    with sqlite3.connect(f"file:{source_path.resolve()}?mode=ro", uri=True) as source:
        if "security_industry_membership_snapshot" not in _tables(source):
            return _validate_identical_history_table(
                source_path,
                target_path,
                "security_industry_membership_history",
                "valid_from, symbol, industry_code",
            )
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


def _validate_identical_history_table(
    source_path: Path,
    target_path: Path,
    table: str,
    order_by: str,
) -> int:
    with sqlite3.connect(f"file:{source_path.resolve()}?mode=ro", uri=True) as source:
        source_rows = (
            source.execute(f"SELECT * FROM {table} ORDER BY {order_by}").fetchall()
            if table in _tables(source)
            else []
        )
    with sqlite3.connect(f"file:{target_path.resolve()}?mode=ro", uri=True) as target:
        target_rows = (
            target.execute(f"SELECT * FROM {table} ORDER BY {order_by}").fetchall()
            if table in _tables(target)
            else []
        )
    if source_rows != target_rows:
        raise ValueError(f"history table mismatch for {table}")
    return len(source_rows)


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


def _optimize_market(path: Path, retention_days: int | None) -> dict[str, Any]:
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
        cutoff = ""
        if retention_days is not None:
            cutoff = _retention_cutoff(
                connection,
                table="daily_bars",
                date_column="trade_date",
                retention_days=retention_days,
                where="market='ashare' AND adj_type='qfq'",
            )
            for table in (
                "daily_bars",
                "daily_metrics",
                "daily_adjustment_factors",
                "market_index_daily_bars",
                "security_st_daily_fact",
            ):
                if table in tables:
                    connection.execute(
                        f"DELETE FROM {table} WHERE trade_date < ?", (cutoff,)
                    )
        trading_dates = [
            str(row[0])
            for row in connection.execute(
                """
                SELECT DISTINCT trade_date FROM daily_bars
                WHERE market='ashare' AND adj_type='qfq'
                ORDER BY trade_date
                """
            )
        ]
        week_ends = sorted(set(completed_week_end_map(trading_dates).values()))
        weinstein_baseline_date = week_ends[32] if len(week_ends) > 33 else ""
        retained_dates = len(trading_dates)
        connection.commit()
        connection.execute("VACUUM")
    return {
        "security_history_count": history_count,
        "retention_days": retention_days,
        "retention_cutoff": cutoff,
        "retained_dates": retained_dates,
        "weinstein_baseline_date": weinstein_baseline_date,
    }


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


def _optimize_watchlist(
    path: Path,
    retention_days: int | None,
    weinstein_baseline_date: str,
) -> dict[str, Any]:
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
        counts: dict[str, Any] = {
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
        cutoff = ""
        if retention_days is not None:
            cutoff = _retention_cutoff(
                connection,
                table="stock_method_daily_fact",
                date_column="as_of_date",
                retention_days=retention_days,
            )
            _populate_lifecycle_baselines(connection, cutoff)
            if weinstein_baseline_date:
                _populate_weinstein_stage_baselines(
                    connection, weinstein_baseline_date
                )
            _drop_retention_triggers(connection)
            for table, column in (
                ("stock_method_daily_fact", "as_of_date"),
                ("stock_method_transition", "as_of_date"),
                ("industry_observation_daily_fact", "as_of_date"),
                ("index_weinstein_weekly_fact", "effective_date"),
                ("index_minervini_stage2_daily_fact", "as_of_date"),
            ):
                if table in tables:
                    connection.execute(
                        f"DELETE FROM {table} WHERE {column} < ?", (cutoff,)
                    )
            connection.executescript(WATCHLIST_SCHEMA_SQL)
        counts.update(
            {
                "retention_days": retention_days,
                "retention_cutoff": cutoff,
                "retained_dates": int(
                    connection.execute(
                        "SELECT COUNT(DISTINCT as_of_date) FROM stock_method_daily_fact"
                    ).fetchone()[0]
                ),
                "lifecycle_baseline_count": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM stock_method_lifecycle_baseline"
                    ).fetchone()[0]
                ),
                "weinstein_baseline_count": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM weinstein_stage_baseline"
                    ).fetchone()[0]
                ),
            }
        )
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


def _validate_retention_days(
    label: str, value: int | None, *, minimum: int
) -> None:
    if value is not None and value < minimum:
        raise ValueError(f"{label} retention must be at least {minimum} trading days")


def _retention_cutoff(
    connection: sqlite3.Connection,
    *,
    table: str,
    date_column: str,
    retention_days: int,
    where: str = "",
) -> str:
    where_sql = f"WHERE {where}" if where else ""
    row = connection.execute(
        f"""
        SELECT MIN(retained_date) FROM (
            SELECT DISTINCT {date_column} AS retained_date
            FROM {table} {where_sql}
            ORDER BY retained_date DESC LIMIT ?
        )
        """,
        (retention_days,),
    ).fetchone()
    cutoff = str(row[0] or "") if row else ""
    if not cutoff:
        raise ValueError(f"cannot determine retention cutoff for {table}")
    return cutoff


def _populate_lifecycle_baselines(
    connection: sqlite3.Connection, cutoff: str
) -> None:
    connection.execute(
        """
        INSERT OR IGNORE INTO stock_method_lifecycle_baseline (
            symbol, method, policy_version, boundary_date, previous_result,
            ever_passed, first_qualified_on, streak_started_on,
            consecutive_sessions
        )
        WITH boundary_facts AS (
            SELECT f.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY symbol, method, policy_version
                       ORDER BY as_of_date DESC
                   ) AS row_number
            FROM stock_method_daily_fact AS f
            WHERE as_of_date < ?
        ), latest_transitions AS (
            SELECT t.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY symbol, method, policy_version
                       ORDER BY as_of_date DESC
                   ) AS row_number
            FROM stock_method_transition AS t
            WHERE as_of_date < ?
        )
        SELECT f.symbol, f.method, f.policy_version, f.as_of_date, f.result,
               CASE WHEN t.symbol IS NULL THEN 0 ELSE 1 END,
               t.first_qualified_on,
               CASE WHEN f.result='PASS' THEN t.streak_started_on END,
               CASE WHEN f.result='PASS' THEN COALESCE(t.consecutive_sessions, 0)
                    ELSE 0 END
        FROM boundary_facts AS f
        LEFT JOIN latest_transitions AS t
          ON t.symbol=f.symbol AND t.method=f.method
         AND t.policy_version=f.policy_version AND t.row_number=1
        WHERE f.row_number=1
        """,
        (cutoff, cutoff),
    )


def _populate_weinstein_stage_baselines(
    connection: sqlite3.Connection, boundary_date: str
) -> None:
    connection.execute(
        """
        INSERT OR IGNORE INTO weinstein_stage_baseline (
            instrument_type, symbol, boundary_effective_date, previous_stage,
            last_directional_stage, stage_started_on, duration_weeks,
            policy_version
        )
        WITH ranked AS (
            SELECT f.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY symbol, policy_version ORDER BY as_of_date DESC
                   ) AS row_number
            FROM stock_method_daily_fact AS f
            WHERE method='weinstein' AND as_of_date <= ?
        ), decoded AS (
            SELECT *,
                   COALESCE(NULLIF(json_extract(
                       evidence_json, '$.profile.effective_week_end'
                   ), ''), as_of_date) AS effective_week_end,
                   COALESCE(json_extract(
                       evidence_json, '$.profile.stage'
                   ), 'UNKNOWN') AS stage,
                   COALESCE(json_extract(
                       evidence_json, '$.profile.stage_started_on'
                   ), '') AS stage_started_on,
                   COALESCE(json_extract(
                       evidence_json, '$.profile.duration_weeks'
                   ), 0) AS duration_weeks,
                   COALESCE(json_extract(
                       evidence_json, '$.profile.metrics.prior_directional_stage'
                   ), '') AS prior_directional_stage
            FROM ranked WHERE row_number=1
        )
        SELECT 'stock', symbol, effective_week_end, stage,
               CASE WHEN stage IN ('STAGE_2','STAGE_4') THEN stage
                    WHEN prior_directional_stage IN ('STAGE_2','STAGE_4')
                    THEN prior_directional_stage ELSE '' END,
               stage_started_on, duration_weeks, policy_version
        FROM decoded
        WHERE stage_started_on <> ''
        """,
        (boundary_date,),
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO weinstein_stage_baseline (
            instrument_type, symbol, boundary_effective_date, previous_stage,
            last_directional_stage, stage_started_on, duration_weeks,
            policy_version
        )
        WITH ranked AS (
            SELECT f.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY index_symbol, policy_version
                       ORDER BY effective_date DESC
                   ) AS row_number
            FROM index_weinstein_weekly_fact AS f
            WHERE effective_date <= ?
        )
        SELECT 'index', index_symbol, effective_date, stage,
               CASE WHEN stage IN ('STAGE_2','STAGE_4') THEN stage
                    WHEN json_extract(evidence_json, '$.prior_directional_stage')
                         IN ('STAGE_2','STAGE_4')
                    THEN json_extract(evidence_json, '$.prior_directional_stage')
                    ELSE '' END,
               stage_started_on, duration_weeks, policy_version
        FROM ranked WHERE row_number=1
        """,
        (boundary_date,),
    )


def _drop_retention_triggers(connection: sqlite3.Connection) -> None:
    for trigger in (
        "trg_stock_method_daily_fact_no_delete",
        "trg_stock_method_transition_no_delete",
        "trg_index_weinstein_weekly_fact_no_delete",
        "trg_index_minervini_stage2_daily_fact_no_delete",
        "trg_industry_observation_daily_fact_no_delete",
    ):
        connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")


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
