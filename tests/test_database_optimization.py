from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

from master_stock_selector.watchlist.database_optimization import (
    _drop_retention_triggers,
    _populate_weinstein_stage_baselines,
    optimize_databases,
    validate_database_equivalence,
)
from master_stock_selector.watchlist.methods import (
    MINERVINI_POLICY_VERSION,
    WEINSTEIN_POLICY_VERSION,
)
from master_stock_selector.watchlist.repository import (
    WATCHLIST_SCHEMA_SQL,
    MarketDataReader,
    WatchlistRepository,
)


def test_builds_compact_temporal_candidates_without_mutating_sources(tmp_path) -> None:
    market_source = tmp_path / "market-source.sqlite3"
    market_target = tmp_path / "market-target.sqlite3"
    watchlist_source = tmp_path / "watchlist-source.sqlite3"
    watchlist_target = tmp_path / "watchlist-target.sqlite3"
    _seed_legacy_market(market_source)
    _seed_legacy_watchlist(watchlist_source)

    result = optimize_databases(
        market_source=market_source,
        market_target=market_target,
        watchlist_source=watchlist_source,
        watchlist_target=watchlist_target,
    )

    assert result["market"]["security_history_count"] == 2
    assert result["watchlist"]["identity_history_count"] == 2
    assert result["watchlist"]["industry_membership_history_count"] == 1
    with sqlite3.connect(market_source) as connection:
        assert connection.execute("SELECT COUNT(*) FROM security_master_snapshots").fetchone()[0] == 2
    with sqlite3.connect(watchlist_source) as connection:
        assert connection.execute("SELECT COUNT(*) FROM security_identity_snapshot").fetchone()[0] == 2
    with sqlite3.connect(market_target) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "security_master_history" in tables
        assert "security_master_snapshots" not in tables
    with sqlite3.connect(watchlist_target) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "security_identity_history" in tables
        assert "security_identity_snapshot" not in tables

    assert MarketDataReader(market_target).security_members("2026-08-01")["000001.SZ"]["name"] == "旧名"
    assert MarketDataReader(market_target).security_members("2026-08-02")["000001.SZ"]["name"] == "新名"
    assert WatchlistRepository(watchlist_target).stock_names({"000001.SZ"}) == {"000001.SZ": "新名"}
    validation = validate_database_equivalence(
        market_source=market_source,
        market_target=market_target,
        watchlist_source=watchlist_source,
        watchlist_target=watchlist_target,
    )
    assert validation["status"] == "EQUIVALENT"
    assert validation["market_snapshot_dates"] == 2


def test_retention_candidate_keeps_320_market_and_130_watchlist_days_with_baselines(
    tmp_path,
) -> None:
    market_source = tmp_path / "market-source.sqlite3"
    market_target = tmp_path / "market-target.sqlite3"
    watchlist_source = tmp_path / "watchlist-source.sqlite3"
    watchlist_target = tmp_path / "watchlist-target.sqlite3"
    trading_dates = _weekday_dates(330)
    _seed_retention_market(market_source, trading_dates)
    _seed_retention_watchlist(watchlist_source, trading_dates)

    result = optimize_databases(
        market_source=market_source,
        market_target=market_target,
        watchlist_source=watchlist_source,
        watchlist_target=watchlist_target,
        market_retention_days=320,
        watchlist_retention_days=130,
    )

    assert result["market"]["retained_dates"] == 320
    assert result["watchlist"]["retained_dates"] == 130
    assert result["watchlist"]["lifecycle_baseline_count"] == 2
    assert result["watchlist"]["weinstein_baseline_count"] >= 1
    assert sqlite3.connect(market_source).execute(
        "SELECT COUNT(DISTINCT trade_date) FROM daily_bars"
    ).fetchone()[0] == 330
    assert sqlite3.connect(watchlist_source).execute(
        "SELECT COUNT(DISTINCT as_of_date) FROM stock_method_daily_fact"
    ).fetchone()[0] == 330

    validation = validate_database_equivalence(
        market_source=market_source,
        market_target=market_target,
        watchlist_source=watchlist_source,
        watchlist_target=watchlist_target,
        market_retention_days=320,
        watchlist_retention_days=130,
    )
    assert validation["status"] == "RETENTION_EQUIVALENT"

    repository = WatchlistRepository(watchlist_target)
    next_date = (date.fromisoformat(trading_dates[-1]) + timedelta(days=1)).isoformat()
    repository.persist_run(
        stock_facts=[
            {
                "as_of_date": next_date,
                "symbol": "000001.SZ",
                "method": "weinstein",
                "result": "PASS",
                "policy_version": WEINSTEIN_POLICY_VERSION,
                "evidence": {},
                "source_digest": "future",
                "origin": "RECONSTRUCTED",
            },
            {
                "as_of_date": next_date,
                "symbol": "000001.SZ",
                "method": "minervini",
                "result": "PASS",
                "policy_version": MINERVINI_POLICY_VERSION,
                "evidence": {},
                "source_digest": "future",
                "origin": "RECONSTRUCTED",
            },
        ],
        index_facts=[],
        receipt={
            "run_id": "future-run",
            "as_of_date": next_date,
            "from_date": next_date,
            "origin": "RECONSTRUCTED",
            "minervini_policy_version": MINERVINI_POLICY_VERSION,
            "weinstein_policy_version": WEINSTEIN_POLICY_VERSION,
            "market_database": str(market_target),
            "source_digest": "future",
            "counts": {},
            "status": "SUCCESS",
            "started_at": next_date,
            "finished_at": next_date,
        },
    )
    with sqlite3.connect(watchlist_target) as connection:
        continuing = connection.execute(
            """
            SELECT state, streak_started_on, consecutive_sessions
            FROM stock_method_transition
            WHERE as_of_date=? AND method='weinstein'
            """,
            (next_date,),
        ).fetchone()
        reentered = connection.execute(
            """
            SELECT state, first_qualified_on FROM stock_method_transition
            WHERE as_of_date=? AND method='minervini'
            """,
            (next_date,),
        ).fetchone()
    assert continuing == ("CONTINUING", trading_dates[0], 331)
    assert reentered == ("REENTERED", trading_dates[0])


