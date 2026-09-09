import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from master_stock_selector.web.trading_plans import PlanConflict
from master_stock_selector.web.users import SESSION_COOKIE, UserRepository
from tests.test_watchlist_web import _authenticated_client, _client, _csrf


def _confirm_all(repository: UserRepository, user_id: str, session_id: str) -> int:
    while True:
        question = repository.trading_plans.next(user_id, session_id)
        if question["status"] == "COMPLETE":
            return int(question["revision"])
        status = repository.trading_plans.status(user_id, session_id)
        assert status is not None
        identifier = question["question"]["question_id"]
        proposed = status["answers"].get(identifier, {}).get("value")
        choices = question["question"].get("choices") or []
        value = proposed or (choices[0]["value"] if choices else "会员明确确认")
        repository.trading_plans.answer(
            user_id,
            session_id,
            {
                "question_id": identifier,
                "expected_revision": status["revision"],
                "decision": {"state": "CONFIRMED", "value": value},
            },
        )


def _published_demo(repository: UserRepository, user_id: str) -> dict:
    session = repository.trading_plans.start(user_id, template="personal-a-share-v1")
    revision = _confirm_all(repository, user_id, session["session_id"])
    return repository.trading_plans.publish(user_id, session["session_id"], revision)


def test_demo_grill_publishes_frozen_private_version(tmp_path: Path) -> None:
    repository = UserRepository(tmp_path / "users.sqlite3")
    repository.initialize()
    alice = repository.create_user("alice", "Secure-password-123")
    bob = repository.create_user("bob", "Secure-password-123")
    session = repository.trading_plans.start(alice, template="personal-a-share-v1")
    assert (
        repository.trading_plans.validate(alice, session["session_id"])["status"]
        == "NEEDS_RESOLUTION"
    )
    revision = _confirm_all(repository, alice, session["session_id"])
    published = repository.trading_plans.publish(alice, session["session_id"], revision)
    assert published["version"] == "v0.1"
    current = repository.trading_plans.current(alice)
    assert current is not None
    risk_codes = {item["code"] for item in current["risk_profile"]}
    assert {
        "MARGIN_ENABLED",
        "RISK_2_PERCENT",
        "UNBOUNDED_PORTFOLIO_RISK",
        "NO_DRAWDOWN_GUARD",
        "NO_PAUSE_GUARD",
        "NO_EVENT_FILTER",
        "NO_LIQUIDITY_FILTER",
        "T1_STOP_GAP",
        "FULL_R_SLIPPAGE",
        "GROSS_EXPECTANCY_ONLY",
    } <= risk_codes
    frozen = json.dumps(current["content"], ensure_ascii=False, sort_keys=True)
    assert hashlib.sha256(frozen.encode()).hexdigest() == current["content_hash"]
    setup_layer = current["content"]["layers"]["setups"]
    assert {"A1.rules", "A3.rules", "B1.rules", "B2.rules", "C1.rules", "C2.rules"} <= set(
        setup_layer
    )
    assert "至少60个交易日" in setup_layer["C1.rules"]["value"]
    assert "分别记录子类型" in setup_layer["C1.rules"]["value"]
    assert [item["setup_id"] for item in current["setup_catalog"] if item["enabled"]] == [
        "A1", "A3", "B1", "B2", "C1", "C2"
    ]
    setup_section = next(
        section for section in current["display_sections"] if section["key"] == "setups"
    )
    assert [rule["title"] for rule in setup_section["rules"]] == ["重入与叠加"]
    assert next(item for item in current["setup_cards"] if item["code"] == "C2")[
        "name"
    ] == "底部区间突破后的首次回调"
    risk_section = next(
        section for section in current["display_sections"] if section["key"] == "risk"
    )
    position_rule = next(
        rule for rule in risk_section["rules"] if rule["id"] == "risk.position_sizing"
    )
    assert position_rule["value"] == "每笔使用完整 1R"
    assert repository.trading_plans.current(bob) is None
    assert repository.trading_plans.show(bob, published["version_id"]) is None
    with pytest.raises(PlanConflict):
        repository.trading_plans.publish(alice, session["session_id"], revision)


def test_owner_plan_sharing_versions_and_global_revoke(tmp_path: Path) -> None:
    repository = UserRepository(tmp_path / "users.sqlite3")
    repository.initialize()
    owner = repository.create_user("我不是来玩的", "Secure-password-123")
    viewer = repository.create_user("viewer", "Secure-password-123")
    first = _published_demo(repository, owner)

    shared_first = repository.trading_plans.share_current(owner, "首次共享执行边界。")
    assert shared_first["version_id"] == first["version_id"]
    assert shared_first["share_status"] == "ACTIVE"
    assert repository.trading_plans.shared_by_id(viewer, shared_first["share_id"]) is None

    session = repository.trading_plans.start(owner, from_version="current")
    revision = _confirm_all(repository, owner, session["session_id"])
    second = repository.trading_plans.publish(owner, session["session_id"], revision)
    shared_second = repository.trading_plans.share_current(owner, "共享新版本。")
    visible = repository.trading_plans.shared_versions(owner)

    assert shared_second["version_id"] == second["version_id"]
    assert [item["share_status"] for item in visible] == ["ACTIVE", "SUPERSEDED"]
    assert repository.trading_plans.shared_by_id(owner, shared_first["share_id"]) is not None
    assert repository.trading_plans.revoke_shares(owner) == 2
    assert repository.trading_plans.current_share(owner) is None
    assert repository.trading_plans.shared_versions(owner) == []
    assert repository.trading_plans.shared_by_id(owner, shared_second["share_id"]) is None


