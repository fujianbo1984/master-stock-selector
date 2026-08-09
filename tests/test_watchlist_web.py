from __future__ import annotations

import sqlite3
from typing import Any

from fastapi.testclient import TestClient

from master_stock_selector.watchlist.methods import (
    MINERVINI_INDEX_STAGE2_POLICY_VERSION,
    MINERVINI_POLICY_VERSION,
    WEINSTEIN_POLICY_VERSION,
)
from master_stock_selector.watchlist.repository import WatchlistRepository
from master_stock_selector.web.app import PROJECT_ROOT, _resolve_database_path, create_app
from master_stock_selector.web.users import SESSION_COOKIE


def _seed_watchlist(path):
    repository = WatchlistRepository(path)
    common = {
        "as_of_date": "2026-07-31",
        "symbol": "000001.SZ",
        "source_digest": "stock-digest",
        "origin": "RECONSTRUCTED",
    }
    eligibility = {
        "name": "平安银行",
        "industry": "银行",
        "list_date": "1991-04-03",
        "is_st": False,
        "is_suspended": False,
    }
    repository.persist_run(
        stock_facts=[
            {
                **common,
                "method": "minervini",
                "result": "PASS",
                "policy_version": MINERVINI_POLICY_VERSION,
                "evidence": {
                    "eligibility": eligibility,
                    "profile": {
                        "result": "PASS",
                        "metrics": {"close": 12.5, "sma150": 10.5, "sma200": 9.8},
                        "checks": {"close_above_sma150": True},
                    },
                },
            },
            {
                **common,
                "method": "weinstein",
                "result": "PASS",
                "policy_version": WEINSTEIN_POLICY_VERSION,
                "evidence": {
                    "eligibility": eligibility,
                    "profile": {
                        "result": "PASS",
                        "stage": "STAGE_2",
                        "stage_started_on": "2026-07-03",
                        "duration_weeks": 5,
                        "effective_week_end": "2026-07-31",
                        "evidence": {"ma30": 10.1, "ma30_slope_4w_pct": 2.4},
                    },
                },
            },
        ],
        index_facts=[
            {
                "effective_date": "2026-07-31",
                "index_symbol": symbol,
                "index_name": name,
                "stage": "STAGE_2",
                "stage_started_on": "2026-07-03",
                "duration_weeks": 5,
                "policy_version": WEINSTEIN_POLICY_VERSION,
                "evidence": {
                    "close": 100.0,
                    "ma30": 95.0,
                    "ma30_slope_4w_pct": 2.0,
                    "return_13w_pct": 8.0,
                    "close_above_ma30": True,
                },
                "source_digest": "index-digest",
                "origin": "RECONSTRUCTED",
            }
            for symbol, name in (
                ("000300.SH", "沪深300"),
                ("000852.SH", "中证1000"),
                ("399006.SZ", "创业板指"),
                ("000688.SH", "科创50"),
            )
        ],
        index_minervini_facts=[
            {
                "as_of_date": "2026-07-31",
                "index_symbol": symbol,
                "index_name": name,
                "result": "PASS",
                "policy_version": MINERVINI_INDEX_STAGE2_POLICY_VERSION,
                "evidence": {
                    "reason": "PRICE_TEMPLATE_COMPLETE",
                    "bar_count": 260,
                    "metrics": {"close": 100.0, "sma200": 80.0},
                    "failed_checks": [],
                    "evaluated_check_count": 9,
                    "relative_strength_rule": "NOT_APPLICABLE_TO_INDEX",
                },
                "source_digest": "index-digest",
                "origin": "RECONSTRUCTED",
            }
            for symbol, name in (
                ("000300.SH", "沪深300"),
                ("000852.SH", "中证1000"),
                ("399006.SZ", "创业板指"),
                ("000688.SH", "科创50"),
            )
        ],
        receipt={
            "run_id": "watchlist-test",
            "as_of_date": "2026-07-31",
            "from_date": "2026-07-31",
            "origin": "RECONSTRUCTED",
            "minervini_policy_version": MINERVINI_POLICY_VERSION,
            "weinstein_policy_version": WEINSTEIN_POLICY_VERSION,
            "market_database": "/tmp/market.sqlite3",
            "source_digest": "source",
            "counts": {
                "stock_facts": 2,
                "minervini_pass": 1,
                "weinstein_pass": 1,
            },
            "status": "SUCCESS",
            "started_at": "2026-08-02T10:00:00+08:00",
            "finished_at": "",
        },
        identities=[
            {
                "snapshot_date": "2026-07-31",
                "symbol": "000001.SZ",
                **eligibility,
                "listing_status": "L",
                "trading_status": "trading",
                "origin": "RECONSTRUCTED",
            }
        ],
    )
    repository.import_industry_snapshot(
        {
            "snapshot_date": "2026-07-31",
            "taxonomy": "SW2021",
            "industry_level": "L3",
            "source": "tushare:index_member_all",
            "source_digest": "industry-digest",
            "ambiguous_symbol_count": 0,
            "dimensions": [
                {
                    "snapshot_date": "2026-07-31",
                    "taxonomy": "SW2021",
                    "industry_level": "L3",
                    "industry_code": "851911.SI",
                    "industry_name": "股份制银行III",
                    "parent_industry_code": "",
                    "source": "tushare:index_member_all",
                    "source_version": "SW2021",
                    "source_digest": "industry-digest",
                    "origin": "RECONSTRUCTED",
                }
            ],
            "memberships": [
                {
                    "snapshot_date": "2026-07-31",
                    "symbol": "000001.SZ",
                    "taxonomy": "SW2021",
                    "industry_level": "L3",
                    "industry_code": "851911.SI",
                    "industry_name": "股份制银行III",
                    "valid_from": "1991-04-03",
                    "valid_to": "",
                    "assignment_state": "VERIFIED",
                    "source": "tushare:index_member_all",
                    "source_digest": "industry-digest",
                    "origin": "RECONSTRUCTED",
                }
            ],
        }
    )