def test_retention_candidate_temporarily_drops_every_pruned_table_delete_guard(
    tmp_path,
) -> None:
    path = tmp_path / "watchlist.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(WATCHLIST_SCHEMA_SQL)
        _drop_retention_triggers(connection)
        remaining = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type='trigger' AND name LIKE '%no_delete'
                """
            )
        }

    assert not remaining.intersection(
        {
            "trg_stock_method_daily_fact_no_delete",
            "trg_stock_method_transition_no_delete",
            "trg_index_weinstein_weekly_fact_no_delete",
            "trg_index_minervini_stage2_daily_fact_no_delete",
            "trg_industry_observation_daily_fact_no_delete",
        }
    )


def test_weinstein_baseline_skips_pre_listing_unknown_without_stage_start(
    tmp_path,
) -> None:
    path = tmp_path / "watchlist.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(WATCHLIST_SCHEMA_SQL)
        connection.executemany(
            """
            INSERT INTO stock_method_daily_fact (
                as_of_date,symbol,method,result,policy_version,evidence_json,
                source_digest,origin
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            [
                (
                    "2025-11-28", "000001.SZ", "weinstein", "PASS",
                    WEINSTEIN_POLICY_VERSION,
                    json.dumps({"profile": {
                        "stage": "STAGE_2", "stage_started_on": "2025-06-06",
                        "duration_weeks": 26, "effective_week_end": "2025-11-28",
                        "metrics": {"prior_directional_stage": "STAGE_2"},
                    }}),
                    "digest", "RECONSTRUCTED",
                ),
                (
                    "2025-11-28", "001220.SZ", "weinstein", "UNKNOWN",
                    WEINSTEIN_POLICY_VERSION,
                    json.dumps({"profile": {
                        "stage": "UNKNOWN", "stage_started_on": "",
                        "duration_weeks": 0, "effective_week_end": "2025-11-28",
                        "metrics": {},
                    }}),
                    "digest", "RECONSTRUCTED",
                ),
            ],
        )
        _populate_weinstein_stage_baselines(connection, "2025-11-28")
        symbols = {
            str(row[0])
            for row in connection.execute(
                "SELECT symbol FROM weinstein_stage_baseline"
            )
        }

    assert symbols == {"000001.SZ"}