def test_shared_plan_uses_shanghai_display_time_across_utc_date_boundary(
    tmp_path: Path,
) -> None:
    repository = UserRepository(tmp_path / "users.sqlite3")
    repository.initialize()
    owner = repository.create_user("我不是来玩的", "Secure-password-123")
    _published_demo(repository, owner)
    share = repository.trading_plans.share_current(owner)

    with repository.connect() as connection:
        connection.execute(
            "UPDATE user_trading_plan_share SET published_at=? WHERE share_id=?",
            ("2026-08-25 23:31:26", share["share_id"]),
        )

    displayed = repository.trading_plans.current_share(owner)
    assert displayed is not None
    assert displayed["published_at"] == "2026-08-26 07:31:26"


def test_only_site_owner_can_share_plan_and_viewers_see_sanitized_page(
    tmp_path: Path,
) -> None:
    viewer = _client(tmp_path)
    users = viewer.app.state.user_repository
    owner_id = users.create_user("我不是来玩的", "Test-password-123", "站长")
    _published_demo(users, owner_id)
    owner = _authenticated_client(viewer.app, "我不是来玩的")

    share_dialog = owner.get("/a/plan")
    assert share_dialog.status_code == 200
    assert 'data-plan-share-statement' in share_dialog.text
    assert 'data-plan-share-count>0 / 300' in share_dialog.text
    assert 'data-plan-share-submit disabled' in share_dialog.text
    assert 'app.css?v=20260907-industry-ledger-v1' in share_dialog.text

    assert (
        viewer.post(
            "/a/plan/share",
            data={"csrf_token": _csrf(viewer), "statement": "读者不能代理共享"},
        ).status_code
        == 403
    )
    published = owner.post(
        "/a/plan/share",
        data={"csrf_token": _csrf(owner), "statement": "本次重点收紧退出边界。"},
        follow_redirects=False,
    )
    assert published.status_code == 303
    share = users.trading_plans.current_share(owner_id)
    assert share is not None

    feed = viewer.get("/a/owner")
    assert feed.status_code == 200
    assert 'data-owner-view="plan"' in feed.text
    assert '交易计划 v0.1' in feed.text
    assert '本次重点收紧退出边界' in feed.text
    detail = viewer.get(share["share_href"])
    assert detail.status_code == 200
    assert detail.headers["cache-control"] == "private, no-store"
    assert detail.headers["x-robots-tag"] == "noindex, nofollow"
    assert "A1 突破前入场" in detail.text
    assert "Grill 过程" in detail.text
    assert share["version_id"] not in detail.text
    assert share["content_hash"] not in detail.text
    assert "evaluation_cycle_id" not in detail.text

    stopped = owner.post(
        "/a/plan/share/stop",
        data={"csrf_token": _csrf(owner)},
        follow_redirects=False,
    )
    assert stopped.status_code == 303
    assert viewer.get(share["share_href"]).status_code == 404
    assert '交易计划 v0.1' not in viewer.get("/a/owner").text


def test_grill_requires_confirmed_answer_revision_and_consistent_rules(
    tmp_path: Path,
) -> None:
    repository = UserRepository(tmp_path / "users.sqlite3")
    repository.initialize()
    user = repository.create_user("alice", "Secure-password-123")
    session = repository.trading_plans.start(user)
    first = repository.trading_plans.next(user, session["session_id"])
    assert first["question"]["question_id"] == "scope.market"
    with pytest.raises(PlanConflict):
        repository.trading_plans.answer(
            user,
            session["session_id"],
            {
                "question_id": "scope.market",
                "expected_revision": 3,
                "decision": {"state": "CONFIRMED", "value": "A股"},
            },
        )
    with pytest.raises(ValueError, match="expected_revision"):
        repository.trading_plans.answer(
            user,
            session["session_id"],
            {
                "question_id": "scope.market",
                "decision": {"state": "CONFIRMED", "value": "A股"},
            },
        )

    conflict = repository.trading_plans.start(user, template="personal-a-share-v1")
    status = repository.trading_plans.status(user, conflict["session_id"])
    assert status is not None
    repository.trading_plans.answer(
        user,
        conflict["session_id"],
        {
            "question_id": "risk.position_sizing",
            "expected_revision": status["revision"],
            "decision": {"state": "CONFIRMED", "value": "FULL_R"},
        },
    )
    status = repository.trading_plans.status(user, conflict["session_id"])
    assert status is not None
    repository.trading_plans.answer(
        user,
        conflict["session_id"],
        {
            "question_id": "portfolio.cash_shortage",
            "expected_revision": status["revision"],
            "decision": {"state": "CONFIRMED", "value": "REDUCE"},
        },
    )
    validation = repository.trading_plans.validate(user, conflict["session_id"])
    assert validation["status"] == "NEEDS_RESOLUTION"
    assert any(
        issue.get("conflict_id") == "cash.full_r_conflict"
        for issue in validation["blocking_issues"]
    )


