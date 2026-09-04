from __future__ import annotations

import pytest

from master_stock_selector.trade_domain import (
    aggregate_completed_trades,
    match_trade_executions,
    open_quantity,
    summarize_open_lots,
    trade_statistics,
)


def _execution(
    execution_id: str,
    traded_on: str,
    side: str,
    quantity: int,
    price: float,
    fee: float,
) -> dict[str, object]:
    return {
        "execution_id": execution_id,
        "traded_on": traded_on,
        "symbol": "000001.SZ",
        "side": side,
        "quantity": quantity,
        "price": price,
        "fee": fee,
        "method": "WEINSTEIN",
        "setup_method": "PULLBACK_SUPPORT",
        "stop_price": 8.0 if side == "BUY" else None,
    }


def test_fifo_accounting_allocates_costs_and_keeps_open_quantity() -> None:
    rows = [
        _execution("buy-1", "2026-07-01", "BUY", 100, 10.0, 5.0),
        _execution("sell-1", "2026-07-03", "SELL", 40, 12.0, 4.0),
    ]

    closed, open_lots, unmatched = match_trade_executions(rows)

    assert unmatched == []
    assert closed[0]["pnl"] == 74.0
    assert closed[0]["holding_days"] == 2
    assert summarize_open_lots(open_lots)[0] == {
        "symbol": "000001.SZ",
        "setup_method": "PULLBACK_SUPPORT",
        "plan_version_id": None,
        "evaluation_cycle_id": None,
        "setup_code": None,
        "setup_kind": "LEGACY",
        "plan_setup_id": None,
        "setup_code_snapshot": "",
        "setup_name_snapshot": "",
        "other_setup_note": "",
        "quantity": 60,
        "cost": 603.0,
        "cost_cents": 60300,
    }
    assert open_quantity(rows) == 60


def test_completed_trade_aggregation_and_statistics_use_full_buy_attempt() -> None:
    rows = [
        _execution("buy-1", "2026-07-01", "BUY", 100, 10.0, 0.0),
        _execution("sell-1", "2026-07-02", "SELL", 40, 11.0, 0.0),
        _execution("sell-2", "2026-07-03", "SELL", 60, 12.0, 0.0),
    ]

    closed, open_lots, _ = match_trade_executions(rows)
    completed = aggregate_completed_trades(closed, open_lots)

    assert len(completed) == 1
    assert completed[0]["quantity"] == 100
    assert completed[0]["pnl"] == 160.0
    assert completed[0]["exit_price"] == 11.6
    assert trade_statistics(completed)["net_pnl"] == 160.0


def test_unmatched_sell_requires_explicit_compatibility_mode() -> None:
    sell = _execution("sell-1", "2026-07-03", "SELL", 40, 12.0, 4.0)

    with pytest.raises(ValueError, match="无法由已有买入记录配对"):
        match_trade_executions([sell])

    _, _, unmatched = match_trade_executions([sell], permit_unmatched_sells=True)
    assert unmatched == [
        {
            "symbol": "000001.SZ",
            "sell_date": "2026-07-03",
            "quantity": 40,
            "price": 12.0,
            "fee": 4.0,
            "exit_reason": "",
        }
    ]
