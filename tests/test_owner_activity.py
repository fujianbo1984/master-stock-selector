from __future__ import annotations

from typing import Any

from master_stock_selector.web.owner import build_owner_activity_feed, build_owner_trade_feed


def _execution(
    execution_id: str,
    *,
    traded_on: str,
    side: str,
    quantity: int,
    price: float,
    rationale: str = "",
    exit_reason: str = "",
    revision: int = 1,
    stop_price: float | None = None,
) -> dict[str, Any]:
    return {
        "execution_id": execution_id,
        "symbol": "000001.SZ",
        "traded_on": traded_on,
        "traded_at": "10:00:00",
        "side": side,
        "quantity": quantity,
        "price": price,
        "stop_price": stop_price,
        "fee": 3.0,
        "setup_label": "简单回调",
        "rationale": rationale,
        "exit_reason": exit_reason,
        "revision": revision,
        "created_at": f"{traded_on} 10:00:00",
        "updated_at": f"{traded_on} 10:05:00",
    }


def test_owner_feed_normalizes_positions_without_returning_private_quantities() -> None:
    feed = build_owner_trade_feed(
        [
            _execution(
                "buy-1",
                traded_on="2026-08-01",
                side="BUY",
                quantity=613,
                price=10.0,
                rationale="首次建仓",
                stop_price=9.0,
            ),
            _execution(
                "buy-2",
                traded_on="2026-08-02",
                side="BUY",
                quantity=387,
                price=12.0,
                rationale="确认后加仓",
            ),
            _execution(
                "sell-1",
                traded_on="2026-08-03",
                side="SELL",
                quantity=250,
                price=15.0,
                exit_reason="部分止盈",
                revision=2,
            ),
        ],
        {"000001.SZ": "平安银行"},
        closed_trades=[
            {
                "sell_execution_id": "sell-1",
                "entry_price": 10.0,
                "stop_price": 9.0,
                "planned_r_multiple": 5.0,
                "actual_r_multiple": 2.5,
            }
        ],
    )

    assert feed["positions"] == [
        {
            "symbol": "000001.SZ",
            "stock_name": "平安银行",
            "setup_label": "简单回调",
            "first_buy_on": "2026-08-01",
            "latest_trade_on": "2026-08-03",
            "latest_action": "卖出",
            "average_price": 11.032,
            "stop_price_label": "9（部分未设置）",
            "remaining_percent": 75,
            "remaining_label": "剩余 75%",
            "bar_percent": 75,
            "rationale": "确认后加仓",
        }
    ]
    assert [row["change_label"] for row in reversed(feed["executions"])] == [
        "建仓 61%",
        "加仓 39%",
        "减仓 25%",
    ]
    assert feed["executions"][0]["remaining_label"] == "剩余 75%"
    assert feed["executions"][0]["revised"] is True
    assert feed["executions"][0]["facts"] == [
        "买入价 10",
        "卖出价 15",
        "卖出仓位 25%",
        "止损设置 9",
        "计划盈亏比 5",
        "实际盈亏比 2.5",
    ]
    assert "quantity" not in repr(feed)
    assert "fee" not in repr(feed)


def test_owner_feed_resets_after_clear_and_keeps_addback_against_frozen_baseline() -> None:
    cleared = build_owner_trade_feed(
        [
            _execution(
                "buy-1", traded_on="2026-08-01", side="BUY", quantity=100, price=10
            ),
            _execution(
                "sell-1", traded_on="2026-08-02", side="SELL", quantity=100, price=11
            ),
            _execution(
                "buy-2", traded_on="2026-08-03", side="BUY", quantity=200, price=12
            ),
        ],
        {},
    )
    assert cleared["positions"][0]["remaining_percent"] == 100
    assert cleared["executions"][0]["change_label"] == "建仓 100%"
    assert any(row["change_label"] == "清仓" for row in cleared["executions"])

    addback = build_owner_trade_feed(
        [
            _execution(
                "buy-1", traded_on="2026-08-01", side="BUY", quantity=100, price=10
            ),
            _execution(
                "sell-1", traded_on="2026-08-02", side="SELL", quantity=50, price=11
            ),
            _execution(
                "buy-2", traded_on="2026-08-03", side="BUY", quantity=70, price=12
            ),
        ],
        {},
    )
    assert addback["executions"][0]["change_label"] == "回补 70%"
    assert addback["positions"][0]["remaining_label"] == "剩余 120%"


def test_owner_feed_marks_unmatched_sell_without_inventing_a_ratio() -> None:
    feed = build_owner_trade_feed(
        [
            _execution(
                "sell-only", traded_on="2026-08-01", side="SELL", quantity=10, price=10
            )
        ],
        {},
    )
    assert feed["positions"] == []
    assert feed["executions"][0]["change_label"] == "历史记录不完整"
    assert feed["executions"][0]["remaining_label"] == "无法计算"


def test_owner_activity_feed_merges_sanitized_records_newest_first() -> None:
    items = build_owner_activity_feed(
        executions=[
            {
                "symbol": "000001.SZ",
                "stock_name": "平安银行",
                "traded_on": "2026-08-14",
                "traded_at": "15:22:00",
                "side": "SELL",
                "side_label": "卖出",
                "change_label": "减仓 30%",
                "remaining_label": "剩余 70%",
                "reason": "按计划减仓",
                "facts": ["买入价 10", "卖出价 12"],
            }
        ],
        positions=[
            {
                "symbol": "000001.SZ",
                "stock_name": "平安银行",
                "latest_trade_on": "2026-08-14",
                "remaining_label": "剩余 70%",
                "rationale": "等待周线确认",
            }
        ],
        focus_items=[
            {
                "symbol": "000002.SZ",
                "name": "万科A",
                "reviewed_at": "2026-08-15 09:30:00",
                "note": "观察平台突破",
                "is_held": False,
            }
        ],
        reading_notes=[
            {
                "title": "建立交易边界",
                "updated_on": "2026-08-13",
                "summary": "把知识收敛成研究边界。",
                "href": "/a/reading/boundaries",
            }
        ],
    )

    assert [item["kind"] for item in items] == [
        "focus",
        "trade",
        "position",
        "article",
    ]
    assert items[0]["day"] == "2026-08-15"
    assert items[0]["clock"] == "09:30:00"
    assert items[1]["summary"] == "减仓 30% · 剩余 70%"
    assert items[1]["facts"] == ["买入价 10", "卖出价 12"]
    assert "quantity" not in repr(items)
    assert "fee" not in repr(items)
