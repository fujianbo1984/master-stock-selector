"""Shared long-only trade accounting rules independent of database storage."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

TRADE_METHOD_LABELS = {
    "WEINSTEIN": "温斯坦",
    "MINERVINI": "米涅尔维尼",
    "MANUAL": "人工判断",
}
TRADE_SETUP_LABELS = {
    "FAILED_TEST": "失败测试",
    "PULLBACK_SUPPORT": "简单回调",
    "LOWER_TIMEFRAME_BREAKOUT": "低周期突破入场",
    "COMPLEX_PULLBACK": "复杂回调",
    "ANTI": "Anti（趋势转换首次回调）",
    "PRE_BREAKOUT_BASE": "突破前基底入场",
    "POST_BREAKOUT_FIRST_PULLBACK": "突破后回调",
    "FAILED_BREAKOUT": "失败突破",
}
TRADE_SETUP_ALIASES = {
    "BREAKOUT": "POST_BREAKOUT_FIRST_PULLBACK",
    "PULLBACK": "PULLBACK_SUPPORT",
}
DEFAULT_TRADE_SETUP = "PULLBACK_SUPPORT"


def open_quantity(rows: Sequence[Mapping[str, Any]]) -> int:
    _, lots, _ = match_trade_executions(rows)
    return sum(int(lot["remaining_quantity"]) for lot in lots)


def match_trade_executions(
    rows: Sequence[Mapping[str, Any]],
    *,
    permit_unmatched_sells: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Match long-only executions FIFO; costs include allocated commissions."""

    lots: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    unmatched_sells: list[dict[str, Any]] = []
    for row in rows:
        if str(row["side"]) == "BUY":
            quantity = int(row["quantity"])
            total_cost_cents = _money_cents(
                Decimal(str(row["price"])) * quantity + Decimal(str(row["fee"]))
            )
            lots.append(
                {
                    "symbol": str(row["symbol"]),
                    "buy_execution_id": str(row["execution_id"]),
                    "buy_date": str(row["traded_on"]),
                    "method": str(row["method"]),
                    "setup_method": TRADE_SETUP_ALIASES.get(
                        str(row.get("setup_method") or DEFAULT_TRADE_SETUP),
                        str(row.get("setup_method") or DEFAULT_TRADE_SETUP),
                    ),
                    "entry_price": float(row["price"]),
                    "stop_price": row.get("stop_price"),
                    "initial_stop_price": row.get("initial_stop_price", row.get("stop_price")),
                    "remaining_quantity": quantity,
                    "unit_cost": total_cost_cents / 100 / quantity,
                    "remaining_cost_cents": total_cost_cents,
                    "rationale": str(row.get("rationale") or ""),
                    "invalidation": str(row.get("invalidation") or ""),
                    "market_context": str(row.get("market_context") or ""),
                    "plan_version_id": row.get("plan_version_id"),
                    "evaluation_cycle_id": row.get("evaluation_cycle_id"),
                    "setup_code": row.get("setup_code"),
                    "setup_kind": row.get("setup_kind", "LEGACY"),
                    "plan_setup_id": row.get("plan_setup_id"),
                    "setup_code_snapshot": row.get("setup_code_snapshot", ""),
                    "setup_name_snapshot": row.get("setup_name_snapshot", ""),
                    "other_setup_note": row.get("other_setup_note", ""),
                    "snapshot": dict(row.get("snapshot") or {}),
                }
            )
            continue

        remaining_to_sell = int(row["quantity"])
        remaining_gross_proceeds_cents = _money_cents(
            Decimal(str(row["price"])) * remaining_to_sell
        )
        remaining_sell_fee_cents = _money_cents(row["fee"])
        matching_lots = [lot for lot in lots if lot["symbol"] == str(row["symbol"])]
        for lot in matching_lots:
            if remaining_to_sell <= 0:
                break
            matched_quantity = min(remaining_to_sell, int(lot["remaining_quantity"]))
            gross_proceeds_cents = _allocate_cents(
                remaining_gross_proceeds_cents, matched_quantity, remaining_to_sell
            )
            sell_fee_cents = _allocate_cents(
                remaining_sell_fee_cents, matched_quantity, remaining_to_sell
            )
            proceeds_cents = gross_proceeds_cents - sell_fee_cents
            cost_cents = _allocate_cents(
                int(lot["remaining_cost_cents"]),
                matched_quantity,
                int(lot["remaining_quantity"]),
            )
            pnl_cents = proceeds_cents - cost_cents
            gross_pnl = (float(row["price"]) - float(lot["entry_price"])) * matched_quantity
            closed.append(
                {
                    "symbol": str(row["symbol"]),
                    "method": str(lot["method"]),
                    "setup_method": str(lot["setup_method"]),
                    "plan_version_id": lot.get("plan_version_id"),
                    "evaluation_cycle_id": lot.get("evaluation_cycle_id"),
                    "setup_code": lot.get("setup_code"),
                    "setup_kind": lot.get("setup_kind", "LEGACY"),
                    "plan_setup_id": lot.get("plan_setup_id"),
                    "setup_code_snapshot": lot.get("setup_code_snapshot", ""),
                    "setup_name_snapshot": lot.get("setup_name_snapshot", ""),
                    "other_setup_note": lot.get("other_setup_note", ""),
                    "snapshot": dict(lot.get("snapshot") or {}),
                    "buy_execution_id": str(lot["buy_execution_id"]),
                    "sell_execution_id": str(row["execution_id"]),
                    "buy_date": str(lot["buy_date"]),
                    "sell_date": str(row["traded_on"]),
                    "quantity": matched_quantity,
                    "cost": cost_cents / 100,
                    "cost_cents": cost_cents,
                    "proceeds": proceeds_cents / 100,
                    "proceeds_cents": proceeds_cents,
                    "pnl": pnl_cents / 100,
                    "pnl_cents": pnl_cents,
                    "gross_pnl": round(gross_pnl, 2),
                    "entry_price": float(lot["entry_price"]),
                    "exit_price": float(row["price"]),
                    "stop_price": lot.get("stop_price"),
                    "initial_stop_price": lot.get("initial_stop_price"),
                    "return_pct": round(pnl_cents / cost_cents * 100, 2)
                    if cost_cents
                    else None,
                    "holding_days": (
                        date.fromisoformat(str(row["traded_on"]))
                        - date.fromisoformat(str(lot["buy_date"]))
                    ).days,
                    "rationale": str(lot["rationale"]),
                    "invalidation": str(lot["invalidation"]),
                    "market_context": str(lot["market_context"]),
                    "exit_reason": str(row.get("exit_reason") or ""),
                }
            )
            lot["remaining_quantity"] = int(lot["remaining_quantity"]) - matched_quantity
            lot["remaining_cost_cents"] = int(lot["remaining_cost_cents"]) - cost_cents
            remaining_to_sell -= matched_quantity
            remaining_gross_proceeds_cents -= gross_proceeds_cents
            remaining_sell_fee_cents -= sell_fee_cents
        if remaining_to_sell:
            if not permit_unmatched_sells:
                raise ValueError("卖出记录无法由已有买入记录配对")
            unmatched_sells.append(
                {
                    "symbol": str(row["symbol"]),
                    "sell_date": str(row["traded_on"]),
                    "quantity": remaining_to_sell,
                    "price": float(row["price"]),
                    "fee": remaining_sell_fee_cents / 100,
                    "exit_reason": str(row.get("exit_reason") or ""),
                }
            )
    return closed, [lot for lot in lots if int(lot["remaining_quantity"]) > 0], unmatched_sells


