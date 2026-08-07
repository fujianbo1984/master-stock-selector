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
from master_stock_selector.web.app import create_app


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
    return TestClient(
        create_app(
            market_database=tmp_path / "market.sqlite3",
            watchlist_database=watchlist_path,
        )
    )


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
        connection.executemany(
            """
            INSERT INTO daily_bars VALUES ('ashare', '000001.SZ', ?, ?, ?, ?, ?, 1000, 2000,
                                           'qfq', ?, 'qfq-scale-v1')
            """,
            [
                (f"2026-06-{day:02d}", 10 + day / 10, 11 + day / 10, 9 + day / 10, 10.5 + day / 10, f"2026-06-{day:02d}")
                for day in range(1, 31)
            ],
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
            INSERT INTO security_identity_snapshot (
                snapshot_date, symbol, name, industry, list_date, is_st,
                is_suspended, listing_status, trading_status, origin
            ) VALUES ('2026-07-31', ?, ?, '测试行业', '2020-01-01', ?, 0, 'L', 'trading', 'RECONSTRUCTED')
            """,
            (
                ("000002.SZ", "ST示例", 1),
                ("000003.SZ", "小盘示例", 0),
            ),
        )
        connection.executemany(
            """
            INSERT INTO security_industry_membership_snapshot (
                snapshot_date, symbol, taxonomy, industry_level, industry_code,
                industry_name, valid_from, valid_to, assignment_state, source,
                source_digest, origin
            ) VALUES (
                '2026-07-31', ?, 'SW2021', 'L3', '851911.SI',
                '股份制银行III', '2020-01-01', '', 'VERIFIED',
                'tushare:index_member_all', 'industry-digest', 'RECONSTRUCTED'
            )
            """,
            (("000002.SZ",), ("000003.SZ",)),
        )
        connection.execute(
            """
            INSERT INTO security_identity_snapshot (
                snapshot_date, symbol, name, industry, list_date, is_st,
                is_suspended, listing_status, trading_status, origin
            ) VALUES (
                '2026-08-01', '000001.SZ', 'ST未来名称', '银行', '1991-04-03',
                1, 0, 'L', 'trading', 'RECONSTRUCTED'
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
        '&amp;manual=all&amp;min_cap=0&amp;section=new-candidates"'
    ) in response.text
    assert 'href="/a/industries/851911.SI/chart?date=2026-07-31"' in response.text
    assert response.text.count("kpi-link") >= 6
    assert "state=PASSING" in response.text
    assert "state=NEW" in response.text
    assert "state=STABLE" in response.text
    assert "state=EXIT" in response.text
    assert "method=both&state=PASSING" in response.text
    assert "manual=FOCUS" in response.text
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
    for state in ("PASSING", "NEW", "STABLE", "EXIT"):
        filtered = client.get(f"/a/daily?date=2026-07-31&state={state}&min_cap=0")
        assert filtered.status_code == 200
        assert f'value="{state}" selected' in filtered.text
    passing = client.get("/a/daily?date=2026-07-31&state=PASSING&min_cap=0")
    assert "CURRENT / PASSING" in passing.text
    assert "当前仍符合 <strong>1</strong>" in passing.text
    assert "显示 1 / 1" in passing.text


def test_four_indices_show_weinstein_stage_and_minervini_stage2_without_composite(
    tmp_path,
):
    client = _client(tmp_path)

    response = client.get("/a/indices?date=2026-07-31")

    assert response.status_code == 200
    assert "Weinstein 完整阶段 + Minervini Stage 2 是/否" in response.text
    assert response.text.count("Minervini Stage 2") >= 4
    assert "不使用个股横截面 RS 排名" in response.text
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
        data={"manual_state": "FOCUS", "note": "等待年报后人工复核"},
        follow_redirects=True,
    )

    assert detail.status_code == 200
    assert "两种方法独立证据" in detail.text
    assert "30周线" in detail.text
    assert "股份制银行III（SW2021-L3）" in detail.text
    assert detail.text.index("manual-review-panel") < detail.text.index(
        "method-evidence-grid"
    )
    assert saved.status_code == 200
    assert "重点观察" in saved.text
    assert "等待年报后人工复核" in saved.text
    api = client.get("/api/a/watchlist/2026-07-31?method=both&min_cap=0").json()
    assert len(api["rows"]) == 1
    assert api["rows"][0]["both_pass"] is True


