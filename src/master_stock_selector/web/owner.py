from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

DEFAULT_SITE_OWNER_USERNAME = "我不是来玩的"

OWNER_READING_NOTES = (
    {
        "title": "Adam Grimes 如何交易回调",
        "summary": (
            "系统梳理回调交易方法，理解推动、简单与复杂回调、"
            "结构止损、1R管理及形态失效。"
        ),
        "href": "/a/reading/adam-grimes-pullback-trading",
        "updated_on": "2026-08-19",
        "tags": ("Adam Grimes", "回调交易", "趋势交易"),
    },
    {
        "title": "均线不是买卖按钮",
        "summary": (
            "对比五位交易大师的均线思想，理解趋势阶段、候选过滤、"
            "价格结构与入场触发之间的区别。"
        ),
        "href": "/a/reading/moving-averages-are-not-trading-buttons",
        "updated_on": "2026-08-18",
        "tags": ("均线", "趋势交易", "价格行为"),
    },
    {
        "title": "止损不是一个百分比",
        "summary": (
            "对比五位交易大师的止损思想，理解结构失效、波动噪声、"
            "风险上限和仓位之间的关系。"
        ),
        "href": "/a/reading/stop-loss-is-not-a-percentage",
        "updated_on": "2026-08-17",
        "tags": ("止损", "风险管理", "仓位管理"),
    },
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
    closed_trades: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Return a quantity-free owner feed derived from private executions."""
    closed_by_sell: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for trade in closed_trades:
        sell_execution_id = str(trade.get("sell_execution_id") or "")
        if sell_execution_id:
            closed_by_sell[sell_execution_id].append(trade)

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
                closed_by_sell=closed_by_sell,
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


def build_owner_activity_feed(
    *,
    executions: Sequence[Mapping[str, Any]],
    positions: Sequence[Mapping[str, Any]],
    focus_items: Sequence[Mapping[str, Any]],
    reading_notes: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Merge sanitized owner records into a newest-first activity index."""
    items: list[dict[str, Any]] = []

    for execution in executions:
        day, clock = _activity_date_time(
            str(execution.get("traded_on") or ""),
            str(execution.get("traded_at") or ""),
        )
        symbol = str(execution.get("symbol") or "")
        side = str(execution.get("side") or "").upper()
        items.append(
            {
                "kind": "trade",
                "kind_label": str(execution.get("side_label") or "成交"),
                "side": side.lower(),
                "day": day,
                "clock": clock,
                "sort_at": f"{day} {clock}",
                "subject": str(execution.get("stock_name") or "名称待补"),
                "symbol": symbol,
                "href": f"/a/stocks/{symbol}",
                "summary": " · ".join(
                    part
                    for part in (
                        str(execution.get("change_label") or ""),
                        str(execution.get("remaining_label") or ""),
                    )
                    if part
                ),
                "facts": list(execution.get("facts") or ()),
                "detail": str(execution.get("reason") or "站长未补充本次记录。"),
                "action_label": "查看详情",
            }
        )

    for position in positions:
        day, clock = _activity_date_time(str(position.get("latest_trade_on") or ""), "")
        symbol = str(position.get("symbol") or "")
        detail = str(position.get("rationale") or "")
        if not detail:
            detail = f"当前交易方案：{position.get('setup_label') or '—'}"
        items.append(
            {
                "kind": "position",
                "kind_label": "持仓更新",
                "side": "",
                "day": day,
                "clock": clock,
                "sort_at": f"{day} {clock}",
                "subject": str(position.get("stock_name") or "名称待补"),
                "symbol": symbol,
                "href": f"/a/stocks/{symbol}",
                "summary": str(position.get("remaining_label") or "无法计算"),
                "facts": [
                    {
                        "label": "持仓均价",
                        "value": _price_label(position.get("average_price")),
                        "tone": "price",
                    },
                    {
                        "label": "止损设置",
                        "value": str(position.get("stop_price_label") or "—"),
                        "tone": "risk",
                    },
                ],
                "detail": detail,
                "action_label": "查看持仓",
            }
        )

    for focus in focus_items:
        day, clock = _activity_date_time(str(focus.get("reviewed_at") or ""), "")
        symbol = str(focus.get("symbol") or "")
        items.append(
            {
                "kind": "focus",
                "kind_label": "重点观察",
                "side": "",
                "day": day,
                "clock": clock,
                "sort_at": f"{day} {clock}",
                "subject": str(focus.get("name") or "名称待补"),
                "symbol": symbol,
                "href": f"/a/stocks/{symbol}",
                "summary": "已持仓" if focus.get("is_held") else "未持仓",
                "facts": [],
                "detail": str(focus.get("note") or "站长尚未填写观察备注。"),
                "action_label": "查看观察",
            }
        )

    for article in reading_notes:
        day, clock = _activity_date_time(str(article.get("updated_on") or ""), "")
        items.append(
            {
                "kind": "article",
                "kind_label": "阅读心得",
                "side": "",
                "day": day,
                "clock": clock,
                "sort_at": f"{day} {clock}",
                "subject": str(article.get("title") or "未命名文章"),
                "symbol": "",
                "href": str(article.get("href") or "#"),
                "summary": "—",
                "facts": [],
                "detail": str(article.get("summary") or ""),
                "action_label": "查看文章",
            }
        )

    items.sort(
        key=lambda item: (
            str(item["sort_at"]),
            {"focus": 3, "trade": 2, "position": 1, "article": 0}.get(
                str(item["kind"]), 0
            ),
            str(item["subject"]),
        ),
        reverse=True,
    )
    return items


def _activity_date_time(day_or_timestamp: str, clock: str) -> tuple[str, str]:
    value = day_or_timestamp.strip()
    day = value[:10]
    time_value = clock.strip() or (value[11:19] if len(value) >= 16 else "")
    return day, time_value


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
    closed_by_sell: Mapping[str, Sequence[Mapping[str, Any]]],
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
            lots.append(
                {
                    "quantity": quantity,
                    "price": float(row.get("price") or 0),
                    "stop_price": row.get("stop_price"),
                }
            )
            first_buy_on = first_buy_on or str(row.get("traded_on") or "")
            latest_buy_reason = str(row.get("rationale") or latest_buy_reason)
            latest_setup_label = str(row.get("setup_label") or latest_setup_label)
        else:
            running_quantity = max(0, running_quantity - quantity)
            _consume_fifo_lots(lots, quantity)

        change_percent = _percent(quantity, baseline) if not unmatched else None
        remaining_percent = _percent(running_quantity, baseline) if not unmatched else None
        execution_id = str(row.get("execution_id") or "")
        matched_trades = list(closed_by_sell.get(execution_id, ())) if side == "SELL" else []
        public_rows.append(
            {
                "execution_id": execution_id,
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
                "facts": _execution_facts(row, matched_trades, change_percent),
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
            "stop_price_label": _stop_range_label(
                lot.get("stop_price") for lot in lots if int(lot["quantity"]) > 0
            ),
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


def _execution_facts(
    row: Mapping[str, Any],
    matched_trades: Sequence[Mapping[str, Any]],
    change_percent: int | None,
) -> list[dict[str, str]]:
    side = str(row.get("side") or "").upper()
    price = _price_label(row.get("price"))
    if side == "BUY":
        return [
            {"label": "买入价", "value": price, "tone": "buy"},
            {
                "label": "止损设置",
                "value": _price_label(row.get("stop_price")),
                "tone": "risk",
            },
        ]

    return [
        {
            "label": "买入价",
            "value": _number_range_label(
                item.get("entry_price") for item in matched_trades
            ),
            "tone": "buy",
        },
        {"label": "卖出价", "value": price, "tone": "sell"},
        {
            "label": "卖出仓位",
            "value": f"{change_percent}%" if change_percent is not None else "—",
            "tone": "position",
        },
        {
            "label": "止损设置",
            "value": _stop_range_label(
                item.get("stop_price") for item in matched_trades
            ),
            "tone": "risk",
        },
        {
            "label": "计划盈亏比",
            "value": _number_range_label(
                item.get("planned_r_multiple") for item in matched_trades
            ),
            "tone": "ratio",
        },
        {
            "label": "实际盈亏比",
            "value": _number_range_label(
                item.get("actual_r_multiple") for item in matched_trades
            ),
            "tone": "ratio",
        },
    ]


def _price_label(value: Any) -> str:
    if value is None or value == "":
        return "—"
    return f"{float(value):.3f}".rstrip("0").rstrip(".")


def _number_range_label(values: Any) -> str:
    normalized = sorted(
        {round(float(value), 3) for value in values if value is not None and value != ""}
    )
    if not normalized:
        return "—"
    if len(normalized) == 1:
        return _price_label(normalized[0])
    return f"{_price_label(normalized[0])}–{_price_label(normalized[-1])}"


def _stop_range_label(values: Any) -> str:
    collected = list(values)
    label = _number_range_label(collected)
    if label != "—" and any(value is None or value == "" for value in collected):
        return f"{label}（部分未设置）"
    return label


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