def _client(tmp_path):
    watchlist_path = tmp_path / "master_watchlist.sqlite3"
    _seed_watchlist(watchlist_path)
    return _authenticated_client(
        create_app(
            market_database=tmp_path / "market.sqlite3",
            watchlist_database=watchlist_path,
            secure_cookies=False,
        )
    )


def _authenticated_client(app, username: str = "tester") -> TestClient:
    users = app.state.user_repository
    account = next(
        (item for item in users.list_users() if item["username"] == username), None
    )
    if account is None:
        users.create_user(username, "Test-password-123", "测试用户")
    client = TestClient(app)
    response = client.post(
        "/login",
        data={"username": username, "password": "Test-password-123"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return client


def _csrf(client: TestClient) -> str:
    user = client.app.state.user_repository.session_user(
        client.cookies.get(SESSION_COOKIE)
    )
    assert user is not None
    return user.csrf_token


def test_relative_database_paths_are_rooted_at_project_directory() -> None:
    assert _resolve_database_path("data/example.sqlite3") == (
        PROJECT_ROOT / "data" / "example.sqlite3"
    ).resolve()


def test_healthz_is_degraded_when_market_or_watchlist_facts_are_missing(tmp_path) -> None:
    client = TestClient(
        create_app(
            market_database=tmp_path / "missing-market.sqlite3",
            watchlist_database=tmp_path / "empty-watchlist.sqlite3",
            user_database=tmp_path / "users.sqlite3",
            secure_cookies=False,
        )
    )

    response = client.get("/healthz")

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert response.json()["runtime"]["has_facts"] is False
    assert response.json()["market"]["exists"] is False


def test_healthz_is_ok_with_market_data_and_watchlist_facts(tmp_path) -> None:
    market_path = tmp_path / "market.sqlite3"
    watchlist_path = tmp_path / "master_watchlist.sqlite3"
    _seed_market_context(market_path)
    _seed_watchlist(watchlist_path)
    client = TestClient(
        create_app(
            market_database=market_path,
            watchlist_database=watchlist_path,
            user_database=tmp_path / "users.sqlite3",
            secure_cookies=False,
        )
    )

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def _seed_market_context(path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE daily_metrics (
                market TEXT NOT NULL,
                symbol TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                total_mv REAL,
                circ_mv REAL,
                PRIMARY KEY (market, symbol, trade_date)
            );
            CREATE TABLE daily_bars (
                market TEXT NOT NULL,
                symbol TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                amount REAL,
                close REAL,
                pre_close REAL,
                pct_chg REAL,
                adj_type TEXT NOT NULL,
                PRIMARY KEY (market, symbol, trade_date, adj_type)
            );
            """
        )
        connection.executemany(
            "INSERT INTO daily_metrics VALUES ('ashare', ?, '2026-07-31', ?, ?)",
            (
                ("000001.SZ", 800000.0, 700000.0),
                ("000002.SZ", 600000.0, 550000.0),
                ("000003.SZ", 400000.0, 300000.0),
            ),
        )
        connection.executemany(
            "INSERT INTO daily_bars VALUES ('ashare', ?, '2026-07-31', ?, ?, ?, ?, 'qfq')",
            (
                ("000001.SZ", 200000.0, 12.5, 12.0, 4.17),
                ("000002.SZ", 180000.0, 8.0, 8.2, -2.44),
                ("000003.SZ", 60000.0, 5.0, 5.0, 0.0),
            ),
        )
        connection.executemany(
            "INSERT INTO daily_bars VALUES ('ashare', ?, '2026-07-31', ?, ?, ?, ?, 'raw')",
            (
                ("000001.SZ", 200000.0, 12.5, 12.0, 4.17),
                ("000002.SZ", 180000.0, 8.0, 8.2, -2.44),
                ("000003.SZ", 60000.0, 5.0, 5.0, 0.0),
            ),
        )


def _seed_chart_market(path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE daily_bars (
                market TEXT NOT NULL, symbol TEXT NOT NULL, trade_date TEXT NOT NULL,
                open REAL, high REAL, low REAL, close REAL, volume REAL, amount REAL,
                adj_type TEXT NOT NULL, data_as_of_date TEXT NOT NULL, price_scale_id TEXT NOT NULL,
                PRIMARY KEY (market, symbol, trade_date, adj_type)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE daily_adjustment_factors (
                market TEXT NOT NULL, symbol TEXT NOT NULL, trade_date TEXT NOT NULL,
                adj_factor REAL NOT NULL,
                PRIMARY KEY (market, symbol, trade_date)
            )
            """
        )
        qfq_rows = [("2026-05-31", 99.0, 99.0, 99.0, 99.0, "2026-05-31", "")] + [
            (f"2026-06-{day:02d}", 10 + day / 10, 11 + day / 10, 9 + day / 10, 10.5 + day / 10, f"2026-06-{day:02d}", "qfq-scale-v1")
            for day in range(1, 31)
        ]
        connection.executemany(
            """
            INSERT INTO daily_bars VALUES ('ashare', '000001.SZ', ?, ?, ?, ?, ?, 1000, 2000,
                                           'qfq', ?, 'qfq-scale-v1')
            """,
            [row[:6] for row in qfq_rows],
        )
        connection.executemany(
            """
            UPDATE daily_bars SET price_scale_id=?
            WHERE market='ashare' AND symbol='000001.SZ' AND trade_date=? AND adj_type='qfq'
            """,
            [(row[6], row[0]) for row in qfq_rows],
        )
        raw_rows = [("2026-05-31", 9.9, 10.9, 8.9, 10.4, "2026-05-31")] + [row[:6] for row in qfq_rows[1:]]
        connection.executemany(
            """
            INSERT INTO daily_bars VALUES ('ashare', '000001.SZ', ?, ?, ?, ?, ?, 1000, 2000,
                                           'raw', ?, '')
            """,
            raw_rows,
        )
        connection.executemany(
            "INSERT INTO daily_adjustment_factors VALUES ('ashare', '000001.SZ', ?, 1.0)",
            [(row[0],) for row in qfq_rows],
        )


def _seed_filter_cases(path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executemany(
            """
            INSERT INTO stock_method_daily_fact (
                as_of_date, symbol, method, result, policy_version,
                evidence_json, source_digest, origin
            ) VALUES ('2026-07-31', ?, 'minervini', 'PASS', ?, '{}', 'digest', 'RECONSTRUCTED')
            """,
            (
                ("000002.SZ", MINERVINI_POLICY_VERSION),
                ("000003.SZ", MINERVINI_POLICY_VERSION),
            ),
        )
        connection.executemany(
            """
            INSERT INTO stock_method_transition (
                as_of_date, symbol, method, state, policy_version,
                first_qualified_on, streak_started_on, consecutive_sessions,
                reason, origin
            ) VALUES (
                '2026-07-31', ?, 'minervini', 'ENTERED', ?,
                '2026-07-31', '2026-07-31', 1, '', 'RECONSTRUCTED'
            )
            """,
            (
                ("000002.SZ", MINERVINI_POLICY_VERSION),
                ("000003.SZ", MINERVINI_POLICY_VERSION),
            ),
        )
        connection.executemany(
            """
            INSERT INTO security_identity_history (
                valid_from, symbol, name, industry, list_date, is_st,
                is_suspended, listing_status, trading_status, source_digest, origin
            ) VALUES ('2026-07-31', ?, ?, '测试行业', '2020-01-01', ?, 0, 'L', 'trading', 'identity-digest', 'RECONSTRUCTED')
            """,
            (
                ("000002.SZ", "ST示例", 1),
                ("000003.SZ", "小盘示例", 0),
            ),
        )
        connection.executemany(
            """
            INSERT INTO security_industry_membership_history (
                symbol, taxonomy, industry_level, industry_code,
                industry_name, valid_from, valid_to, assignment_state, source,
                source_digest, origin
            ) VALUES (
                ?, 'SW2021', 'L3', '851911.SI',
                '股份制银行III', '2020-01-01', '', 'VERIFIED',
                'tushare:index_member_all', 'industry-digest', 'RECONSTRUCTED'
            )
            """,
            (("000002.SZ",), ("000003.SZ",)),
        )
        connection.execute(
            """
            INSERT INTO security_identity_history (
                valid_from, symbol, name, industry, list_date, is_st,
                is_suspended, listing_status, trading_status, source_digest, origin
            ) VALUES (
                '2026-08-01', '000001.SZ', 'ST未来名称', '银行', '1991-04-03',
                1, 0, 'L', 'trading', 'future-digest', 'RECONSTRUCTED'
            )
            """
        )


def test_new_site_only_exposes_two_master_watchlist_surfaces(tmp_path):
    client = _client(tmp_path)

    response = client.get("/a/daily?min_cap=0")

    assert response.status_code == 200
    assert "大师观察池" in response.text
    assert "Weinstein" in response.text
    assert "Minervini" in response.text
    assert "两法同时符合" in response.text
    assert "行业聚集观察" in response.text
    assert "行业观察" in response.text
    assert "股份制银行III" in response.text
    assert 'role="tablist"' in response.text
    assert 'aria-controls="new-candidates"' in response.text
    assert 'id="continuing-candidates" role="tabpanel"' in response.text
    assert 'id="continuing-candidates" role="tabpanel" aria-labelledby="tab-continuing-candidates" tabindex="0" hidden' in response.text
    assert "https://cn.tradingview.com/chart/?symbol=SZSE%3A000001" in response.text
    assert "https://stockpage.10jqka.com.cn/000001/" in response.text
    assert (
        'href="/a/stocks/000001.SZ?date=2026-07-31&amp;method=all&amp;state=all'
        '&amp;min_cap=0&amp;section=new-candidates"'
    ) in response.text
    assert 'href="/a/industries/851911.SI/chart?date=2026-07-31"' in response.text
    assert response.text.count("kpi-link") >= 5
    assert "state=PASSING" in response.text
    assert "state=NEW" in response.text
    assert "state=STABLE" in response.text
    assert "state=EXIT" in response.text
    assert "method=both&state=PASSING" in response.text
    assert "manual=FOCUS" not in response.text
    assert "人工状态" not in response.text
    assert "+加入观察" in response.text
    for symbol in ("000300.SH", "000852.SH", "399006.SZ", "000688.SH"):
        assert symbol in response.text
    assert "O’Neil" not in response.text
    assert "VCP" not in response.text
    assert "ETF" not in response.text
    assert "综合评分" not in response.text
    assert response.text.index('id="new-candidates"') < response.text.index(
        'id="industry-observation"'
    )
    assert client.get("/").history[0].headers["location"] == "/a/daily"
    assert client.get("/a/dashboard").history[0].headers["location"] == "/a/daily"
    focus_redirect = client.get(
        "/a/focus?date=2026-07-31&min_cap=0", follow_redirects=False
    )
    assert focus_redirect.headers["location"] == (
        "/a/observations?date=2026-07-31&min_cap=0&state=FOCUS"
    )
    focus = client.get("/a/focus", follow_redirects=True)
    assert 'aria-current="page" href="/a/observations?date=2026-07-31">我的观察</a>' in focus.text
    assert "个人工作区 · 测试用户" in focus.text
    assert "这一类还是空的" in focus.text
    assert "仅当前账号可见" in focus.text
    assert 'aria-label="我的观察分类"' in focus.text
    review = client.get("/a/review")
    assert "个人数据 · 仅你可见" in review.text
    assert 'aria-label="当前个人工作区"' in review.text
    for state in ("PASSING", "NEW", "STABLE", "EXIT"):
        filtered = client.get(f"/a/daily?date=2026-07-31&state={state}&min_cap=0")
        assert filtered.status_code == 200
        assert f'value="{state}" selected' in filtered.text
    passing = client.get("/a/daily?date=2026-07-31&state=PASSING&min_cap=0")
    assert "当前符合" in passing.text
    assert "当前仍符合 <strong>1</strong>" in passing.text
    assert "显示 1 / 1" in passing.text


def test_four_indices_show_weinstein_stage_and_minervini_stage2_without_composite(
    tmp_path,
):
    client = _client(tmp_path)

    response = client.get("/a/indices?date=2026-07-31")

    assert response.status_code == 200
    assert "Weinstein 完整阶段 + Minervini 第二阶段是/否" in response.text
    assert response.text.count("Minervini 第二阶段") >= 4
    assert "不使用个股横截面相对强度排名" in response.text
    assert "沪深300" in response.text
    assert "中证1000" in response.text
    assert "创业板指" in response.text
    assert "科创50" in response.text
    assert "000300.SH" in response.text
    assert "000852.SH" in response.text
    assert "399006.SZ" in response.text
    assert "000688.SH" in response.text
    assert "市场总分" not in response.text
    payload = client.get("/api/a/indices/2026-07-31").json()
    assert payload["methods"] == {
        "weinstein": "full_stage",
        "minervini": "stage2_only",
    }
    assert {row["stage"] for row in payload["rows"]} == {"STAGE_2"}
    assert {row["minervini"]["result"] for row in payload["rows"]} == {"PASS"}
    assert {row["minervini"]["result_label"] for row in payload["rows"]} == {"是"}


def test_stock_detail_keeps_method_facts_separate_from_manual_review(tmp_path):
    client = _client(tmp_path)

    detail = client.get("/a/stocks/000001.SZ")
    saved = client.post(
        "/a/stocks/000001.SZ/review",
        data={
            "csrf_token": _csrf(client),
            "manual_state": "FOCUS",
            "note": "等待年报后人工复核",
        },
        follow_redirects=True,
    )

    assert detail.status_code == 200
    assert "两种方法独立证据" in detail.text
    assert "30周线" in detail.text
    assert "股份制银行III（申万2021三级）" in detail.text
    assert detail.text.index("method-evidence-grid") < detail.text.index(
        "manual-review-panel"
    )
    assert "两种方法逐项对照" in detail.text
    assert '<details class="section stock-disclosure trade-journal-panel"' in detail.text
    assert saved.status_code == 200
    assert 'manual-focus">重点' in saved.text
    assert "等待年报后人工复核" in saved.text
    assert '<select name="manual_state">' not in saved.text
    api = client.get("/api/a/watchlist/2026-07-31?method=both&min_cap=0").json()
    assert len(api["rows"]) == 1
    assert api["rows"][0]["both_pass"] is True


def test_chart_navigation_keeps_the_originating_watchlist_section_and_filters(tmp_path):
    watchlist_path = tmp_path / "master_watchlist.sqlite3"
    market_path = tmp_path / "market.sqlite3"
    _seed_watchlist(watchlist_path)
    _seed_filter_cases(watchlist_path)
    _seed_market_context(market_path)
    app = create_app(
        market_database=market_path,
        watchlist_database=watchlist_path,
        secure_cookies=False,
    )
    client = _authenticated_client(app)
    user = app.state.user_repository.session_user(client.cookies.get(SESSION_COOKIE))
    assert user is not None
    app.state.user_repository.save_review(
        user.user_id, "000001.SZ", "FOCUS", "保留既有备注"
    )
    app.state.user_repository.save_review(user.user_id, "000003.SZ", "FOCUS", "")

    workspace = client.get("/a/observations?date=2026-07-31&state=FOCUS")
    detail = client.get(
        "/a/stocks/000001.SZ?date=2026-07-31&manual=FOCUS&min_cap=0&section=personal-observations"
    )
    chart = client.get(
        "/a/stocks/000001.SZ/chart?date=2026-07-31&manual=FOCUS&min_cap=0&section=personal-observations"
    )

    assert workspace.status_code == 200
    assert "section=personal-observations" in workspace.text
    assert detail.status_code == 200
    assert "我的观察 · 1 / 2" in detail.text
    assert "小盘示例 →" in detail.text
    assert "/a/stocks/000003.SZ?date=2026-07-31" in detail.text
    assert chart.status_code == 200
    assert "我的观察 <b>1 / 2</b>" in chart.text
    assert "今日观察名单" in chart.text
    assert "Minervini：符合" in chart.text
    assert "Weinstein：符合" in chart.text
    assert "/a/stocks/000003.SZ/chart?date=2026-07-31" in chart.text
    assert "data-chart-next" in chart.text
    assert "data-chart-next href=" in chart.text
    assert "/chart?date=2026-07-31&amp;" in chart.text
    next_href = chart.text.split('data-chart-next href="', 1)[1].split('"', 1)[0]
    assert "#stock-chart" not in next_href
    assert 'aria-label="我的观察"' in chart.text
    assert "归档" in chart.text
    assert ">保存</button>" not in chart.text

    next_chart = client.get(
        "/a/stocks/000003.SZ/chart?date=2026-07-31&manual=FOCUS&min_cap=0"
        "&section=personal-observations"
    )
    assert next_chart.status_code == 200
    assert "data-chart-previous" in next_chart.text
    assert "平安银行" in next_chart.text

    legacy_fragment = client.get(
        "/a/stocks/000003.SZ/chart?date=2026-07-31&manual=FOCUS&min_cap=0"
        "&section=personal-observations%23stock-chart"
    )
    assert legacy_fragment.status_code == 200
    assert "data-chart-previous" in legacy_fragment.text

    updated = client.post(
        "/a/stocks/000001.SZ/review",
        data={
            "csrf_token": _csrf(client),
            "manual_state": "ARCHIVED",
            "return_to": "/a/stocks/000001.SZ/chart?date=2026-07-31&manual=FOCUS&min_cap=0&section=personal-observations#stock-chart",
            "nav_position": "1",
            "nav_total": "2",
            "nav_next": "000003.SZ",
        },
        follow_redirects=False,
    )

    assert updated.status_code == 303
    assert updated.headers["location"].endswith("#stock-chart")
    assert "nav_next=000003.SZ" in updated.headers["location"]
    revised_chart = client.get(updated.headers["location"])
    assert 'manual-archived">已归档' in revised_chart.text
    assert "data-chart-next" in revised_chart.text
    assert "/a/stocks/000003.SZ/chart?date=2026-07-31" in revised_chart.text
    assert "保留既有备注" in client.get("/a/stocks/000001.SZ").text


def test_industry_observation_page_and_api_are_fact_only(tmp_path):
    client = _client(tmp_path)

    page = client.get("/a/industries?date=2026-07-31")
    chart = client.get("/a/industries/851911.SI/chart?date=2026-07-31")
    payload = client.get("/api/a/industries/2026-07-31").json()
    detail = client.get("/api/a/industries/2026-07-31/851911.SI").json()

    assert page.status_code == 200
    assert chart.status_code == 200
    assert "成分股等权代理日线" in chart.text
    assert "不是申万官方行业指数" in chart.text
    assert "行业技术确认" in chart.text
    assert "行业确认数据不足，不作趋势判断。" in chart.text
    assert "industry-weinstein-confirmation-v1" in chart.text
    assert 'href="/a/daily?date=2026-07-31&industry=851911.SI"' in chart.text
    assert "不计分、不判定“主线”" in page.text
    assert "小样本，仅看数量" in page.text
    assert "行业总分" not in page.text
    assert 'aria-current="page" href="/a/industries?date=2026-07-31"' in page.text
    assert payload["policy_version"] == "industry-observation-v2-non-st"
    assert payload["rows"][0]["both_pass_count"] == 1
    assert detail["members"][0]["symbol"] == "000001.SZ"


def test_stock_industry_context_is_optional_and_does_not_replace_method_evidence(tmp_path):
    client = _client(tmp_path)

    page = client.get("/a/stocks/000001.SZ?date=2026-07-31")

    assert page.status_code == 200
    assert "Weinstein" in page.text
    assert "交易方法" in page.text
    assert "回调" in page.text
    assert "行业背景（仅人工参考）" in page.text
    assert "行业代理不参与 000001.SZ 的 Weinstein 或 Minervini 结论。" in page.text


def test_trade_journal_records_executions_and_renders_descriptive_review(tmp_path):
    client = _client(tmp_path)

    bought = client.post(
        "/a/stocks/000001.SZ/trades",
        data={
            "csrf_token": _csrf(client),
            "traded_on": "2026-07-31", "side": "BUY", "quantity": "100",
                "price": "12.5", "fee": "5", "setup_method": "BREAKOUT", "stop_price": "12.0",
            "rationale": "突破", "invalidation": "跌破止损", "market_context": "指数 Stage 2",
        },
        follow_redirects=False,
    )
    sold = client.post(
        "/a/stocks/000001.SZ/trades",
        data={
            "csrf_token": _csrf(client),
            "traded_on": "2026-08-03", "side": "SELL", "quantity": "100",
                "price": "13.0", "fee": "5", "setup_method": "BREAKOUT", "exit_reason": "计划止盈",
        },
        follow_redirects=False,
    )
    page = client.get("/a/review")

    assert bought.status_code == sold.status_code == 303
    assert page.status_code == 200
    assert "实际成交的描述统计，不生成买卖指令" in page.text
    assert "已完成交易" in page.text
    assert "突破" in page.text
    assert "平安银行" in page.text
    assert "000001.SZ" in page.text
    assert "计划盈亏比" in page.text
    assert "1.0倍初始风险" in page.text
    assert ">修改</a>" in page.text
    assert "2026-07-31" in page.text


def test_legacy_product_pages_are_not_mounted(tmp_path):
    client = _client(tmp_path)

    for path in (
        "/a/market",
        "/a/setups",
        "/a/research",
        "/a/stats",
        "/a/raw",
        "/a/queries",
        "/a/vcp",
        "/a/etfs",
        "/northstar",
    ):
        assert client.get(path).status_code == 404


def test_watchlist_excludes_st_and_hides_small_cap_by_default(tmp_path):
    watchlist_path = tmp_path / "master_watchlist.sqlite3"
    market_path = tmp_path / "market.sqlite3"
    _seed_watchlist(watchlist_path)
    _seed_filter_cases(watchlist_path)
    _seed_market_context(market_path)
    client = TestClient(
        create_app(
            market_database=market_path,
            watchlist_database=watchlist_path,
        )
    )

    default_page = client.get("/a/daily?date=2026-07-31")
    all_page = client.get("/a/daily?date=2026-07-31&min_cap=0")
    filtered_page = client.get("/a/daily?date=2026-07-31&min_cap=50")
    stock_page = client.get("/a/stocks/000001.SZ")
    payload = client.get("/api/a/watchlist/2026-07-31?min_cap=50").json()
    industry_payload = client.get("/api/a/industries/2026-07-31").json()

    assert default_page.status_code == 200
    assert "已按 2026-07-31 当日风险标识排除风险警示股 1 只" in default_page.text
    assert "ST示例" not in default_page.text
    assert "ST未来名称" not in default_page.text
    assert "ST未来名称" not in stock_page.text
    assert "平安银行" in default_page.text
    assert "小盘示例" not in default_page.text
    assert "小盘示例" in all_page.text
    assert "总市值" in default_page.text
    assert "20日成交中位" not in default_page.text
    assert "收 12.5" in default_page.text
    assert "+0.500 / +4.17%" in default_page.text
    assert "20日成交中位" in stock_page.text
    assert 'data-download-tradingview-list="new-candidates"' in default_page.text
    assert 'data-copy-tradingview-list="new-candidates"' in default_page.text
    assert 'data-tradingview-symbol="SZSE:000001"' in default_page.text
    assert "小盘示例" not in filtered_page.text
    assert {row["symbol"] for row in payload["rows"]} == {"000001.SZ"}
    assert payload["rows"][0]["total_market_cap_yi"] == 80.0
    assert payload["rows"][0]["median_amount_20d_yi"] == 2.0
    assert payload["rows"][0]["change_amount"] == 0.5
    assert payload["rows"][0]["change_pct"] == 4.17
    bank_industry = next(
        row for row in industry_payload["rows"] if row["industry_code"] == "851911.SI"
    )
    assert bank_industry["eligible_member_count"] == 2
    assert bank_industry["minervini_pass_count"] == 2
    assert client.get("/a/daily?min_cap=42").status_code == 422


def test_stock_chart_uses_local_qfq_bars_keltner_and_persists_drawings(tmp_path):
    watchlist_path = tmp_path / "master_watchlist.sqlite3"
    market_path = tmp_path / "market.sqlite3"
    _seed_watchlist(watchlist_path)
    _seed_chart_market(market_path)
    app = create_app(
        market_database=market_path,
        watchlist_database=watchlist_path,
        secure_cookies=False,
    )
    client = _authenticated_client(app)
    user = app.state.user_repository.session_user(client.cookies.get(SESSION_COOKIE))
    assert user is not None
    buy_id = app.state.user_repository.record_trade(
        user.user_id,
        traded_on="2026-06-10", symbol="000001.SZ", side="BUY", quantity=100,
        price=11.5, fee=0.0, method="MANUAL", stop_price=10.5,
    )
    app.state.user_repository.record_trade(
        user.user_id,
        traded_on="2026-06-20", symbol="000001.SZ", side="SELL", quantity=100,
        price=12.5, fee=0.0, method="MANUAL",
    )

    page = client.get("/a/stocks/000001.SZ/chart?date=2026-06-30")
    chart = client.get("/api/a/stocks/000001.SZ/chart?date=2026-06-30&limit=30")

    assert page.status_code == 200
    assert "行业：股份制银行III" in page.text
    assert "lightweight-charts-5.1.0.js" in page.text
    assert 'class="chart-workbench-title"' in page.text
    assert "https://cn.tradingview.com/chart/?symbol=SZSE%3A000001" in page.text
    assert "https://stockpage.10jqka.com.cn/000001/" in page.text
    assert 'data-kc-source' in page.text
    assert 'data-chart-limit="all"' in page.text
    assert chart.status_code == 200
    payload = chart.json()
    assert payload["status"] == "OK"
    assert payload["adjustment"] == "qfq"
    assert payload["price_scale_id"] == "qfq-scale-v1"
    assert len(payload["bars"]) == 30
    all_payload = client.get(
        "/api/a/stocks/000001.SZ/chart?date=2026-06-30"
    ).json()
    assert len(all_payload["bars"]) > len(payload["bars"])
    assert all_payload["bars"][0]["trade_date"] == "2026-05-31"
    assert all_payload["bars"][0]["close"] == 10.4
    assert payload["keltner"][-1]["upper"] is not None
    assert "trade_overlay" not in payload
    overlay = client.get(
        "/api/me/stocks/000001.SZ/overlay"
        "?date=2026-06-30&price_scale_id=qfq-scale-v1"
    ).json()
    assert overlay["trade_overlay"]["executions"][0]["execution_id"] == buy_id
    assert overlay["trade_overlay"]["executions"][0]["stop_price"] == 10.5
    weekly = client.get(
        "/api/a/stocks/000001.SZ/chart?date=2026-06-30&limit=30&interval=week"
    ).json()
    assert weekly["interval"] == "week"
    assert len(weekly["bars"]) < len(payload["bars"])
    open_source = client.get(
        "/api/a/stocks/000001.SZ/chart?date=2026-06-30&limit=30&source=open"
    ).json()
    assert open_source["keltner"][-1]["basis"] != payload["keltner"][-1]["basis"]
    saved = client.post(
        "/api/me/stocks/000001.SZ/chart/drawings",
        headers={"X-CSRF-Token": _csrf(client)},
        json={
            "drawing_id": "line-1",
            "price_scale_id": "qfq-scale-v1",
            "tool": "trendline",
            "anchors": [
                {"date": "2026-06-10", "price": 11.5},
                {"logical_from_end": 5.5, "price": 12.5},
            ],
        },
    )
    assert saved.status_code == 200
    drawings = client.get(
        "/api/me/stocks/000001.SZ/overlay"
        "?date=2026-06-30&price_scale_id=qfq-scale-v1"
    ).json()["drawings"]
    assert drawings[0]["anchors"][1]["logical_from_end"] == 5.5
    deleted = client.delete(
        "/api/me/stocks/000001.SZ/chart/drawings/line-1?price_scale_id=qfq-scale-v1",
        headers={"X-CSRF-Token": _csrf(client)},
    )
    assert deleted.json() == {"deleted": True}
    prefilled = client.get("/a/stocks/000001.SZ?traded_on=2026-06-10&trade_price=11.5")
    assert 'name="traded_on" type="date" value="2026-06-10"' in prefilled.text
    assert 'name="price" type="number" min="0.001" step="0.001" value="11.5"' in prefilled.text


def test_private_review_overlays_public_cache_and_exposes_timing(tmp_path):
    watchlist_path = tmp_path / "master_watchlist.sqlite3"
    market_path = tmp_path / "market.sqlite3"
    _seed_watchlist(watchlist_path)
    _seed_market_context(market_path)
    app = create_app(
        market_database=market_path,
        watchlist_database=watchlist_path,
        secure_cookies=False,
    )
    market_reader = app.state.market_reader
    original = market_reader.safe_stock_market_metrics
    calls: list[bool] = []

    def traced_metrics(*args: Any, **kwargs: Any) -> dict[str, dict[str, Any]]:
        calls.append(bool(kwargs.get("include_liquidity", True)))
        return original(*args, **kwargs)

    market_reader.safe_stock_market_metrics = traced_metrics
    client = _authenticated_client(app)

    first = client.get("/a/daily?date=2026-07-31&min_cap=0")
    second = client.get("/a/daily?date=2026-07-31&min_cap=0")
    assert first.status_code == second.status_code == 200
    assert calls == [False]
    assert first.headers["server-timing"].startswith("app;dur=")

    saved = client.post(
        "/a/stocks/000001.SZ/review",
        data={
            "csrf_token": _csrf(client),
            "manual_state": "FOCUS",
            "note": "缓存失效后仍应显示",
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303
    refreshed = client.get("/a/daily?date=2026-07-31&min_cap=0")
    assert calls == [False]
    assert 'manual-focus">重点' in refreshed.text

    static = client.get("/static/vendor/lightweight-charts-5.1.0.js")
    assert static.status_code == 200
    assert static.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_anonymous_surface_is_public_read_only_and_private_routes_require_login(tmp_path):
    watchlist_path = tmp_path / "master_watchlist.sqlite3"
    market_path = tmp_path / "market.sqlite3"
    _seed_watchlist(watchlist_path)
    _seed_chart_market(market_path)
    app = create_app(
        market_database=market_path,
        watchlist_database=watchlist_path,
        secure_cookies=False,
    )
    client = TestClient(app)

    login = client.get("/login")
    assert "公开事实与个人判断" in login.text
    assert "一个账号对应一个独立工作区" in login.text
    assert 'data-password-toggle' in login.text
    assert "进入我的工作区" in login.text

    stock = client.get("/a/stocks/000001.SZ")
    chart = client.get("/api/a/stocks/000001.SZ/chart?date=2026-06-30")

    assert stock.status_code == 200
    assert "登录后记录个人观察" in stock.text
    assert 'action="/a/stocks/000001.SZ/review"' not in stock.text
    assert "trade_overlay" not in chart.json()
    assert "drawings" not in chart.json()
    assert client.get("/a/focus", follow_redirects=False).headers["location"].startswith(
        "/login"
    )
    assert client.get("/a/review", follow_redirects=False).headers["location"].startswith(
        "/login"
    )
    assert client.post(
        "/a/stocks/000001.SZ/review",
        data={"manual_state": "FOCUS", "note": "不应写入"},
    ).status_code == 401
    assert client.get(
        "/api/me/stocks/000001.SZ/overlay"
        "?date=2026-06-30&price_scale_id=qfq-scale-v1"
    ).status_code == 401


def test_two_users_are_isolated_for_reviews_trades_drawings_and_csrf(tmp_path):
    watchlist_path = tmp_path / "master_watchlist.sqlite3"
    market_path = tmp_path / "market.sqlite3"
    _seed_watchlist(watchlist_path)
    _seed_chart_market(market_path)
    app = create_app(
        market_database=market_path,
        watchlist_database=watchlist_path,
        secure_cookies=False,
    )
    alice = _authenticated_client(app, "alice")
    bob = _authenticated_client(app, "bob")

    assert alice.post(
        "/a/stocks/000001.SZ/review",
        data={
            "csrf_token": _csrf(alice),
            "manual_state": "FOCUS",
            "note": "Alice 私有备注",
        },
        follow_redirects=False,
    ).status_code == 303
    assert alice.post(
        "/a/stocks/000001.SZ/trades",
        data={
            "csrf_token": _csrf(alice),
            "traded_on": "2026-06-10",
            "side": "BUY",
            "quantity": "100",
            "price": "11.5",
            "fee": "0",
            "setup_method": "PULLBACK",
            "stop_price": "10.5",
        },
        follow_redirects=False,
    ).status_code == 303
    alice_user = app.state.user_repository.session_user(
        alice.cookies.get(SESSION_COOKIE)
    )
    assert alice_user is not None
    alice_review = app.state.user_repository.trade_review(
        alice_user.user_id, {"000001.SZ": "平安银行"}
    )
    execution_id = alice_review["executions"][0]["execution_id"]

    assert "Alice 私有备注" in alice.get("/a/stocks/000001.SZ").text
    assert "Alice 私有备注" not in bob.get("/a/stocks/000001.SZ").text
    assert "Alice 私有备注" not in bob.get("/api/a/watchlist/2026-07-31?min_cap=0").text
    assert bob.get("/a/daily?manual=FOCUS&min_cap=0").text.count("000001.SZ") == 0
    assert bob.post(
        f"/a/trades/{execution_id}",
        data={"csrf_token": _csrf(bob)},
    ).status_code == 404
    assert alice.post(
        "/a/stocks/000001.SZ/review",
        data={"manual_state": "ARCHIVED"},
    ).status_code == 403

    saved = alice.post(
        "/api/me/stocks/000001.SZ/chart/drawings",
        headers={"X-CSRF-Token": _csrf(alice)},
        json={
            "drawing_id": "alice-line",
            "price_scale_id": "qfq-scale-v1",
            "tool": "horizontal",
            "anchors": [{"date": "2026-06-10", "price": 11.5}],
        },
    )
    assert saved.status_code == 200
    alice_overlay = alice.get(
        "/api/me/stocks/000001.SZ/overlay"
        "?date=2026-06-30&price_scale_id=qfq-scale-v1"
    ).json()
    bob_overlay = bob.get(
        "/api/me/stocks/000001.SZ/overlay"
        "?date=2026-06-30&price_scale_id=qfq-scale-v1"
    ).json()
    assert len(alice_overlay["drawings"]) == 1
    assert len(alice_overlay["trade_overlay"]["executions"]) == 1
    assert bob_overlay == {
        "drawings": [],
        "trade_overlay": {"executions": [], "open_stops": []},
    }
    assert bob.delete(
        "/api/me/stocks/000001.SZ/chart/drawings/alice-line"
        "?price_scale_id=qfq-scale-v1",
        headers={"X-CSRF-Token": _csrf(bob)},
    ).json() == {"deleted": False}


def test_login_cookie_is_secure_by_default(tmp_path):
    watchlist_path = tmp_path / "master_watchlist.sqlite3"
    _seed_watchlist(watchlist_path)
    app = create_app(
        market_database=tmp_path / "market.sqlite3",
        watchlist_database=watchlist_path,
    )
    app.state.user_repository.create_user("secure-user", "Secure-password-123")
    client = TestClient(app)

    response = client.post(
        "/login",
        data={"username": "secure-user", "password": "Secure-password-123"},
        follow_redirects=False,
    )

    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=lax" in cookie
    assert response.headers["strict-transport-security"] == "max-age=31536000"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"


def test_authenticated_user_can_change_password_from_account_settings(tmp_path):
    watchlist_path = tmp_path / "master_watchlist.sqlite3"
    _seed_watchlist(watchlist_path)
    app = create_app(
        market_database=tmp_path / "market.sqlite3",
        watchlist_database=watchlist_path,
        secure_cookies=False,
    )
    client = _authenticated_client(app)

    page = client.get("/account/password")
    assert page.status_code == 200
    assert "修改密码" in page.text
    assert "账户设置" in page.text
    assert 'autocomplete="current-password"' in page.text
    assert page.headers["cache-control"] == "private, no-store"

    wrong = client.post(
        "/account/password",
        data={
            "csrf_token": _csrf(client),
            "current_password": "wrong-password",
            "new_password": "New-test-password-456",
            "new_password_confirmation": "New-test-password-456",
        },
    )
    assert wrong.status_code == 400
    assert "当前密码不正确" in wrong.text
    assert client.get("/a/observations").status_code == 200

    changed = client.post(
        "/account/password",
        data={
            "csrf_token": _csrf(client),
            "current_password": "Test-password-123",
            "new_password": "New-test-password-456",
            "new_password_confirmation": "New-test-password-456",
        },
        follow_redirects=False,
    )
    assert changed.status_code == 303
    assert changed.headers["location"] == "/login?password_changed=1"
    assert client.cookies.get(SESSION_COOKIE) is None
    assert client.get("/a/observations", follow_redirects=False).status_code == 303

    notice = client.get(changed.headers["location"])
    assert "密码已更新" in notice.text
    assert client.post(
        "/login",
        data={"username": "tester", "password": "Test-password-123"},
        follow_redirects=False,
    ).status_code == 401
    assert client.post(
        "/login",
        data={"username": "tester", "password": "New-test-password-456"},
        follow_redirects=False,
    ).status_code == 303


def test_change_password_requires_login_csrf_and_matching_confirmation(tmp_path):
    watchlist_path = tmp_path / "master_watchlist.sqlite3"
    _seed_watchlist(watchlist_path)
    app = create_app(
        market_database=tmp_path / "market.sqlite3",
        watchlist_database=watchlist_path,
        secure_cookies=False,
    )
    anonymous = TestClient(app)
    assert anonymous.get("/account/password", follow_redirects=False).headers[
        "location"
    ] == "/login?next=/account/password"
    assert anonymous.post("/account/password", data={}).status_code == 401

    client = _authenticated_client(app)
    assert client.post(
        "/account/password",
        data={
            "csrf_token": "wrong",
            "current_password": "Test-password-123",
            "new_password": "New-test-password-456",
            "new_password_confirmation": "New-test-password-456",
        },
    ).status_code == 403
    mismatch = client.post(
        "/account/password",
        data={
            "csrf_token": _csrf(client),
            "current_password": "Test-password-123",
            "new_password": "New-test-password-456",
            "new_password_confirmation": "Different-password-789",
        },
    )
    assert mismatch.status_code == 400
    assert "两次输入的新密码不一致" in mismatch.text