def test_chart_navigation_keeps_the_originating_watchlist_section_and_filters(tmp_path):
    watchlist_path = tmp_path / "master_watchlist.sqlite3"
    market_path = tmp_path / "market.sqlite3"
    _seed_watchlist(watchlist_path)
    _seed_filter_cases(watchlist_path)
    _seed_market_context(market_path)
    repository = WatchlistRepository(watchlist_path)
    repository.save_review("000001.SZ", "FOCUS", "保留既有备注")
    repository.save_review("000003.SZ", "FOCUS", "")
    client = TestClient(
        create_app(market_database=market_path, watchlist_database=watchlist_path)
    )

    daily = client.get("/a/daily?date=2026-07-31&min_cap=0")
    detail = client.get(
        "/a/stocks/000001.SZ?date=2026-07-31&manual=FOCUS&min_cap=0&section=focus-candidates"
    )
    chart = client.get(
        "/a/stocks/000001.SZ/chart?date=2026-07-31&manual=FOCUS&min_cap=0&section=focus-candidates"
    )

    assert daily.status_code == 200
    assert "section=focus-candidates" in daily.text
    assert detail.status_code == 200
    assert "我的重点观察 · 1 / 2" in detail.text
    assert "小盘示例 →" in detail.text
    assert "/a/stocks/000003.SZ?date=2026-07-31" in detail.text
    assert chart.status_code == 200
    assert "我的重点观察 <b>1 / 2</b>" in chart.text
    assert "今日观察名单" in chart.text
    assert "Minervini：符合" in chart.text
    assert "Weinstein：符合" in chart.text
    assert "/a/stocks/000003.SZ/chart?date=2026-07-31" in chart.text
    assert "data-chart-next" in chart.text
    assert 'aria-label="人工观察记录"' in chart.text
    assert "自动保存" in chart.text
    assert ">保存</button>" not in chart.text

    updated = client.post(
        "/a/stocks/000001.SZ/review",
        data={
            "manual_state": "DROPPED",
            "return_to": "/a/stocks/000001.SZ/chart?date=2026-07-31&manual=FOCUS&min_cap=0&section=focus-candidates#stock-chart",
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
    assert 'value="DROPPED" selected' in revised_chart.text
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
    assert "成分股等权代理K线" in chart.text
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
    assert "Minervini" in page.text
    assert "行业背景（仅人工参考）" in page.text
    assert "行业代理不参与 000001.SZ 的 Weinstein 或 Minervini 结论。" in page.text


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
    assert "已按 2026-07-31 当日风险标识排除 ST/*ST 1 只" in default_page.text
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
    client = TestClient(create_app(market_database=market_path, watchlist_database=watchlist_path))

    page = client.get("/a/stocks/000001.SZ/chart?date=2026-06-30")
    chart = client.get("/api/a/stocks/000001.SZ/chart?date=2026-06-30&limit=30")

    assert page.status_code == 200
    assert "本地图表只用于人工复核" in page.text
    assert "lightweight-charts-5.1.0.js" in page.text
    assert 'class="chart-workbench-title"' in page.text
    assert "https://cn.tradingview.com/chart/?symbol=SZSE%3A000001" in page.text
    assert "https://stockpage.10jqka.com.cn/000001/" in page.text
    assert 'data-kc-source' in page.text
    assert chart.status_code == 200
    payload = chart.json()
    assert payload["status"] == "OK"
    assert payload["adjustment"] == "qfq"
    assert payload["price_scale_id"] == "qfq-scale-v1"
    assert len(payload["bars"]) == 30
    assert payload["keltner"][-1]["upper"] is not None
    open_source = client.get(
        "/api/a/stocks/000001.SZ/chart?date=2026-06-30&limit=30&source=open"
    ).json()
    assert open_source["keltner"][-1]["basis"] != payload["keltner"][-1]["basis"]
    saved = client.post(
        "/api/a/stocks/000001.SZ/chart/drawings",
        json={
            "drawing_id": "line-1",
            "price_scale_id": "qfq-scale-v1",
            "tool": "trendline",
            "anchors": [
                {"date": "2026-06-10", "price": 11.5},
                {"logical": 34.5, "price": 12.5},
            ],
        },
    )
    assert saved.status_code == 200
    drawings = client.get(
        "/api/a/stocks/000001.SZ/chart?date=2026-06-30&limit=30"
    ).json()["drawings"]
    assert drawings[0]["anchors"][1]["logical"] == 34.5
    deleted = client.delete(
        "/api/a/stocks/000001.SZ/chart/drawings/line-1?price_scale_id=qfq-scale-v1"
    )
    assert deleted.json() == {"deleted": True}


def test_daily_context_cache_invalidates_after_manual_review_and_exposes_timing(tmp_path):
    watchlist_path = tmp_path / "master_watchlist.sqlite3"
    market_path = tmp_path / "market.sqlite3"
    _seed_watchlist(watchlist_path)
    _seed_market_context(market_path)
    app = create_app(market_database=market_path, watchlist_database=watchlist_path)
    market_reader = app.state.market_reader
    original = market_reader.safe_stock_market_metrics
    calls: list[bool] = []

    def traced_metrics(*args: Any, **kwargs: Any) -> dict[str, dict[str, Any]]:
        calls.append(bool(kwargs.get("include_liquidity", True)))
        return original(*args, **kwargs)

    market_reader.safe_stock_market_metrics = traced_metrics
    client = TestClient(app)

    first = client.get("/a/daily?date=2026-07-31&min_cap=0")
    second = client.get("/a/daily?date=2026-07-31&min_cap=0")
    assert first.status_code == second.status_code == 200
    assert calls == [False]
    assert first.headers["server-timing"].startswith("app;dur=")

    saved = client.post(
        "/a/stocks/000001.SZ/review",
        data={"manual_state": "FOCUS", "note": "缓存失效后仍应显示"},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    refreshed = client.get("/a/daily?date=2026-07-31&min_cap=0")
    assert calls == [False, False]
    assert "重点观察" in refreshed.text

    static = client.get("/static/vendor/lightweight-charts-5.1.0.js")
    assert static.status_code == 200
    assert static.headers["cache-control"] == "public, max-age=31536000, immutable"