def test_risk_profile_uses_confirmed_rule_semantics_not_exact_phrase(tmp_path: Path) -> None:
    repository = UserRepository(tmp_path / "users.sqlite3")
    repository.initialize()
    answers = {
        "event.filter": {
            "state": "CONFIRMED",
            "value": "不设置财报、业绩预告、解禁、除权、复牌等事件过滤",
        },
        "scope.universe": {"state": "CONFIRMED", "value": "不设置流动性过滤"},
        "execution.exit": {"state": "CONFIRMED", "value": "A股T＋1当日无法卖出止损"},
        "review.recording": {
            "state": "CONFIRMED",
            "value": "只评价未扣佣金、融资利息和滑点的毛平均R",
        },
        "risk.risk_percent": {"state": "PROPOSED", "value": "2%"},
    }
    profile = repository.trading_plans._warnings(answers)
    codes = {item["code"] for item in profile}
    assert codes == {
        "NO_EVENT_FILTER",
        "NO_LIQUIDITY_FILTER",
        "T1_STOP_GAP",
        "GROSS_EXPECTANCY_ONLY",
    }
    assert all(item["source_question_ids"] for item in profile)


def test_minor_versions_reuse_cycle_and_trades_bind_to_active_version(
    tmp_path: Path,
) -> None:
    repository = UserRepository(tmp_path / "users.sqlite3")
    repository.initialize()
    alice = repository.create_user("alice", "Secure-password-123")
    bob = repository.create_user("bob", "Secure-password-123")
    first = _published_demo(repository, alice)

    session = repository.trading_plans.start(alice, from_version="current")
    revision = _confirm_all(repository, alice, session["session_id"])
    assert (
        repository.trading_plans.validate(alice, session["session_id"])["change_class"] == "MINOR"
    )
    minor = repository.trading_plans.publish(alice, session["session_id"], revision)
    assert minor["version"] == "v0.2"
    assert minor["evaluation_cycle"] == first["evaluation_cycle"]

    buy_id = repository.record_trade(
        alice,
        traded_on="2026-08-01",
        symbol="000001.SZ",
        side="BUY",
        quantity=100,
        price=10,
        fee=0,
        method="MANUAL",
        setup_method="PRE_BREAKOUT_BASE",
        stop_price=9,
    )
    buy = repository.trade_execution(alice, buy_id)
    assert buy is not None
    assert buy["plan_version_id"] == minor["version_id"]
    assert buy["setup_code"] == "A1"
    assert buy["setup_kind"] == "PLAN"
    assert buy["plan_setup_id"] == "A1"
    assert buy["setup_name_snapshot"] == "突破前入场"
    raw_token, _ = repository.create_api_token(alice, "stop-editor", scopes=("trades:write",))
    principal = repository.api_token_user(raw_token)
    assert principal is not None
    repository.update_trade_stop(
        alice,
        buy_id,
        9.8,
        expected_revision=1,
        principal=principal,
        request_id="move-stop",
    )
    repository.record_trade(
        alice,
        traded_on="2026-08-02",
        symbol="000001.SZ",
        side="SELL",
        quantity=50,
        price=11,
        fee=0,
        method="MANUAL",
    )
    partial_review = repository.trade_review(alice, {"000001.SZ": "平安银行"})
    assert partial_review["by_plan_setup"] == {}
    assert partial_review["completed_lot_summary"]["count"] == 0
    assert partial_review["completed_lot_summary"]["net_pnl"] == 0
    assert partial_review["realized_summary"]["net_pnl"] == 50.0
    repository.record_trade(
        alice,
        traded_on="2026-08-03",
        symbol="000001.SZ",
        side="SELL",
        quantity=50,
        price=11,
        fee=0,
        method="MANUAL",
    )
    review = repository.trade_review(alice, {"000001.SZ": "平安银行"})
    group = review["by_plan_setup"][f"{minor['evaluation_cycle']}:A1"]
    assert group["plan_versions"] == ["v0.2"]
    assert group["summary"]["count"] == 1
    assert group["summary"]["gross_avg_r"] == 1.0
    assert group["summary"]["r_count"] == 1
    assert review["realized_summary"]["net_pnl"] == 100.0
    with pytest.raises(ValueError, match="不属于当前用户"):
        repository.record_trade(
            bob,
            traded_on="2026-08-01",
            symbol="000002.SZ",
            side="BUY",
            quantity=100,
            price=10,
            fee=0,
            method="MANUAL",
            plan_version_id=minor["version_id"],
        )


