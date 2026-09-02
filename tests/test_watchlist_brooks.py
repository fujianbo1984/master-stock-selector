from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from master_stock_selector.watchlist.brooks import (
    BROOKS_POLICY_VERSION,
    BrooksBar,
    BrooksImpulse,
    SecurityState,
    build_brooks_daily_facts,
    build_brooks_impulses,
    impulse_row,
)
from master_stock_selector.watchlist.brooks_service import BrooksRunConfig, run_brooks_watchlist
from master_stock_selector.watchlist.repository import WatchlistRepository
from master_stock_selector.web.app import create_app
from master_stock_selector.web.users import UserRepository


def _dates(count: int) -> list[str]:
    values: list[str] = []
    current = date(2025, 1, 2)
    while len(values) < count:
        if current.weekday() < 5:
            values.append(current.isoformat())
        current += timedelta(days=1)
    return values


def _bar(
    trade_date: str,
    close: float,
    *,
    open_price: float | None = None,
    high: float | None = None,
    low: float | None = None,
) -> BrooksBar:
    open_value = close - 0.3 if open_price is None else open_price
    high_value = close + 0.1 if high is None else high
    low_value = min(open_value, close) - 0.1 if low is None else low
    return BrooksBar(
        trade_date=trade_date,
        open=open_value,
        high=high_value,
        low=low_value,
        close=close,
        amount=30_000.0,
        raw_open=open_value,
        raw_high=high_value,
        raw_low=low_value,
        raw_close=close,
        pre_close=close - 0.1,
        source_token=trade_date,
    )


def test_brooks_impulse_uses_frozen_longest_window_and_quality_formulae() -> None:
    dates = _dates(142)
    bars: list[BrooksBar] = []
    for index, trade_date in enumerate(dates[:134]):
        close = 100.0 + (0.2 if index % 2 else 0.0)
        bars.append(
            _bar(
                trade_date,
                close,
                open_price=close + (0.1 if index % 2 else -0.1),
                high=close + 0.5,
                low=close - 0.5,
            )
        )
    start_close = bars[-1].close
    for offset, trade_date in enumerate(dates[134:], 1):
        close = start_close + offset * 0.5
        bars.append(
            _bar(
                trade_date,
                close,
                open_price=close - 0.3,
                high=close + 0.1,
                low=close - 0.4,
            )
        )

    events = build_brooks_impulses(
        "000001.SZ",
        bars,
        [SecurityState(valid_from=dates[0])],
    )

    event = events[-1]
    assert event.duration_bars == 8
    assert event.signal_date in dates[-4:]
    assert event.move_atr >= 3.0
    assert event.path_efficiency >= 0.75
    assert event.bullish_body_count >= 5
    assert event.mean_close_location >= 0.65
    assert event.mean_overlap <= 0.35


def test_brooks_state_machine_moves_touch25_to_h1_and_completion_without_lookahead() -> None:
    dates = _dates(16)
    bars = [_bar(value, 90.0 + index) for index, value in enumerate(dates[:11])]
    bars[10] = _bar(dates[10], 100.0, high=101.0, low=99.0)
    bars.extend(
        [
            _bar(dates[11], 97.4, open_price=98.0, high=98.5, low=97.0),
            _bar(dates[12], 98.5, open_price=97.2, high=99.2, low=97.1),
            _bar(dates[13], 100.0, open_price=99.0, high=102.0, low=98.8),
            _bar(dates[14], 106.0, open_price=101.0, high=108.0, low=100.5),
            _bar(dates[15], 107.0, open_price=106.0, high=108.0, low=105.0),
        ]
    )
    event = BrooksImpulse(
        event_id="brooks|000001.SZ|test|8",
        symbol="000001.SZ",
        signal_pos=10,
        start_pos=2,
        signal_date=dates[10],
        start_date=dates[2],
        duration_bars=8,
        impulse_start=90.0,
        signal_close=100.0,
        signal_high=101.0,
        impulse_height=10.0,
        atr20=2.0,
        move_atr=5.0,
        path_efficiency=0.9,
        bullish_body_count=7,
        bullish_body_share=0.875,
        mean_close_location=0.8,
        mean_overlap=0.1,
        speed_atr_per_session=0.625,
        median_amount20=30_000.0,
        source_digest="event-digest",
    )

    facts = build_brooks_daily_facts(
        event,
        bars,
        [SecurityState(valid_from=dates[0])],
        [dates[10], dates[11], dates[12], dates[14]],
        origin="RECONSTRUCTED",
    )
    by_date = {row["as_of_date"]: row for row in facts}

    assert by_date[dates[10]]["state"] == "S0_IMPULSE"
    assert by_date[dates[11]]["state"] == "S1_TOUCH25"
    assert by_date[dates[11]]["next_open_status"] == "PENDING"
    assert by_date[dates[12]]["state"] == "S2_H1"
    assert by_date[dates[12]]["next_open_status"] == "PENDING"
    assert by_date[dates[14]]["state"] == "COMPLETED"
    assert by_date[dates[14]]["prior_high_touched"] is True
    assert by_date[dates[14]]["mm_1_0_completed"] is True