def aggregate_completed_trades(
    closed: Sequence[Mapping[str, Any]],
    open_lots: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Collapse FIFO exit fragments into completed BUY trade attempts."""

    open_buy_ids = {str(lot["buy_execution_id"]) for lot in open_lots}
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for fragment in closed:
        grouped.setdefault(str(fragment["buy_execution_id"]), []).append(fragment)
    completed: list[dict[str, Any]] = []
    for buy_id, fragments in grouped.items():
        if buy_id in open_buy_ids:
            continue
        ordered = sorted(
            fragments, key=lambda item: (str(item["sell_date"]), str(item["sell_execution_id"]))
        )
        first = dict(ordered[0])
        quantity = sum(int(item["quantity"]) for item in ordered)
        pnl_cents = sum(
            int(item.get("pnl_cents", round(float(item["pnl"]) * 100))) for item in ordered
        )
        gross_pnl = sum(float(item.get("gross_pnl") or 0) for item in ordered)
        cost_cents = sum(
            int(item.get("cost_cents", round(float(item["cost"]) * 100))) for item in ordered
        )
        proceeds_cents = sum(
            int(item.get("proceeds_cents", round(float(item["proceeds"]) * 100)))
            for item in ordered
        )
        last = ordered[-1]
        stop_price = first.get("initial_stop_price")
        actual_risk = (
            (float(first["entry_price"]) - float(stop_price)) * quantity
            if stop_price is not None and float(first["entry_price"]) > float(stop_price)
            else None
        )
        first.update(
            {
                "sell_execution_id": ",".join(
                    str(item["sell_execution_id"]) for item in ordered
                ),
                "sell_date": str(last["sell_date"]),
                "quantity": quantity,
                "cost": cost_cents / 100,
                "cost_cents": cost_cents,
                "proceeds": proceeds_cents / 100,
                "proceeds_cents": proceeds_cents,
                "pnl": pnl_cents / 100,
                "pnl_cents": pnl_cents,
                "gross_pnl": round(gross_pnl, 2),
                "exit_price": round(
                    sum(float(item["exit_price"]) * int(item["quantity"]) for item in ordered)
                    / quantity,
                    6,
                ),
                "return_pct": round(pnl_cents / cost_cents * 100, 2)
                if cost_cents
                else None,
                "holding_days": int(last["holding_days"]),
                "exit_reason": "；".join(
                    dict.fromkeys(str(item.get("exit_reason") or "") for item in ordered)
                ).strip("；"),
                "actual_risk": round(actual_risk, 2) if actual_risk is not None else None,
                "r_multiple": round(gross_pnl / actual_risk, 4) if actual_risk else None,
            }
        )
        completed.append(first)
    return completed


def summarize_open_lots(lots: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for lot in lots:
        key = (
            str(lot["symbol"]),
            str(lot.get("setup_method") or DEFAULT_TRADE_SETUP),
            str(lot.get("plan_version_id") or ""),
            str(lot.get("setup_code") or ""),
            str(lot.get("plan_setup_id") or ""),
        )
        item = grouped.setdefault(
            key,
            {
                "symbol": key[0],
                "setup_method": key[1],
                "plan_version_id": lot.get("plan_version_id"),
                "evaluation_cycle_id": lot.get("evaluation_cycle_id"),
                "setup_code": lot.get("setup_code"),
                "setup_kind": lot.get("setup_kind", "LEGACY"),
                "plan_setup_id": lot.get("plan_setup_id"),
                "setup_code_snapshot": lot.get("setup_code_snapshot", ""),
                "setup_name_snapshot": lot.get("setup_name_snapshot", ""),
                "other_setup_note": lot.get("other_setup_note", ""),
                "quantity": 0,
                "cost": 0.0,
                "cost_cents": 0,
            },
        )
        quantity = int(lot["remaining_quantity"])
        item["quantity"] += quantity
        item["cost_cents"] += int(lot["remaining_cost_cents"])
    return [
        {**item, "cost": int(item["cost_cents"]) / 100}
        for item in sorted(
            grouped.values(),
            key=lambda item: (
                str(item["symbol"]),
                str(item["setup_method"]),
                str(item.get("plan_version_id") or ""),
            ),
        )
    ]


def trade_statistics(trades: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    count = len(trades)
    pnl_cents = [
        int(item.get("pnl_cents", round(float(item["pnl"]) * 100))) for item in trades
    ]
    pnl_values = [value / 100 for value in pnl_cents]
    gains = [value for value in pnl_values if value > 0]
    losses = [value for value in pnl_values if value < 0]
    total_cost_cents = sum(
        int(item.get("cost_cents", round(float(item["cost"]) * 100))) for item in trades
    )
    r_values = [float(item["r_multiple"]) for item in trades if item.get("r_multiple") is not None]
    return {
        "count": count,
        "net_pnl": sum(pnl_cents) / 100,
        "return_pct": round(sum(pnl_cents) / total_cost_cents * 100, 2)
        if total_cost_cents
        else None,
        "win_rate_pct": round(len(gains) / count * 100, 1) if count else None,
        "avg_gain": round(sum(gains) / len(gains), 2) if gains else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "profit_loss_ratio": round(
            (sum(gains) / len(gains)) / abs(sum(losses) / len(losses)), 2
        )
        if gains and losses
        else None,
        "expectancy": round(sum(pnl_values) / count, 2) if count else None,
        "gross_avg_r": round(sum(r_values) / len(r_values), 4) if r_values else None,
        "r_count": len(r_values),
        "avg_holding_days": round(sum(int(item["holding_days"]) for item in trades) / count, 1)
        if count
        else None,
    }


def _money_cents(value: Any) -> int:
    """Convert one monetary amount to integer fen using financial half-up rounding."""

    return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _allocate_cents(total_cents: int, quantity: int, remaining_quantity: int) -> int:
    """Allocate integer fen proportionally and leave any remainder for the final fragment."""

    if quantity == remaining_quantity:
        return total_cents
    return int(
        (Decimal(total_cents) * quantity / remaining_quantity).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )
