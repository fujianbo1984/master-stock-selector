from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

DEFAULT_SITE_OWNER_USERNAME = "我不是来玩的"

OWNER_READING_NOTES = (
    {
        "title": "少学一种形态，多建立一道边界",
        "summary": (
            "从交易大师的不同方法中重新理解交易体系，"
            "把知识收敛成可执行、可验证的研究边界。"
        ),
        "href": "/a/reading/trading-system-boundaries",
        "updated_on": "2026-08-09",
        "tags": ("交易体系", "趋势交易", "风险管理"),
    },
    {
        "title": "八张示意图读懂 Adam Grimes 的交易模板",
        "summary": (
            "通过八种市场结构理解失败测试、回调、Anti 与突破交易的"
            "设置、触发和风险边界。"
        ),
        "href": "/a/reading/adam-grimes-trading-templates",
        "updated_on": "2026-08-13",
        "tags": ("Adam Grimes", "交易模板", "市场结构"),
    },
)


def build_owner_trade_feed(
    executions: Sequence[Mapping[str, Any]],
    names: Mapping[str, str],
) -> dict[str, Any]:
    """Return a quantity-free owner feed derived from private executions."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for execution in executions:
        symbol = str(execution.get("symbol") or "").upper()
        if symbol:
            grouped[symbol].append(dict(execution))

    public_executions: list[dict[str, Any]] = []
    positions: list[dict[str, Any]] = []
    latest_updated_at = ""
    for symbol, rows in grouped.items():
        rows.sort(key=_execution_sort_key)
        cycles = _split_trade_cycles(rows)
        for cycle_index, cycle in enumerate(cycles, start=1):
            result = _public_cycle(
                cycle,
                symbol=symbol,
                stock_name=names.get(symbol, "名称待补"),
                cycle_index=cycle_index,
            )
            public_executions.extend(result["executions"])
            if result["position"] is not None:
                positions.append(result["position"])
            latest_updated_at = max(latest_updated_at, str(result["latest_updated_at"] or ""))

    public_executions.sort(key=_public_execution_sort_key, reverse=True)
    positions.sort(key=lambda item: (str(item["latest_trade_on"]), str(item["symbol"])), reverse=True)
    return {
        "positions": positions,
        "executions": public_executions,
        "latest_updated_at": latest_updated_at,
    }


def _split_trade_cycles(rows: Sequence[Mapping[str, Any]]) -> list[list[dict[str, Any]]]:
    cycles: list[list[dict[str, Any]]] = []
    current_cycle: list[dict[str, Any]] = []
    open_quantity = 0
    for source in rows:
        row = dict(source)
        side = str(row.get("side") or "").upper()
        quantity = max(0, int(row.get("quantity") or 0))
        if side == "BUY":
            if open_quantity == 0 and current_cycle:
                cycles.append(current_cycle)
                current_cycle = []
            current_cycle.append(row)
            open_quantity += quantity
            continue

        if not current_cycle:
            cycles.append([row])
            open_quantity = 0
            continue
        current_cycle.append(row)
        open_quantity -= quantity
        if open_quantity <= 0:
            cycles.append(current_cycle)
            current_cycle = []
            open_quantity = 0
    if current_cycle:
        cycles.append(current_cycle)
    return cycles


def _public_cycle(
    rows: Sequence[Mapping[str, Any]],
    *,
    symbol: str,
    stock_name: str,
    cycle_index: int,
) -> dict[str, Any]:
    baseline = _cycle_baseline(rows)
    running_quantity = 0
    lots: list[dict[str, Any]] = []
    public_rows: list[dict[str, Any]] = []
    first_buy_on = ""
    latest_updated_at = ""
    latest_buy_reason = ""
    latest_setup_label = ""
    invalid_cycle = baseline <= 0

    for row in rows:
        side = str(row.get("side") or "").upper()
        quantity = max(0, int(row.get("quantity") or 0))
        before = running_quantity
        unmatched = side == "SELL" and quantity > before
        if side == "BUY":
            running_quantity += quantity
            lots.append({"quantity": quantity, "price": float(row.get("price") or 0)})
            first_buy_on = first_buy_on or str(row.get("traded_on") or "")
            latest_buy_reason = str(row.get("rationale") or latest_buy_reason)
            latest_setup_label = str(row.get("setup_label") or latest_setup_label)
        else:
            running_quantity = max(0, running_quantity - quantity)
            _consume_fifo_lots(lots, quantity)

        change_percent = _percent(quantity, baseline) if not unmatched else None
        remaining_percent = _percent(running_quantity, baseline) if not unmatched else None
        public_rows.append(
            {
                "execution_id": str(row.get("execution_id") or ""),
                "symbol": symbol,
                "stock_name": stock_name,
                "traded_on": str(row.get("traded_on") or ""),
                "traded_at": str(row.get("traded_at") or ""),
                "side": side,
                "side_label": "买入" if side == "BUY" else "卖出",
                "price": float(row.get("price") or 0),
                "setup_label": str(row.get("setup_label") or "—"),
                "change_percent": change_percent,
                "remaining_percent": remaining_percent,
                "change_label": _change_label(
                    side=side,
                    before=before,
                    after=running_quantity,
                    percent=change_percent,
                    unmatched=unmatched or invalid_cycle,
                    sold_before=any(item["side"] == "SELL" for item in public_rows),
                ),
                "remaining_label": (
                    _remaining_label(remaining_percent)
                    if not unmatched and not invalid_cycle
                    else "无法计算"
                ),
                "reason": str(
                    (row.get("rationale") if side == "BUY" else row.get("exit_reason"))
                    or ""
                ),
                "revised": int(row.get("revision") or 1) > 1,
                "updated_at": str(row.get("updated_at") or ""),
                "cycle_index": cycle_index,
                "incomplete": unmatched or invalid_cycle,
            }
        )
        latest_updated_at = max(latest_updated_at, str(row.get("updated_at") or ""))

    position: dict[str, Any] | None = None
    if running_quantity > 0 and baseline > 0:
        average_price = _open_average_price(lots)
        remaining_percent = _percent(running_quantity, baseline)
        last_row = rows[-1]
        position = {
            "symbol": symbol,
            "stock_name": stock_name,
            "setup_label": latest_setup_label or "—",
            "first_buy_on": first_buy_on,
            "latest_trade_on": str(last_row.get("traded_on") or ""),
            "latest_action": "买入" if str(last_row.get("side")) == "BUY" else "卖出",
            "average_price": average_price,
            "remaining_percent": remaining_percent,
            "remaining_label": _remaining_label(remaining_percent),
            "bar_percent": min(100, remaining_percent or 0),
            "rationale": latest_buy_reason,
        }

    return {
        "executions": public_rows,
        "position": position,
        "latest_updated_at": latest_updated_at,
    }


def _cycle_baseline(rows: Sequence[Mapping[str, Any]]) -> int:
    running_quantity = 0
    peak_quantity = 0
    for row in rows:
        side = str(row.get("side") or "").upper()
        quantity = max(0, int(row.get("quantity") or 0))
        if side == "BUY":
            running_quantity += quantity
            peak_quantity = max(peak_quantity, running_quantity)
            continue
        if running_quantity > 0:
            return running_quantity
        return 0
    return peak_quantity


def _consume_fifo_lots(lots: list[dict[str, Any]], quantity: int) -> None:
    remaining = quantity
    for lot in lots:
        if remaining <= 0:
            break
        matched = min(remaining, int(lot["quantity"]))
        lot["quantity"] = int(lot["quantity"]) - matched
        remaining -= matched


def _open_average_price(lots: Sequence[Mapping[str, Any]]) -> float | None:
    quantity = sum(int(lot["quantity"]) for lot in lots)
    if quantity <= 0:
        return None
    cost = sum(int(lot["quantity"]) * float(lot["price"]) for lot in lots)
    return round(cost / quantity, 3)


def _percent(quantity: int, baseline: int) -> int | None:
    if baseline <= 0:
        return None
    value = quantity * 100 / baseline
    return max(1, int(value + 0.5)) if quantity > 0 else 0


def _change_label(
    *,
    side: str,
    before: int,
    after: int,
    percent: int | None,
    unmatched: bool,
    sold_before: bool,
) -> str:
    if unmatched or percent is None:
        return "历史记录不完整"
    if side == "SELL":
        return "清仓" if after == 0 else f"减仓 {percent}%"
    if before == 0:
        return f"建仓 {percent}%"
    return f"{'回补' if sold_before else '加仓'} {percent}%"


def _remaining_label(percent: int | None) -> str:
    if percent is None:
        return "无法计算"
    return "已清仓" if percent == 0 else f"剩余 {percent}%"


def _execution_sort_key(row: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row.get("traded_on") or ""),
        str(row.get("traded_at") or ""),
        str(row.get("created_at") or ""),
        str(row.get("execution_id") or ""),
    )


def _public_execution_sort_key(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(row.get("traded_on") or ""),
        str(row.get("traded_at") or ""),
        str(row.get("execution_id") or ""),
    )