def test_chutianlong_first_leg_expands_after_three_day_trigger_and_freezes_at_touch25() -> None:
    dates = [
        "2026-08-19",
        "2026-08-20",
        "2026-08-21",
        "2026-08-24",
        "2026-08-25",
        "2026-08-26",
        "2026-08-27",
        "2026-08-28",
        "2026-08-31",
    ]
    closes = [10.85, 11.63, 12.79, 14.07, 15.48, 17.03, 18.73, 16.70, 20.00]
    bars = [
        _bar(
            trade_date,
            close,
            open_price=close - 0.2,
            high=close + 0.2,
            low=close - 0.3,
        )
        for trade_date, close in zip(dates, closes, strict=True)
    ]
    event = BrooksImpulse(
        event_id="brooks|003040.SZ|2026-08-24|3",
        symbol="003040.SZ",
        signal_pos=3,
        start_pos=0,
        signal_date="2026-08-24",
        start_date="2026-08-19",
        duration_bars=3,
        impulse_start=10.85,
        signal_close=14.07,
        signal_high=14.27,
        impulse_height=3.22,
        atr20=0.65,
        move_atr=4.95,
        path_efficiency=1.0,
        bullish_body_count=3,
        bullish_body_share=1.0,
        mean_close_location=0.8,
        mean_overlap=0.0,
        speed_atr_per_session=1.65,
        median_amount20=100_000.0,
        source_digest="chutianlong-trigger",
    )

    facts = build_brooks_daily_facts(
        event,
        bars,
        [SecurityState(valid_from=dates[0])],
        dates[3:],
        origin="RECONSTRUCTED",
    )
    by_date = {row["as_of_date"]: row for row in facts}

    trigger = by_date["2026-08-24"]["evidence"]
    expanded = by_date["2026-08-27"]["evidence"]
    frozen = by_date["2026-08-28"]["evidence"]
    after_touch = by_date["2026-08-31"]["evidence"]

    assert trigger["trigger_window_bars"] == 3
    assert trigger["effective_leg_bars"] == 3
    assert expanded["effective_leg_bars"] == 6
    assert expanded["effective_leg_start_date"] == "2026-08-20"
    assert expanded["effective_leg_end_date"] == "2026-08-27"
    assert expanded["max_consecutive_up_bars"] == 6
    assert expanded["max_consecutive_up_start"] == "2026-08-20"
    assert expanded["max_consecutive_up_end"] == "2026-08-27"
    assert expanded["leg_status"] == "EXPANDING"

    assert by_date["2026-08-28"]["state"] == "S1_TOUCH25"
    assert frozen["leg_status"] == "FROZEN_TOUCH25"
    assert frozen["effective_leg_end_date"] == "2026-08-27"
    assert after_touch["effective_leg_end_date"] == "2026-08-27"
    assert after_touch["effective_signal_close"] == 18.73
    assert by_date["2026-08-31"]["prior_high"] == 18.93