def test_trade_review_reconciles_fifo_fragments_fees_and_completed_buy_lots(
    tmp_path: Path,
) -> None:
    repository = UserRepository(tmp_path / "users.sqlite3")
    repository.initialize()
    user_id = repository.create_user("owner", "Secure-password-123")

    repository.record_trade(
        user_id,
        traded_on="2026-08-01",
        symbol="000001.SZ",
        side="BUY",
        quantity=100,
        price=10,
        fee=1,
        method="MANUAL",
    )
    repository.record_trade(
        user_id,
        traded_on="2026-08-02",
        symbol="000001.SZ",
        side="BUY",
        quantity=100,
        price=20,
        fee=1,
        method="MANUAL",
    )
    repository.record_trade(
        user_id,
        traded_on="2026-08-03",
        symbol="000001.SZ",
        side="SELL",
        quantity=150,
        price=30,
        fee=3,
        method="MANUAL",
    )

    partial = repository.trade_review(user_id, {"000001.SZ": "平安银行"})
    assert partial["realized_summary"] == {
        "net_pnl": 2495.5,
        "matched_quantity": 150,
        "fifo_fragment_count": 2,
        "sell_execution_count": 1,
    }
    assert partial["completed_lot_summary"]["count"] == 1
    assert partial["completed_lot_summary"]["net_pnl"] == 1997.0
    assert partial["open_positions"][0]["quantity"] == 50
    assert partial["open_positions"][0]["cost"] == 1000.5
    assert sum(item["pnl_cents"] for item in partial["closed_fragments"]) == 249_550

    repository.record_trade(
        user_id,
        traded_on="2026-08-04",
        symbol="000001.SZ",
        side="SELL",
        quantity=50,
        price=25,
        fee=1,
        method="MANUAL",
    )
    complete = repository.trade_review(user_id, {"000001.SZ": "平安银行"})
    assert complete["realized_summary"]["net_pnl"] == 2744.0
    assert complete["completed_lot_summary"]["count"] == 2
    assert complete["completed_lot_summary"]["net_pnl"] == 2744.0
    assert complete["open_positions"] == []
    assert sum(item["pnl_cents"] for item in complete["completed_lots"]) == 274_400


def test_trade_review_excludes_unmatched_sell_from_realized_summary(tmp_path: Path) -> None:
    repository = UserRepository(tmp_path / "users.sqlite3")
    repository.initialize()
    user_id = repository.create_user("owner", "Secure-password-123")
    repository.record_trade(
        user_id,
        traded_on="2026-08-01",
        symbol="000001.SZ",
        side="SELL",
        quantity=100,
        price=10,
        fee=2,
        method="MANUAL",
        permit_unmatched_sell=True,
    )

    review = repository.trade_review(user_id, {"000001.SZ": "平安银行"})

    assert review["realized_summary"]["net_pnl"] == 0
    assert review["realized_summary"]["matched_quantity"] == 0
    assert review["completed_lot_summary"]["count"] == 0
    assert review["unmatched_sells"][0]["fee"] == 2.0


def test_personal_plan_controls_trade_setup_options_and_other_is_default(tmp_path: Path) -> None:
    repository = UserRepository(tmp_path / "users.sqlite3")
    repository.initialize()
    alice = repository.create_user("alice", "Secure-password-123")
    bob = repository.create_user("bob", "Secure-password-123")
    session = repository.trading_plans.start(alice, template="personal-a-share-v1")
    while True:
        question = repository.trading_plans.next(alice, session["session_id"])
        if question["status"] == "COMPLETE":
            revision = int(question["revision"])
            break
        status = repository.trading_plans.status(alice, session["session_id"])
        assert status is not None
        identifier = question["question"]["question_id"]
        proposed = status["answers"].get(identifier, {}).get("value")
        value = "A1,B1" if identifier == "setup.enabled" else proposed
        repository.trading_plans.answer(
            alice,
            session["session_id"],
            {
                "question_id": identifier,
                "expected_revision": status["revision"],
                "decision": {"state": "CONFIRMED", "value": value},
            },
        )
    repository.trading_plans.publish(alice, session["session_id"], revision)

    assert repository.trade_setup_options(alice) == [
        {"value": "OTHER", "label": "其他（未归入交易计划）"},
        {"value": "A1", "label": "A1 · 突破前入场"},
        {"value": "B1", "label": "B1 · 简单回调"},
    ]
    assert repository.trade_setup_options(bob) == [
        {"value": "OTHER", "label": "其他（未归入交易计划）"}
    ]
    other_id = repository.record_trade(
        alice, traded_on="2026-08-01", symbol="000001.SZ", side="BUY",
        quantity=100, price=10, fee=0, method="MANUAL",
    )
    other = repository.trade_execution(alice, other_id)
    assert other is not None
    assert other["setup_kind"] == "OTHER"
    assert other["plan_version_id"]
    with pytest.raises(ValueError, match="未启用"):
        repository.record_trade(
            alice, traded_on="2026-08-01", symbol="000002.SZ", side="BUY",
            quantity=100, price=10, fee=0, method="MANUAL", plan_setup_id="A3",
        )