def _seed_legacy_market(path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE daily_bars (
                market TEXT, adj_type TEXT, symbol TEXT, trade_date TEXT, amount REAL
            );
            CREATE TABLE security_master_snapshots (
                snapshot_id TEXT PRIMARY KEY, run_id TEXT, market TEXT, as_of_date TEXT,
                provider TEXT, source_version TEXT, symbol_count INTEGER, symbols_hash TEXT,
                symbols_json TEXT, members_json TEXT, fetched_at TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        for as_of_date, name in (("2026-08-01", "旧名"), ("2026-08-02", "新名")):
            member = {
                "ts_code": "000001.SZ", "symbol": "000001", "name": name,
                "industry": "银行", "market": "主板", "list_date": "1991-04-03",
                "provider_list_status": "L", "listing_status_as_of": "listed",
                "is_st": False, "st_status": "normal", "is_suspended": False,
                "trading_status": "trading",
            }
            connection.execute(
                "INSERT INTO security_master_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                (
                    f"snapshot-{as_of_date}", f"run-{as_of_date}", "ashare", as_of_date,
                    "provider", "v1", 1, "hash", '["000001.SZ"]',
                    json.dumps([member], ensure_ascii=False), as_of_date,
                ),
            )


def _seed_legacy_watchlist(path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE security_identity_snapshot (
                snapshot_date TEXT, symbol TEXT, name TEXT, industry TEXT, list_date TEXT,
                is_st INTEGER, is_suspended INTEGER, listing_status TEXT,
                trading_status TEXT, origin TEXT
            );
            CREATE TABLE industry_dimension_snapshot (
                snapshot_date TEXT, taxonomy TEXT, industry_level TEXT, industry_code TEXT,
                industry_name TEXT, parent_industry_code TEXT, source TEXT,
                source_version TEXT, source_digest TEXT, origin TEXT
            );
            CREATE TABLE security_industry_membership_snapshot (
                snapshot_date TEXT, symbol TEXT, taxonomy TEXT, industry_level TEXT,
                industry_code TEXT, industry_name TEXT, valid_from TEXT, valid_to TEXT,
                assignment_state TEXT, source TEXT, source_digest TEXT, origin TEXT
            );
            """
        )
        connection.executemany(
            "INSERT INTO security_identity_snapshot VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                ("2026-08-01", "000001.SZ", "旧名", "银行", "1991-04-03", 0, 0, "L", "trading", "RECONSTRUCTED"),
                ("2026-08-02", "000001.SZ", "新名", "银行", "1991-04-03", 0, 0, "L", "trading", "RECONSTRUCTED"),
            ],
        )
        connection.execute(
            "INSERT INTO industry_dimension_snapshot VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("2026-08-01", "SW2021", "L3", "850001.SI", "银行", "", "source", "v1", "dimension", "RECONSTRUCTED"),
        )
        connection.execute(
            "INSERT INTO security_industry_membership_snapshot VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("2026-08-01", "000001.SZ", "SW2021", "L3", "850001.SI", "银行", "2026-01-01", "", "VERIFIED", "source", "membership", "RECONSTRUCTED"),
        )


def _weekday_dates(count: int) -> list[str]:
    values: list[str] = []
    current = date(2025, 1, 2)
    while len(values) < count:
        if current.weekday() < 5:
            values.append(current.isoformat())
        current += timedelta(days=1)
    return values


def _seed_retention_market(path, trading_dates: list[str]) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE daily_bars (
                market TEXT, adj_type TEXT, symbol TEXT, trade_date TEXT, amount REAL
            )
            """
        )
        connection.executemany(
            "INSERT INTO daily_bars VALUES ('ashare','qfq','000001.SZ',?,100)",
            [(value,) for value in trading_dates],
        )


def _seed_retention_watchlist(path, trading_dates: list[str]) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(WATCHLIST_SCHEMA_SQL)
        weinstein_facts = []
        minervini_facts = []
        transitions = []
        for index, value in enumerate(trading_dates, start=1):
            profile = {
                "profile": {
                    "stage": "STAGE_2",
                    "stage_started_on": trading_dates[0],
                    "duration_weeks": max(1, index // 5),
                    "effective_week_end": value,
                    "metrics": {"prior_directional_stage": "STAGE_2"},
                }
            }
            weinstein_facts.append(
                (
                    value,
                    "000001.SZ",
                    "weinstein",
                    "PASS",
                    WEINSTEIN_POLICY_VERSION,
                    json.dumps(profile),
                    "digest",
                    "RECONSTRUCTED",
                )
            )
            minervini_result = "PASS" if index == 1 else "FAIL"
            minervini_facts.append(
                (
                    value,
                    "000001.SZ",
                    "minervini",
                    minervini_result,
                    MINERVINI_POLICY_VERSION,
                    "{}",
                    "digest",
                    "RECONSTRUCTED",
                )
            )
            transitions.append(
                (
                    value,
                    "000001.SZ",
                    "weinstein",
                    "ENTERED" if index == 1 else "CONTINUING",
                    WEINSTEIN_POLICY_VERSION,
                    trading_dates[0],
                    trading_dates[0],
                    index,
                    "",
                    "RECONSTRUCTED",
                )
            )
        transitions.extend(
            [
                (
                    trading_dates[0],
                    "000001.SZ",
                    "minervini",
                    "ENTERED",
                    MINERVINI_POLICY_VERSION,
                    trading_dates[0],
                    trading_dates[0],
                    1,
                    "",
                    "RECONSTRUCTED",
                ),
                (
                    trading_dates[1],
                    "000001.SZ",
                    "minervini",
                    "EXITED",
                    MINERVINI_POLICY_VERSION,
                    trading_dates[0],
                    None,
                    0,
                    "",
                    "RECONSTRUCTED",
                ),
            ]
        )
        connection.executemany(
            """
            INSERT INTO stock_method_daily_fact (
                as_of_date,symbol,method,result,policy_version,evidence_json,
                source_digest,origin
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            [*weinstein_facts, *minervini_facts],
        )
        connection.executemany(
            """
            INSERT INTO stock_method_transition (
                as_of_date,symbol,method,state,policy_version,first_qualified_on,
                streak_started_on,consecutive_sessions,reason,origin
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            transitions,
        )