def test_brooks_h2_is_only_a_tag_and_same_bar_target_failure_stays_ambiguous() -> None:
    dates = _dates(15)
    base = [_bar(value, 90.0 + index) for index, value in enumerate(dates[:11])]
    base[10] = _bar(dates[10], 100.0, high=101.0, low=99.0)
    event = BrooksImpulse(
        event_id="brooks|000001.SZ|h2|8",
        symbol="000001.SZ",
        signal_pos=10,
        start_pos=2,
        signal_date=dates[10],
        start_date=dates[2],
        duration_bars=8,
        impulse_start=90.0,
        signal_close=100.0,
        signal_high=101.0,
        impulse_height=10.0,
        atr20=2.0,
        move_atr=5.0,
        path_efficiency=0.9,
        bullish_body_count=7,
        bullish_body_share=0.875,
        mean_close_location=0.8,
        mean_overlap=0.1,
        speed_atr_per_session=0.625,
        median_amount20=30_000.0,
        source_digest="event-digest",
    )
    h2_bars = [
        *base,
        _bar(dates[11], 97.4, open_price=98.0, high=98.5, low=97.0),
        _bar(dates[12], 98.5, open_price=97.2, high=99.2, low=97.1),
        _bar(dates[13], 99.2, open_price=96.8, high=100.0, low=96.5),
        _bar(dates[14], 99.5, open_price=99.0, high=100.0, low=98.5),
    ]
    h2_fact = build_brooks_daily_facts(
        event,
        h2_bars,
        [SecurityState(valid_from=dates[0])],
        [dates[13]],
        origin="RECONSTRUCTED",
    )[0]
    assert h2_fact["state"] == "S2_H1_H2_TAG"
    assert h2_fact["state_rank"] == 1

    ambiguous_bars = [
        *base,
        _bar(dates[11], 97.4, open_price=98.0, high=98.5, low=97.0),
        _bar(dates[12], 98.5, open_price=97.2, high=99.2, low=97.1),
        _bar(dates[13], 95.0, open_price=98.0, high=102.0, low=89.0),
        _bar(dates[14], 96.0, open_price=95.0, high=97.0, low=94.0),
    ]
    ambiguous_fact = build_brooks_daily_facts(
        event,
        ambiguous_bars,
        [SecurityState(valid_from=dates[0])],
        [dates[13]],
        origin="RECONSTRUCTED",
    )[0]
    assert ambiguous_fact["state"] == "INVALIDATED"
    assert ambiguous_fact["evidence"]["ambiguous_same_bar"] is True


def test_brooks_path_is_censored_after_60_sessions_and_late_failure_still_invalidates() -> None:
    dates = _dates(75)
    bars = [_bar(value, 90.0 + index) for index, value in enumerate(dates[:11])]
    bars[10] = _bar(dates[10], 100.0, high=101.0, low=99.0)
    bars.extend(
        [
            _bar(dates[11], 97.4, open_price=98.0, high=98.5, low=97.0),
            _bar(dates[12], 98.5, open_price=97.2, high=99.2, low=97.1),
        ]
    )
    bars.extend(_bar(value, 98.4, open_price=98.2, high=99.0, low=97.5) for value in dates[13:])
    event = BrooksImpulse(
        event_id="brooks|000001.SZ|censor|8",
        symbol="000001.SZ",
        signal_pos=10,
        start_pos=2,
        signal_date=dates[10],
        start_date=dates[2],
        duration_bars=8,
        impulse_start=90.0,
        signal_close=100.0,
        signal_high=101.0,
        impulse_height=10.0,
        atr20=2.0,
        move_atr=5.0,
        path_efficiency=0.9,
        bullish_body_count=7,
        bullish_body_share=0.875,
        mean_close_location=0.8,
        mean_overlap=0.1,
        speed_atr_per_session=0.625,
        median_amount20=30_000.0,
        source_digest="event-digest",
    )

    facts = build_brooks_daily_facts(
        event,
        bars,
        [SecurityState(valid_from=dates[0])],
        [dates[72], dates[73]],
        origin="RECONSTRUCTED",
    )
    assert facts[0]["state"] == "S2_H1"
    assert facts[1]["state"] == "STALE"
    assert facts[1]["state_reason"] == "STALE_PATH_CENSORED_60D"

    late_bars = list(bars)
    late_bars[12] = _bar(
        dates[12], 97.8, open_price=98.2, high=98.8, low=97.4
    )
    late_bars[26] = _bar(
        dates[26], 99.2, open_price=97.6, high=99.5, low=97.5
    )
    late = build_brooks_daily_facts(
        event,
        late_bars,
        [SecurityState(valid_from=dates[0])],
        [dates[26]],
        origin="RECONSTRUCTED",
    )[0]
    assert late["state"] == "S1_TOUCH25"
    assert late["evidence"]["late_h1_diagnostic_date"] == dates[26]

    failed_bars = list(bars)
    failed_bars[45] = _bar(dates[45], 91.0, open_price=98.0, high=98.5, low=90.0)
    failed = build_brooks_daily_facts(
        event,
        failed_bars,
        [SecurityState(valid_from=dates[0])],
        [dates[45]],
        origin="RECONSTRUCTED",
    )[0]
    assert failed["state"] == "INVALIDATED"
    assert failed["state_reason"] == "STRUCTURAL_FAILURE_AFTER_DECISION"

    missing_bars = [bar for index, bar in enumerate(bars) if index != 13]
    missing = build_brooks_daily_facts(
        event,
        missing_bars,
        [SecurityState(valid_from=dates[0])],
        [dates[13]],
        origin="RECONSTRUCTED",
    )[0]
    assert missing["state"] == "PAUSED"
    assert missing["next_open_status"] == "REJECTED_MISSING"