def test_dynamic_setup_keeps_stable_identity_snapshots_and_can_be_archived(
    tmp_path: Path,
) -> None:
    repository = UserRepository(tmp_path / "users.sqlite3")
    repository.initialize()
    user_id = repository.create_user("owner", "Secure-password-123")
    _published_demo(repository, user_id)

    draft = repository.trading_plans.start(user_id, from_version="current")
    saved = repository.trading_plans.save_setup(
        user_id,
        draft["session_id"],
        {
            "code": "D1",
            "name": "自定义突破",
            "enabled": True,
            "order": 20,
            "structure": "三次收缩后靠近压力位",
            "trigger": "突破信号K线高点",
            "initial_stop": "结构低点下方",
            "first_target": "1R减半",
        },
        expected_revision=draft["revision"],
    )
    custom = next(item for item in saved["setups"] if item["code"] == "D1")
    setup_id = custom["setup_id"]
    revision = _confirm_all(repository, user_id, draft["session_id"])
    first = repository.trading_plans.publish(user_id, draft["session_id"], revision)
    first_plan = repository.trading_plans.current(user_id)
    first_setup = next(item for item in first_plan["setup_catalog"] if item["setup_id"] == setup_id)

    old_trade_id = repository.record_trade(
        user_id, traded_on="2026-08-01", symbol="000001.SZ", side="BUY",
        quantity=100, price=10, fee=0, method="MANUAL", plan_setup_id=setup_id,
        stop_price=9,
    )
    rename = repository.trading_plans.start(user_id, from_version="current")
    renamed_card = {**first_setup, "code": "D2", "name": "自定义突破修订"}
    renamed = repository.trading_plans.save_setup(
        user_id,
        rename["session_id"],
        renamed_card,
        expected_revision=rename["revision"],
    )
    second = repository.trading_plans.publish(
        user_id, rename["session_id"], renamed["revision"]
    )
    second_plan = repository.trading_plans.current(user_id)
    second_setup = next(item for item in second_plan["setup_catalog"] if item["setup_id"] == setup_id)
    assert second["evaluation_cycle"] == first["evaluation_cycle"]
    assert second_setup["evaluation_cycle_id"] == first_setup["evaluation_cycle_id"]
    assert repository.trade_execution(user_id, old_trade_id)["setup_code_snapshot"] == "D1"
    assert {item["value"] for item in repository.trade_setup_options(user_id)} >= {setup_id}

    archive = repository.trading_plans.start(user_id, from_version="current")
    archived = repository.trading_plans.archive_setup(
        user_id,
        archive["session_id"],
        setup_id,
        expected_revision=archive["revision"],
    )
    repository.trading_plans.publish(
        user_id, archive["session_id"], archived["revision"]
    )
    assert setup_id not in {item["value"] for item in repository.trade_setup_options(user_id)}
    with pytest.raises(ValueError, match="未启用"):
        repository.record_trade(
            user_id, traded_on="2026-08-02", symbol="000002.SZ", side="BUY",
            quantity=100, price=10, fee=0, method="MANUAL", plan_setup_id=setup_id,
        )


def test_setup_cycles_restart_only_for_changed_setup_or_common_execution_rule(
    tmp_path: Path,
) -> None:
    repository = UserRepository(tmp_path / "users.sqlite3")
    repository.initialize()
    user_id = repository.create_user("owner", "Secure-password-123")
    _published_demo(repository, user_id)
    base = repository.trading_plans.current(user_id)
    base_cycles = {
        item["setup_id"]: item["evaluation_cycle_id"] for item in base["setup_catalog"]
    }

    c2_draft = repository.trading_plans.start(user_id, from_version="current")
    c2 = next(item for item in c2_draft["setups"] if item["setup_id"] == "C2")
    c2["rules"] += " 新增：入场前必须保留区间上沿截图。"
    updated = repository.trading_plans.save_setup(
        user_id,
        c2_draft["session_id"],
        c2,
        expected_revision=c2_draft["revision"],
    )
    repository.trading_plans.publish(
        user_id, c2_draft["session_id"], updated["revision"]
    )
    changed = repository.trading_plans.current(user_id)
    changed_cycles = {
        item["setup_id"]: item["evaluation_cycle_id"] for item in changed["setup_catalog"]
    }
    assert changed_cycles["A1"] == base_cycles["A1"]
    assert changed_cycles["C2"] != base_cycles["C2"]

    common = repository.trading_plans.start(user_id, from_version="current")
    repository.trading_plans.answer(
        user_id,
        common["session_id"],
        {
            "question_id": "execution.entry",
            "expected_revision": common["revision"],
            "decision": {
                "state": "CONFIRMED",
                "value": "公共买入规则实质修改：仅使用下一交易日有效的条件单。",
            },
        },
    )
    common_status = repository.trading_plans.status(user_id, common["session_id"])
    repository.trading_plans.publish(
        user_id, common["session_id"], common_status["revision"]
    )
    final = repository.trading_plans.current(user_id)
    final_cycles = {
        item["setup_id"]: item["evaluation_cycle_id"] for item in final["setup_catalog"]
    }
    assert all(final_cycles[key] != changed_cycles[key] for key in changed_cycles)