def _event_and_fact() -> tuple[dict[str, object], dict[str, object]]:
    event = BrooksImpulse(
        event_id="brooks|000001.SZ|2026-07-31|8",
        symbol="000001.SZ",
        signal_pos=20,
        start_pos=12,
        signal_date="2026-07-31",
        start_date="2026-07-21",
        duration_bars=8,
        impulse_start=10.0,
        signal_close=14.0,
        signal_high=14.2,
        impulse_height=4.0,
        atr20=1.0,
        move_atr=4.0,
        path_efficiency=0.9,
        bullish_body_count=7,
        bullish_body_share=0.875,
        mean_close_location=0.8,
        mean_overlap=0.1,
        speed_atr_per_session=0.5,
        median_amount20=100_000.0,
        source_digest="event-digest",
    )
    fact: dict[str, object] = {
        "as_of_date": "2026-07-31",
        "event_id": event.event_id,
        "symbol": event.symbol,
        "state": "S1_TOUCH25",
        "state_reason": "TOUCH25_CONFIRMED",
        "state_rank": 2,
        "state_changed_on": "2026-07-31",
        "touch25_date": "2026-07-31",
        "h1_date": "",
        "h1_failure_date": "",
        "h2_date": "",
        "bars_after_signal": 1,
        "bars_after_touch25": 0,
        "current_retracement": 0.25,
        "maximum_low_retracement": 0.3,
        "pullback_atr": 1.2,
        "pullback_low_anchor": 12.8,
        "prior_high": 14.2,
        "mm_0_8": 16.0,
        "mm_1_0": 16.8,
        "tight_invalidation": 12.8,
        "structural_invalidation": 10.0,
        "prior_high_touched": False,
        "prior_high_closed": False,
        "mm_0_8_completed": False,
        "mm_1_0_completed": False,
        "next_open_status": "PENDING",
        "tradability_reason": "",
        "evidence": {
            "current_close": 13.0,
            "trigger_window_bars": 8,
            "trigger_date": "2026-07-31",
            "effective_leg_start_date": "2026-07-22",
            "effective_leg_end_date": "2026-07-31",
            "effective_leg_bars": 8,
            "effective_move_atr": 4.0,
            "leg_frozen": True,
            "leg_status": "FROZEN_TOUCH25",
            "max_consecutive_up_bars": 6,
            "max_consecutive_up_start": "2026-07-24",
            "max_consecutive_up_end": "2026-07-31",
        },
        "source_digest": "fact-digest",
        "origin": "RECONSTRUCTED",
        "policy_version": BROOKS_POLICY_VERSION,
    }
    return impulse_row(event, origin="RECONSTRUCTED"), fact


def test_brooks_schema_is_explicit_and_repository_keeps_facts_separate(
    tmp_path: Path,
) -> None:
    repository = WatchlistRepository(tmp_path / "watchlist.sqlite3")
    repository.initialize()
    assert repository.brooks_schema_ready() is False

    migration = repository.migrate_brooks_schema()
    event, fact = _event_and_fact()
    repository.persist_brooks_run(
        events=[event],
        facts=[fact],
        receipt={
            "run_id": "brooks-test",
            "as_of_date": "2026-07-31",
            "from_date": "2026-07-31",
            "origin": "RECONSTRUCTED",
            "policy_version": BROOKS_POLICY_VERSION,
            "market_database": "/tmp/market.sqlite3",
            "source_digest": "source",
            "counts": {"daily_facts": 1},
            "status": "SUCCESS",
            "started_at": "2026-07-31T17:00:00+08:00",
            "finished_at": "2026-07-31T17:01:00+08:00",
        },
    )

    assert migration["state"] == "CREATED"
    assert repository.latest_brooks_run_date() == "2026-07-31"
    rows = repository.brooks_rows("2026-07-31")
    assert len(rows) == 1
    assert rows[0]["state"] == "S1_TOUCH25"
    with repository.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM stock_method_daily_fact").fetchone()[0] == 0


def test_brooks_repository_keeps_v1_and_v2_separate_on_the_same_date(tmp_path: Path) -> None:
    repository = WatchlistRepository(tmp_path / "watchlist.sqlite3")
    repository.initialize()
    repository.migrate_brooks_schema()
    current_event, current_fact = _event_and_fact()
    legacy_version = "brooks-second-leg-daily-v1"
    for version, run_id in (
        (legacy_version, "brooks-v1"),
        (BROOKS_POLICY_VERSION, "brooks-v2"),
    ):
        repository.persist_brooks_run(
            events=[{**current_event, "policy_version": version}],
            facts=[{**current_fact, "policy_version": version}],
            receipt={
                "run_id": run_id,
                "as_of_date": "2026-07-31",
                "from_date": "2026-07-31",
                "origin": "RECONSTRUCTED",
                "policy_version": version,
                "market_database": "/tmp/market.sqlite3",
                "source_digest": version,
                "counts": {"daily_facts": 1},
                "status": "SUCCESS",
                "started_at": "2026-07-31T17:00:00+08:00",
                "finished_at": "2026-07-31T17:01:00+08:00",
            },
        )

    assert repository.latest_brooks_run_date(legacy_version) == "2026-07-31"
    assert repository.latest_brooks_run_date(BROOKS_POLICY_VERSION) == "2026-07-31"
    assert len(repository.brooks_rows("2026-07-31")) == 2
    assert len(repository.brooks_rows("2026-07-31", legacy_version)) == 1
    assert len(repository.brooks_rows("2026-07-31", BROOKS_POLICY_VERSION)) == 1


def test_brooks_page_is_independent_and_does_not_claim_a_buy_signal(tmp_path: Path) -> None:
    market_path = tmp_path / "market.sqlite3"
    watchlist_path = tmp_path / "watchlist.sqlite3"
    user_path = tmp_path / "users.sqlite3"
    with sqlite3.connect(market_path):
        pass
    UserRepository(user_path).migrate_schema()
    repository = WatchlistRepository(watchlist_path)
    repository.initialize()
    repository.migrate_brooks_schema()
    event, fact = _event_and_fact()
    repository.persist_brooks_run(
        events=[event],
        facts=[fact],
        receipt={
            "run_id": "brooks-web",
            "as_of_date": "2026-07-31",
            "from_date": "2026-07-31",
            "origin": "RECONSTRUCTED",
            "policy_version": BROOKS_POLICY_VERSION,
            "market_database": str(market_path),
            "source_digest": "source",
            "counts": {"daily_facts": 1},
            "status": "SUCCESS",
            "started_at": "2026-07-31T17:00:00+08:00",
            "finished_at": "2026-07-31T17:01:00+08:00",
        },
    )
    app = create_app(
        market_database=market_path,
        watchlist_database=watchlist_path,
        user_database=user_path,
        secure_cookies=False,
        access_logging=False,
    )

    page = TestClient(app).get("/a/brooks-second-leg?date=2026-07-31")

    assert page.status_code == 200
    assert "强势第一腿—第二腿观察池" in page.text
    assert "S1 回吐25%" in page.text
    assert "000001.SZ" in page.text
    assert "不与 Weinstein、Minervini 合成总分" in page.text
    assert "机械买点" in page.text
    assert "观察链" in page.text
    assert "S0" in page.text
    assert "S2" in page.text
    assert "现价" in page.text
    assert "13.00" in page.text
    assert "首次触发 8日" in page.text
    assert "第一腿 8 个交易日" in page.text
    assert "最大连续收涨 6 日" in page.text
    assert "data-download-tradingview-list=\"brooks-results\"" in page.text

    filtered = TestClient(app).get("/a/brooks-second-leg?date=2026-07-31&min_amount=2")
    assert "当前条件下没有 Brooks 观察事件" in filtered.text
    assert "没有事件本身也是有效的研究结果" in filtered.text
    assert "查看全部事实" in filtered.text
    assert "查看运行状态" in filtered.text
    assert "data-download-tradingview-list=\"brooks-results\"" not in filtered.text

    runs = TestClient(app).get("/a/runs")
    assert "Brooks 独立计算凭证" in runs.text
    assert "brooks-web" not in runs.text  # 运行 ID 不作为产品展示字段。
    assert "1" in runs.text