def test_c2_generic_observation_is_frozen_with_trade(tmp_path: Path) -> None:
    repository = UserRepository(tmp_path / "users.sqlite3")
    repository.initialize()
    user_id = repository.create_user("owner", "Secure-password-123")
    _published_demo(repository, user_id)
    repository.save_setup_observation(
        user_id,
        "000001.SZ",
        "C2",
        {
            "status": "READY",
            "as_of_date": "2026-08-01",
            "structure_started_on": "2026-04-01",
            "signal_on": "2026-07-20",
            "reference_low": "9",
            "reference_high": "12",
            "trigger_price": "12.51",
            "invalidation_price": "11.99",
            "summary": "长期区间突破第一腿后，首次回调收盘仍在上沿之上。",
        },
    )
    buy_id = repository.record_trade(
        user_id, traded_on="2026-08-02", symbol="000001.SZ", side="BUY",
        quantity=100, price=12.6, fee=0, method="MANUAL", plan_setup_id="C2",
        stop_price=11.9,
    )
    repository.clear_setup_observation(user_id, "000001.SZ", "C2")
    trade = repository.trade_execution(user_id, buy_id)
    assert trade["setup_code_snapshot"] == "C2"
    assert trade["snapshot"]["setup_observation"]["status"] == "READY"
    assert "首次回调" in trade["snapshot"]["setup_observation"]["evidence"]["summary"]
    assert repository.setup_observations(user_id, "000001.SZ") == []


def test_dynamic_plan_editor_and_c2_observation_are_available_on_web(tmp_path: Path) -> None:
    client = _client(tmp_path)
    repository = client.app.state.user_repository
    user = repository.session_user(client.cookies.get(SESSION_COOKIE))
    assert user is not None
    _published_demo(repository, user.user_id)
    draft = repository.trading_plans.start(user.user_id, from_version="current")

    editor = client.get(f"/a/plan?session={draft['session_id']}")
    assert editor.status_code == 200
    assert 'id="setup-editor"' in editor.text
    assert "动态形态目录" in editor.text
    assert "C2" in editor.text
    assert "底部区间突破后的首次回调" in editor.text
    assert "/setups/save" in editor.text

    stock = client.get("/a/stocks/000001.SZ?date=2026-08-01")
    assert stock.status_code == 200
    assert "按形态保存入场前证据" in stock.text
    assert "/setups/C2/observation" in stock.text
    response = client.post(
        "/a/stocks/000001.SZ/setups/C2/observation",
        data={
            "csrf_token": _csrf(client),
            "status": "READY",
            "as_of_date": "2026-08-01",
            "structure_started_on": "2026-04-01",
            "signal_on": "2026-07-20",
            "reference_low": "9",
            "reference_high": "12",
            "trigger_price": "12.51",
            "invalidation_price": "11.99",
            "summary": "突破后首次回调未收回区间。",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("#setup-observations")
    saved = repository.setup_observations(user.user_id, "000001.SZ")
    assert saved[0]["setup_id"] == "C2"
    assert saved[0]["status"] == "READY"


def test_c1_trade_freezes_manual_structure_and_reviews_variants_separately(tmp_path: Path):
    repository = UserRepository(tmp_path / "users.sqlite3")
    repository.initialize()
    user_id = repository.create_user("owner", "Secure-password-123")
    plan = _published_demo(repository, user_id)
    repository.save_c1_record(
        user_id,
        "000001.SZ",
        {
            "as_of_date": "2026-08-01",
            "range_started_on": "2026-04-01",
            "lower_bound": "9",
            "upper_bound": "13",
            "atr14": "1",
            "support_test_count": "2",
            "structure_state": "READY",
            "entry_variant": "SECOND",
            "note": "强下跌后等待二次测试",
        },
    )
    buy_id = repository.record_trade(
        user_id,
        traded_on="2026-08-01",
        symbol="000001.SZ",
        side="BUY",
        quantity=100,
        price=10,
        fee=0,
        method="MANUAL",
        plan_setup_id="C1",
        stop_price=9,
    )
    repository.clear_c1_record(user_id, "000001.SZ")
    repository.record_trade(
        user_id,
        traded_on="2026-08-02",
        symbol="000001.SZ",
        side="SELL",
        quantity=100,
        price=11,
        fee=0,
        method="MANUAL",
    )

    buy = repository.trade_execution(user_id, buy_id)
    assert buy is not None
    assert buy["setup_method"] == "FAILED_TEST"
    assert buy["setup_kind"] == "PLAN"
    assert buy["plan_setup_id"] == "C1"
    assert buy["setup_name_snapshot"] == "区间失败测试"
    assert buy["snapshot"]["c1_record"]["entry_variant"] == "SECOND"
    group = repository.trade_review(user_id, {"000001.SZ": "平安银行"})[
        "by_plan_setup"
    ][f"{plan['evaluation_cycle']}:C1"]
    assert group["summary"]["count"] == 1
    assert group["summary"]["gross_avg_r"] == 1.0
    assert group["by_entry_variant"]["SECOND"]["summary"]["count"] == 1


def test_plan_api_scopes_freeze_and_web_isolation(tmp_path: Path) -> None:
    client = _client(tmp_path)
    users = client.app.state.user_repository
    user = users.session_user(client.cookies.get(SESSION_COOKIE))
    assert user is not None
    token, _ = users.create_api_token(
        user.user_id,
        "plan-agent",
        scopes=("plans:read", "plans:write", "plans:publish"),
    )
    write_token, _ = users.create_api_token(
        user.user_id,
        "plan-writer",
        scopes=("plans:read", "plans:write"),
    )
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/v1/me", headers=headers).status_code == 200
    started = client.post(
        "/api/v1/plans/grill/sessions",
        headers=headers,
        json={"template": "personal-a-share-v1"},
    )
    session_id = started.json()["session_id"]
    revision = _confirm_all(users, user.user_id, session_id)
    path = f"/api/v1/plans/grill/sessions/{session_id}"
    for malformed_revision in (True, 1.9, "1"):
        assert (
            client.post(
                path + "/publish",
                headers=headers,
                json={"expected_revision": malformed_revision},
            ).status_code
            == 422
        )
    assert (
        client.post(
            path + "/publish",
            headers={"Authorization": f"Bearer {write_token}"},
            json={"expected_revision": revision},
        ).status_code
        == 403
    )
    assert (
        client.post(
            path + "/publish", headers=headers, json={"expected_revision": revision}
        ).status_code
        == 200
    )
    assert (
        client.get("/api/v1/plans/current", headers=headers).json()["plan"]["version_number"]
        == "v0.1"
    )
    assert (
        client.post(
            path + "/answers",
            headers=headers,
            json={
                "question_id": "scope.market",
                "expected_revision": revision,
                "decision": {"state": "CONFIRMED", "value": "A股"},
            },
        ).status_code
        == 409
    )

    bob_id = users.create_user("bob", "Secure-password-123")
    bob_token, _ = users.create_api_token(bob_id, "bob-plan", scopes=("plans:read",))
    assert client.get(path, headers={"Authorization": f"Bearer {bob_token}"}).status_code == 404

    anonymous = TestClient(client.app)
    assert anonymous.get("/a/plan", follow_redirects=False).status_code == 303
    page = client.get("/a/plan")
    assert page.status_code == 200
    assert "风险画像" in page.text
    assert "A1 突破前入场" in page.text
    assert "A3 首次回调" in page.text
    assert "B1 简单回调" in page.text
    assert "B2 复杂回调" in page.text
    stock_page = client.get("/a/stocks/000001.SZ?date=2026-07-31")
    assert stock_page.status_code == 200
    assert '<option value="OTHER" selected>其他（未归入交易计划）</option>' in stock_page.text
    for code in ("A1", "A3", "B1", "B2"):
        assert f'<option value="{code}"' in stock_page.text
    assert "每笔使用完整 1R" in page.text
    assert ">FULL_R<" not in page.text
    assert "从完整模板开始" in page.text
    assert 'href="/a/plan' in page.text
    bob = TestClient(client.app)
    assert (
        bob.post(
            "/login",
            data={"username": "bob", "password": "Secure-password-123"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    assert bob.get(f"/a/plan?session={session_id}").status_code == 404
    assert client.post("/a/plan/grill/start", data={"template": "blank"}).status_code == 403
    assert (
        client.post(
            "/a/plan/grill/start",
            data={"csrf_token": _csrf(client), "template": "blank"},
            follow_redirects=False,
        ).status_code
        == 303
    )


def test_stale_grill_session_cannot_overwrite_new_head(tmp_path: Path) -> None:
    repository = UserRepository(tmp_path / "users.sqlite3")
    repository.initialize()
    user = repository.create_user("alice", "Secure-password-123")
    _published_demo(repository, user)
    first = repository.trading_plans.start(user, from_version="current")
    stale = repository.trading_plans.start(user, from_version="current")
    first_revision = _confirm_all(repository, user, first["session_id"])
    stale_revision = _confirm_all(repository, user, stale["session_id"])
    repository.trading_plans.publish(user, first["session_id"], first_revision)
    with pytest.raises(PlanConflict, match="最新版本"):
        repository.trading_plans.publish(user, stale["session_id"], stale_revision)
    with pytest.raises(PlanConflict, match="当前生效版本"):
        repository.trading_plans.start(user, from_version=first["base_version_id"])


def test_trade_edits_keep_plan_binding_consistent(tmp_path: Path) -> None:
    repository = UserRepository(tmp_path / "users.sqlite3")
    repository.initialize()
    user = repository.create_user("alice", "Secure-password-123")
    legacy_buy = repository.record_trade(
        user,
        traded_on="2026-07-31",
        symbol="000003.SZ",
        side="BUY",
        quantity=100,
        price=10,
        fee=0,
        method="MANUAL",
        setup_method="PRE_BREAKOUT_BASE",
    )
    assert repository.trade_execution(user, legacy_buy)["plan_version_id"] is None
    plan = _published_demo(repository, user)
    repository.update_trade(
        user,
        legacy_buy,
        traded_on="2026-07-31",
        traded_at="",
        side="BUY",
        quantity=100,
        price=10,
        fee=0,
        method="MANUAL",
        plan_setup_id="A1",
    )
    rebound_legacy_buy = repository.trade_execution(user, legacy_buy)
    assert rebound_legacy_buy["plan_version_id"] == plan["version_id"]
    assert rebound_legacy_buy["plan_setup_id"] == "A1"

    buy_id = repository.record_trade(
        user,
        traded_on="2026-08-01",
        symbol="000001.SZ",
        side="BUY",
        quantity=200,
        price=10,
        fee=0,
        method="MANUAL",
        setup_method="PRE_BREAKOUT_BASE",
    )
    repository.update_trade(
        user,
        buy_id,
        traded_on="2026-08-01",
        traded_at="",
        side="BUY",
        quantity=200,
        price=10,
        fee=0,
        method="MANUAL",
        setup_method="COMPLEX_PULLBACK",
    )
    assert repository.trade_execution(user, buy_id)["setup_code"] == "B2"

    second_buy = repository.record_trade(
        user,
        traded_on="2026-08-02",
        symbol="000001.SZ",
        side="BUY",
        quantity=100,
        price=10,
        fee=0,
        method="MANUAL",
    )
    repository.update_trade(
        user,
        second_buy,
        traded_on="2026-08-02",
        traded_at="",
        side="SELL",
        quantity=100,
        price=11,
        fee=0,
        method="MANUAL",
    )
    changed_sell = repository.trade_execution(user, second_buy)
    assert changed_sell["plan_version_id"] is None
    assert changed_sell["setup_code"] is None

    unmatched_sell = repository.record_trade(
        user,
        traded_on="2026-08-03",
        symbol="000002.SZ",
        side="SELL",
        quantity=100,
        price=10,
        fee=0,
        method="MANUAL",
        permit_unmatched_sell=True,
    )
    repository.update_trade(
        user,
        unmatched_sell,
        traded_on="2026-08-03",
        traded_at="",
        side="BUY",
        quantity=100,
        price=10,
        fee=0,
        method="MANUAL",
        setup_method="PULLBACK_SUPPORT",
    )
    changed_buy = repository.trade_execution(user, unmatched_sell)
    assert changed_buy["plan_version_id"] == plan["version_id"]
    assert changed_buy["setup_code"] == "B1"


def test_web_trade_edit_binds_legacy_buy_to_current_plan(tmp_path: Path) -> None:
    client = _client(tmp_path)
    repository = client.app.state.user_repository
    user = repository.session_user(client.cookies.get(SESSION_COOKIE))
    assert user is not None
    execution_id = repository.record_trade(
        user.user_id,
        traded_on="2026-07-31",
        symbol="000001.SZ",
        side="BUY",
        quantity=100,
        price=12.5,
        fee=0,
        method="MANUAL",
        setup_method="PRE_BREAKOUT_BASE",
    )
    plan = _published_demo(repository, user.user_id)

    response = client.post(
        f"/a/trades/{execution_id}",
        data={
            "csrf_token": _csrf(client),
            "traded_on": "2026-07-31",
            "traded_at": "",
            "side": "BUY",
            "quantity": "100",
            "price": "12.5",
            "fee": "0",
            "plan_setup_id": "A1",
            "stop_price": "12.0",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/a/stocks/000001.SZ#trade-journal"
    saved = repository.trade_execution(user.user_id, execution_id)
    assert saved is not None
    assert saved["plan_version_id"] == plan["version_id"]
    assert saved["plan_setup_id"] == "A1"


def test_web_trade_edit_can_rebind_old_plan_buy_to_setup_added_by_current_plan(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    repository = client.app.state.user_repository
    user = repository.session_user(client.cookies.get(SESSION_COOKIE))
    assert user is not None

    old_draft = repository.trading_plans.start(
        user.user_id, template="personal-a-share-v1"
    )
    while True:
        question = repository.trading_plans.next(user.user_id, old_draft["session_id"])
        if question["status"] == "COMPLETE":
            break
        status = repository.trading_plans.status(user.user_id, old_draft["session_id"])
        assert status is not None
        identifier = question["question"]["question_id"]
        proposed = status["answers"].get(identifier, {}).get("value")
        value = "A1,A3,B1,B2,C1" if identifier == "setup.enabled" else proposed
        repository.trading_plans.answer(
            user.user_id,
            old_draft["session_id"],
            {
                "question_id": identifier,
                "expected_revision": status["revision"],
                "decision": {"state": "CONFIRMED", "value": value},
            },
        )
    old_status = repository.trading_plans.status(user.user_id, old_draft["session_id"])
    assert old_status is not None
    old_plan = repository.trading_plans.publish(
        user.user_id, old_draft["session_id"], old_status["revision"]
    )
    execution_id = repository.record_trade(
        user.user_id,
        traded_on="2026-07-31",
        symbol="000001.SZ",
        side="BUY",
        quantity=100,
        price=12.5,
        fee=0,
        method="MANUAL",
        plan_setup_id="C1",
    )
    current_plan = _published_demo(repository, user.user_id)

    edit_page = client.get(f"/a/stocks/000001.SZ?edit_trade={execution_id}")
    assert edit_page.status_code == 200
    assert '<option value="C2"' in edit_page.text

    response = client.post(
        f"/a/trades/{execution_id}",
        data={
            "csrf_token": _csrf(client),
            "traded_on": "2026-07-31",
            "traded_at": "",
            "side": "BUY",
            "quantity": "100",
            "price": "12.5",
            "fee": "0",
            "plan_setup_id": "C2",
            "stop_price": "12.0",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    saved = repository.trade_execution(user.user_id, execution_id)
    assert saved is not None
    assert saved["plan_version_id"] != old_plan["version_id"]
    assert saved["plan_version_id"] == current_plan["version_id"]
    assert saved["plan_setup_id"] == "C2"
    assert saved["setup_code_snapshot"] == "C2"