def test_brooks_service_reads_raw_times_factor_and_persists_a_run(tmp_path: Path) -> None:
    market_path = tmp_path / "market.sqlite3"
    watchlist_path = tmp_path / "watchlist.sqlite3"
    dates = _dates(142)
    with sqlite3.connect(market_path) as connection:
        connection.executescript(
            """
            CREATE TABLE daily_bars (
                market TEXT, symbol TEXT, trade_date TEXT, open REAL, high REAL,
                low REAL, close REAL, volume REAL, amount REAL, adj_type TEXT,
                pre_close REAL, source_version TEXT, input_hash TEXT,
                data_as_of_date TEXT, price_scale_id TEXT
            );
            CREATE TABLE daily_adjustment_factors (
                market TEXT, symbol TEXT, trade_date TEXT, adj_factor REAL,
                factor_as_of_date TEXT
            );
            CREATE TABLE security_master_history (
                valid_from TEXT, symbol TEXT, name TEXT, industry TEXT,
                list_date TEXT, listing_status_as_of TEXT, is_st INTEGER,
                is_suspended INTEGER, trading_status TEXT
            );
            CREATE TABLE market_index_daily_bars (
                market TEXT, index_symbol TEXT, trade_date TEXT
            );
            """
        )
        rows: list[tuple[object, ...]] = []
        factors: list[tuple[object, ...]] = []
        close = 100.0
        for index, trade_date in enumerate(dates):
            if index < 134:
                close = 100.0 + (0.2 if index % 2 else 0.0)
                open_price, high, low = close - 0.1, close + 0.5, close - 0.5
            else:
                close = 100.2 + (index - 133) * 0.5
                open_price, high, low = close - 0.3, close + 0.1, close - 0.4
            for adj_type in ("raw", "qfq"):
                rows.append(
                    (
                        "ashare",
                        "000001.SZ",
                        trade_date,
                        open_price,
                        high,
                        low,
                        close,
                        1000.0,
                        30_000.0,
                        adj_type,
                        close - 0.1,
                        "test",
                        trade_date,
                        dates[-1],
                        "scale",
                    )
                )
            factors.append(("ashare", "000001.SZ", trade_date, 1.0, dates[-1]))
        connection.executemany(
            "INSERT INTO daily_bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        connection.executemany(
            "INSERT INTO daily_adjustment_factors VALUES (?,?,?,?,?)",
            factors,
        )
        connection.execute(
            "INSERT INTO security_master_history VALUES (?,?,?,?,?,?,?,?,?)",
            (
                dates[0],
                "000001.SZ",
                "测试股票",
                "测试行业",
                "2000-01-01",
                "listed",
                0,
                0,
                "trading",
            ),
        )
    repository = WatchlistRepository(watchlist_path)
    repository.initialize()
    repository.migrate_brooks_schema()

    payload = run_brooks_watchlist(
        BrooksRunConfig(
            market_database=market_path,
            watchlist_database=watchlist_path,
            as_of_date=dates[-1],
            from_date=dates[-1],
            origin="RECONSTRUCTED",
        )
    )

    assert payload["status"] == "SUCCESS"
    assert payload["counts"]["symbols_scanned"] == 1
    assert payload["counts"]["impulse_events_seen"] >= 1
    assert repository.latest_brooks_run_date() == dates[-1]
    assert repository.brooks_rows(dates[-1])
