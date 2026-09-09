from __future__ import annotations

import hmac
import json
import math
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any
from uuid import uuid4

from ..trade_domain import (
    DEFAULT_TRADE_SETUP,
    aggregate_completed_trades,
    match_trade_executions,
    open_quantity,
    summarize_open_lots,
    trade_statistics,
)
from .user_store import UserStore
from .users import (
    C1_ENTRY_VARIANT_LABELS,
    OTHER_SETUP_ID,
    ApiTokenPrincipal,
    TradeBatchValidationError,
    TradeRevisionConflict,
    _decorate_setup,
    _normalize_api_trade,
    _normalize_drawing,
    _setup_assignment,
    _strict_positive_integer,
    _token_hash,
    _trade_fingerprint,
    _trade_identity,
    _validated_trade_values,
)


class UserTradeStore(UserStore):
    def record_trade(
        self,
        user_id: str,
        *,
        traded_on: str,
        traded_at: str = "",
        symbol: str,
        side: str,
        quantity: int,
        price: float,
        fee: float,
        method: str,
        setup_method: str = "",
        stop_price: float | None = None,
        rationale: str = "",
        invalidation: str = "",
        exit_reason: str = "",
        market_context: str = "",
        observation_snapshot: Mapping[str, Any] | None = None,
        permit_unmatched_sell: bool = False,
        plan_version_id: str | None = None,
        setup_code: str | None = None,
        plan_setup_id: str = OTHER_SETUP_ID,
        other_setup_note: str = "",
    ) -> str:
        compatibility_setup = setup_method or DEFAULT_TRADE_SETUP
        values = _validated_trade_values(
            traded_on=traded_on,
            traded_at=traded_at,
            side=side,
            quantity=quantity,
            price=price,
            fee=fee,
            method=method,
            setup_method=compatibility_setup,
            stop_price=stop_price,
        )
        normalized_symbol = symbol.upper().strip()
        if not normalized_symbol or "." not in normalized_symbol:
            raise ValueError("股票代码格式不正确")
        execution_id = uuid4().hex
        with self.connect() as connection:
            plan = (
                self.trading_plans.show(user_id, plan_version_id)
                if plan_version_id
                else self.trading_plans.current(user_id)
            )
            if values["side"] == "SELL":
                plan = None
            if plan_version_id and plan is None:
                raise ValueError("计划版本不存在或不属于当前用户")
            assignment = _setup_assignment(
                plan,
                plan_setup_id=plan_setup_id,
                setup_code=setup_code,
                setup_method=setup_method,
                other_setup_note=other_setup_note,
            )
            values["setup_method"] = assignment["setup_method"]
            frozen_snapshot = dict(observation_snapshot or {})
            if values["side"] == "BUY" and assignment["setup_kind"] == "PLAN":
                observation_row = connection.execute(
                    """SELECT setup_code_snapshot, setup_name_snapshot, status,
                              evidence_json, updated_at
                    FROM user_setup_observation
                    WHERE user_id=? AND symbol=? AND setup_id=?""",
                    (user_id, normalized_symbol, assignment["plan_setup_id"]),
                ).fetchone()
                if observation_row is not None:
                    try:
                        setup_evidence = json.loads(str(observation_row["evidence_json"] or "{}"))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        setup_evidence = {}
                    frozen_snapshot["setup_observation"] = {
                        "setup_id": assignment["plan_setup_id"],
                        "setup_code": str(observation_row["setup_code_snapshot"]),
                        "setup_name": str(observation_row["setup_name_snapshot"]),
                        "status": str(observation_row["status"]),
                        "evidence": setup_evidence if isinstance(setup_evidence, dict) else {},
                        "updated_at": str(observation_row["updated_at"]),
                    }
            if values["side"] == "BUY" and assignment["plan_setup_id"] == "C1":
                review_row = connection.execute(
                    """SELECT c1_record_json FROM user_stock_review
                    WHERE user_id=? AND symbol=?""",
                    (user_id, normalized_symbol),
                ).fetchone()
                if review_row is not None:
                    try:
                        c1_record = json.loads(str(review_row["c1_record_json"] or "{}"))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        c1_record = {}
                    if isinstance(c1_record, dict) and c1_record:
                        frozen_snapshot["c1_record"] = c1_record
            existing_rows = self._trade_rows(connection, user_id, normalized_symbol)
            candidate = {
                "execution_id": execution_id,
                "traded_on": traded_on,
                "traded_at": values["traded_at"],
                "symbol": normalized_symbol,
                "side": values["side"],
                "quantity": quantity,
                "price": price,
                "fee": fee,
                "method": values["method"],
                "setup_method": values["setup_method"],
                "stop_price": values["stop_price"],
            }
            if values["side"] == "SELL":
                try:
                    match_trade_executions([*existing_rows, candidate])
                except ValueError as exc:
                    if not permit_unmatched_sell:
                        available = open_quantity(existing_rows)
                        raise ValueError(f"卖出数量超过可复盘持仓：可用 {available} 股") from exc
            connection.execute(
                """
                INSERT INTO user_trade_execution(
                    execution_id, user_id, traded_on, traded_at, symbol, side,
                    quantity, price, fee, method, setup_method, stop_price,
                    initial_stop_price,
                    rationale, invalidation, exit_reason, market_context,
                    observation_snapshot_json, plan_version_id, evaluation_cycle_id, setup_code
                    , setup_kind, plan_setup_id, setup_code_snapshot,
                    setup_name_snapshot, other_setup_note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    execution_id,
                    user_id,
                    traded_on,
                    values["traded_at"],
                    normalized_symbol,
                    values["side"],
                    quantity,
                    price,
                    fee,
                    values["method"],
                    values["setup_method"],
                    values["stop_price"],
                    values["stop_price"] if values["side"] == "BUY" else None,
                    rationale.strip(),
                    invalidation.strip(),
                    exit_reason.strip(),
                    market_context.strip(),
                    json.dumps(frozen_snapshot, ensure_ascii=False, sort_keys=True),
                    plan["version_id"] if plan else None,
                    assignment["evaluation_cycle_id"] if values["side"] == "BUY" else None,
                    assignment["setup_code"] if values["side"] == "BUY" else None,
                    assignment["setup_kind"] if values["side"] == "BUY" else "OTHER",
                    assignment["plan_setup_id"] if values["side"] == "BUY" else None,
                    assignment["setup_code_snapshot"] if values["side"] == "BUY" else "",
                    assignment["setup_name_snapshot"] if values["side"] == "BUY" else "",
                    assignment["other_setup_note"] if values["side"] == "BUY" else "",
                ),
            )
        return execution_id

    def trade_review(self, user_id: str, names: Mapping[str, str]) -> dict[str, Any]:
        with self.connect() as connection:
            rows = self._trade_rows(connection, user_id)
            version_rows = connection.execute(
                """SELECT version.version_id, version.version_number
                FROM user_trading_plan_version AS version
                JOIN user_trading_plan AS plan ON plan.plan_id=version.plan_id
                WHERE plan.user_id=?""",
                (user_id,),
            ).fetchall()
        version_labels = {
            str(row["version_id"]): str(row["version_number"]) for row in version_rows
        }
        for row in rows:
            row["snapshot"] = json.loads(str(row.pop("observation_snapshot_json") or "{}"))
        closed, open_lots, unmatched_sells = match_trade_executions(
            rows, permit_unmatched_sells=True
        )
        completed_lots = aggregate_completed_trades(closed, open_lots)
        open_positions = summarize_open_lots(open_lots)
        for collection in (rows, completed_lots, open_positions, unmatched_sells):
            for item in collection:
                symbol = str(item["symbol"])
                item["stock_name"] = names.get(symbol, "名称待补")
                _decorate_setup(item)
        by_plan_setup: dict[str, dict[str, Any]] = {}
        for item in completed_lots:
            if item.get("setup_kind") != "PLAN" or not item.get("plan_setup_id"):
                continue
            cycle_id = str(item.get("evaluation_cycle_id") or "UNBOUND")
            setup_id = str(item["plan_setup_id"])
            setup_code = item.get("setup_code_snapshot") or item.get("setup_code") or setup_id
            key = f"{cycle_id}:{setup_id}"
            by_plan_setup.setdefault(
                key,
                {
                    "evaluation_cycle_id": item.get("evaluation_cycle_id"),
                    "plan_version_ids": set(),
                    "setup_code": setup_code,
                    "setup_name": item.get("setup_name_snapshot") or setup_code,
                    "trades": [],
                },
            )
            group = by_plan_setup[key]
            if item.get("plan_version_id"):
                group["plan_version_ids"].add(str(item["plan_version_id"]))
            group["trades"].append(item)
        for value in by_plan_setup.values():
            version_ids = sorted(value.pop("plan_version_ids"))
            value["plan_version_ids"] = version_ids
            value["plan_versions"] = [version_labels.get(item, item) for item in version_ids]
            trades = value.pop("trades")
            value["summary"] = trade_statistics(trades)
            value["by_entry_variant"] = {}
            if str(value.get("setup_code") or "") == "C1":
                for variant, label in C1_ENTRY_VARIANT_LABELS.items():
                    matching = [
                        item
                        for item in trades
                        if str(
                            (item.get("snapshot", {}).get("c1_record") or {}).get("entry_variant")
                            or ""
                        )
                        == variant
                    ]
                    if matching:
                        value["by_entry_variant"][variant] = {
                            "label": label,
                            "summary": trade_statistics(matching),
                        }
            value["sample_state"] = (
                "样本不足：少于 30 个可计算R的完整买入批次。"
                if value["summary"].get("r_count", 0) < 30
                else "样本达到本计划的 30 个可计算R完整买入批次初步评价门槛。"
            )
        completed_lot_summary = trade_statistics(completed_lots)
        realized_summary = {
            "net_pnl": sum(
                int(item.get("pnl_cents", round(float(item["pnl"]) * 100))) for item in closed
            )
            / 100,
            "matched_quantity": sum(int(item["quantity"]) for item in closed),
            "fifo_fragment_count": len(closed),
            "sell_execution_count": len({str(item["sell_execution_id"]) for item in closed}),
        }
        return {
            "executions": list(reversed(rows)),
            "completed_lots": list(reversed(completed_lots)),
            "closed_fragments": list(reversed(closed)),
            "open_positions": open_positions,
            "unmatched_sells": list(reversed(unmatched_sells)),
            "realized_summary": realized_summary,
            "completed_lot_summary": completed_lot_summary,
            "completed_lots_by_setup": {
                label: trade_statistics(
                    [item for item in completed_lots if item["setup_label"] == label]
                )
                for label in dict.fromkeys(item["setup_label"] for item in completed_lots)
            },
            "other_count": sum(1 for item in completed_lots if item.get("setup_kind") == "OTHER"),
            "legacy_count": sum(1 for item in completed_lots if item.get("setup_kind") == "LEGACY"),
            "by_plan_setup": by_plan_setup,
            "completed_lot_sample_state": (
                "样本不足：少于 20 个完整买入批次，仅展示描述统计。"
                if len(completed_lots) < 20
                else "完整买入批次达到基础复盘门槛；仍不构成策略有效性或买卖建议。"
            ),
        }

    def trade_symbols(self, user_id: str) -> set[str]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT symbol FROM user_trade_execution WHERE user_id=?",
                (user_id,),
            ).fetchall()
        return {str(row["symbol"]) for row in rows}

    def trade_execution(self, user_id: str, execution_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM user_trade_execution
                WHERE user_id=? AND execution_id=?
                """,
                (user_id, execution_id),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["snapshot"] = json.loads(str(item.pop("observation_snapshot_json") or "{}"))
        _decorate_setup(item)
        return item

    def update_trade(
        self,
        user_id: str,
        execution_id: str,
        *,
        traded_on: str,
        traded_at: str,
        side: str,
        quantity: int,
        price: float,
        fee: float,
        method: str,
        setup_method: str = "",
        plan_setup_id: str = OTHER_SETUP_ID,
        other_setup_note: str = "",
        stop_price: float | None = None,
        rationale: str = "",
        invalidation: str = "",
        exit_reason: str = "",
        market_context: str = "",
        observation_snapshot: Mapping[str, Any] | None = None,
    ) -> None:
        values = _validated_trade_values(
            traded_on=traded_on,
            traded_at=traded_at,
            side=side,
            quantity=quantity,
            price=price,
            fee=fee,
            method=method,
            setup_method=setup_method or DEFAULT_TRADE_SETUP,
            stop_price=stop_price,
        )
        with self.connect() as connection:
            current = connection.execute(
                """
                SELECT * FROM user_trade_execution
                WHERE user_id=? AND execution_id=?
                """,
                (user_id, execution_id),
            ).fetchone()
            if current is None:
                raise ValueError("成交记录不存在")
            if values["side"] == "SELL":
                plan_version_id = None
                evaluation_cycle_id = None
                assignment = _setup_assignment(None)
            elif str(current["side"]) == "BUY":
                plan_version_id = current["plan_version_id"]
                evaluation_cycle_id = current["evaluation_cycle_id"]
                plan = (
                    self.trading_plans.show(user_id, str(plan_version_id))
                    if plan_version_id
                    else None
                )
                if plan_setup_id == "LEGACY" and str(current["setup_kind"]) == "LEGACY":
                    assignment = {
                        "setup_kind": "LEGACY",
                        "plan_setup_id": None,
                        "setup_code": current["setup_code"],
                        "setup_code_snapshot": current["setup_code_snapshot"],
                        "setup_name_snapshot": current["setup_name_snapshot"],
                        "other_setup_note": "",
                        "setup_method": current["setup_method"],
                        "evaluation_cycle_id": current["evaluation_cycle_id"],
                    }
                else:
                    requested_setup = str(plan_setup_id or "").strip().upper()
                    if (plan is None and requested_setup not in {"", OTHER_SETUP_ID}) or (
                        plan is None and setup_method
                    ):
                        plan = self.trading_plans.current(user_id)
                        plan_version_id = plan["version_id"] if plan else None
                    elif requested_setup not in {"", OTHER_SETUP_ID} and plan is not None:
                        historical_match = next(
                            (
                                item
                                for item in plan.get("setup_catalog", [])
                                if requested_setup
                                in {
                                    str(item.get("setup_id") or "").upper(),
                                    str(item.get("code") or "").upper(),
                                }
                                and bool(item.get("enabled"))
                                and str(item.get("status") or "ACTIVE").upper() == "ACTIVE"
                            ),
                            None,
                        )
                        if historical_match is None:
                            current_plan = self.trading_plans.current(user_id)
                            if current_plan is not None:
                                plan = current_plan
                                plan_version_id = current_plan["version_id"]
                    assignment = _setup_assignment(
                        plan,
                        plan_setup_id=plan_setup_id,
                        setup_method=setup_method,
                        other_setup_note=other_setup_note,
                    )
            else:
                plan = self.trading_plans.current(user_id)
                plan_version_id = plan["version_id"] if plan else None
                assignment = _setup_assignment(
                    plan,
                    plan_setup_id=plan_setup_id,
                    setup_method=setup_method,
                    other_setup_note=other_setup_note,
                )
            if values["side"] == "BUY":
                evaluation_cycle_id = assignment.get("evaluation_cycle_id")
            values["setup_method"] = assignment["setup_method"]
            rows = self._trade_rows(connection, user_id)
            for row in rows:
                if str(row["execution_id"]) == execution_id:
                    row.update(
                        {
                            "traded_on": traded_on,
                            "traded_at": values["traded_at"],
                            "side": values["side"],
                            "quantity": quantity,
                            "price": price,
                            "fee": fee,
                            "method": values["method"],
                            "setup_method": values["setup_method"],
                            "stop_price": values["stop_price"],
                            "plan_version_id": plan_version_id,
                            "evaluation_cycle_id": evaluation_cycle_id,
                            "setup_code": assignment["setup_code"],
                            "setup_kind": assignment["setup_kind"],
                            "plan_setup_id": assignment["plan_setup_id"],
                            "setup_code_snapshot": assignment["setup_code_snapshot"],
                            "setup_name_snapshot": assignment["setup_name_snapshot"],
                            "other_setup_note": assignment["other_setup_note"],
                        }
                    )
                    break
            match_trade_executions(rows)
            connection.execute(
                """
                UPDATE user_trade_execution SET
                    traded_on=?, traded_at=?, side=?, quantity=?, price=?, fee=?,
                    method=?, setup_method=?, stop_price=?, rationale=?, invalidation=?,
                    exit_reason=?, market_context=?, observation_snapshot_json=?,
                    plan_version_id=?, evaluation_cycle_id=?, setup_code=?,
                    setup_kind=?, plan_setup_id=?, setup_code_snapshot=?,
                    setup_name_snapshot=?, other_setup_note=?,
                    revision=revision + 1, updated_at=CURRENT_TIMESTAMP
                WHERE user_id=? AND execution_id=?
                """,
                (
                    traded_on,
                    values["traded_at"],
                    values["side"],
                    quantity,
                    price,
                    fee,
                    values["method"],
                    values["setup_method"],
                    values["stop_price"],
                    rationale.strip(),
                    invalidation.strip(),
                    exit_reason.strip(),
                    market_context.strip(),
                    json.dumps(observation_snapshot or {}, ensure_ascii=False, sort_keys=True),
                    plan_version_id,
                    evaluation_cycle_id,
                    assignment["setup_code"],
                    assignment["setup_kind"],
                    assignment["plan_setup_id"],
                    assignment["setup_code_snapshot"],
                    assignment["setup_name_snapshot"],
                    assignment["other_setup_note"],
                    user_id,
                    execution_id,
                ),
            )

    def trade_executions(
        self,
        user_id: str,
        *,
        symbol: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict[str, Any]]:
        normalized_symbol = symbol.upper().strip() if symbol else None
        with self.connect() as connection:
            rows = self._trade_rows(connection, user_id, normalized_symbol, date_to)
        if date_from:
            date.fromisoformat(date_from)
            rows = [row for row in rows if str(row["traded_on"]) >= date_from]
        if date_to:
            date.fromisoformat(date_to)
        for row in rows:
            row["snapshot"] = json.loads(str(row.pop("observation_snapshot_json") or "{}"))
            _decorate_setup(row)
        return list(reversed(rows))

    def validate_trade_batch(
        self, user_id: str, trades: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        with self.connect() as connection:
            report, _ = self._prepare_trade_batch(connection, user_id, trades)
        for item in report["results"]:
            if item["status"] == "READY":
                item.pop("execution_id", None)
        return report

    def record_trade_batch(
        self,
        user_id: str,
        trades: Sequence[Mapping[str, Any]],
        *,
        idempotency_key: str,
        principal: ApiTokenPrincipal,
        request_id: str,
    ) -> dict[str, Any]:
        if principal.user_id != user_id:
            raise ValueError("Token 与交易用户不匹配")
        key = idempotency_key.strip()
        if not key or len(key) > 128:
            raise ValueError("Idempotency-Key 长度必须为 1 至 128 个字符")
        request_payload = [{str(k): v for k, v in trade.items()} for trade in trades]
        request_hash = _token_hash(
            json.dumps(
                request_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                """
                SELECT request_hash, response_json FROM user_trade_batch
                WHERE user_id=? AND idempotency_key=?
                """,
                (user_id, key),
            ).fetchone()
            if previous is not None:
                if not hmac.compare_digest(str(previous["request_hash"]), request_hash):
                    raise ValueError("同一 Idempotency-Key 不能用于不同交易批次")
                response = json.loads(str(previous["response_json"]))
                response["replayed"] = True
                self._insert_api_audit(
                    connection,
                    principal,
                    action="trades.batch",
                    request_id=request_id,
                    outcome="COMMITTED",
                    details={
                        "batch_id": response["batch_id"],
                        "created": response["created"],
                        "duplicates": response["duplicates"],
                        "replayed": True,
                    },
                )
                return response
            report, candidates = self._prepare_trade_batch(connection, user_id, trades)
            if report["rejected"]:
                raise TradeBatchValidationError(report)
            for candidate in candidates:
                connection.execute(
                    """
                    INSERT INTO user_trade_execution(
                        execution_id, user_id, traded_on, traded_at, source_ref, symbol, side,
                        quantity, price, fee, method, setup_method, stop_price,
                        initial_stop_price,
                        rationale, invalidation, exit_reason, market_context,
                        observation_snapshot_json, plan_version_id, evaluation_cycle_id, setup_code,
                        setup_kind, plan_setup_id, setup_code_snapshot,
                        setup_name_snapshot, other_setup_note
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        candidate["execution_id"],
                        user_id,
                        candidate["traded_on"],
                        candidate["traded_at"],
                        candidate["source_ref"],
                        candidate["symbol"],
                        candidate["side"],
                        candidate["quantity"],
                        candidate["price"],
                        candidate["fee"],
                        candidate["method"],
                        candidate["setup_method"],
                        candidate["stop_price"],
                        candidate["stop_price"] if candidate["side"] == "BUY" else None,
                        candidate["rationale"],
                        candidate["invalidation"],
                        candidate["exit_reason"],
                        candidate["market_context"],
                        json.dumps(
                            candidate.get("observation_snapshot") or {},
                            ensure_ascii=False,
                            sort_keys=True,
                            allow_nan=False,
                        ),
                        candidate.get("plan_version_id"),
                        candidate.get("evaluation_cycle_id"),
                        candidate.get("setup_code"),
                        candidate.get("setup_kind", "OTHER"),
                        candidate.get("plan_setup_id"),
                        candidate.get("setup_code_snapshot", ""),
                        candidate.get("setup_name_snapshot", ""),
                        candidate.get("other_setup_note", ""),
                    ),
                )
            results = []
            for item in report["results"]:
                result = dict(item)
                if result["status"] == "READY":
                    result["status"] = "CREATED"
                results.append(result)
            response = {
                "batch_id": uuid4().hex,
                "status": "COMMITTED",
                "created": report["ready"],
                "duplicates": report["duplicates"],
                "rejected": 0,
                "replayed": False,
                "results": results,
            }
            connection.execute(
                """
                INSERT INTO user_trade_batch(
                    batch_id, user_id, idempotency_key, request_hash, response_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    response["batch_id"],
                    user_id,
                    key,
                    request_hash,
                    json.dumps(response, ensure_ascii=False, sort_keys=True),
                ),
            )
            self._insert_api_audit(
                connection,
                principal,
                action="trades.batch",
                request_id=request_id,
                outcome="COMMITTED",
                details={
                    "batch_id": response["batch_id"],
                    "created": response["created"],
                    "duplicates": response["duplicates"],
                    "replayed": False,
                },
            )
        return response

    def update_trade_stop(
        self,
        user_id: str,
        execution_id: str,
        stop_price: float,
        *,
        expected_revision: int,
        principal: ApiTokenPrincipal,
        request_id: str,
    ) -> dict[str, Any]:
        if principal.user_id != user_id:
            raise ValueError("Token 与交易用户不匹配")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM user_trade_execution
                WHERE user_id=? AND execution_id=?
                """,
                (user_id, execution_id),
            ).fetchone()
            if row is None:
                raise ValueError("成交记录不存在")
            current = dict(row)
            if str(current["side"]) != "BUY":
                raise ValueError("止损价只能更新既有 BUY")
            normalized_stop = float(stop_price)
            if not math.isfinite(normalized_stop):
                raise ValueError("止损价必须为有限数值")
            if normalized_stop <= 0 or normalized_stop >= float(current["price"]):
                raise ValueError("止损价必须大于 0 且低于买入成交价")
            normalized_revision = _strict_positive_integer(
                expected_revision, field="expected_revision"
            )
            cursor = connection.execute(
                """
                UPDATE user_trade_execution
                SET stop_price=?, revision=revision + 1, updated_at=CURRENT_TIMESTAMP
                WHERE user_id=? AND execution_id=? AND revision=?
                """,
                (normalized_stop, user_id, execution_id, normalized_revision),
            )
            if cursor.rowcount != 1:
                raise TradeRevisionConflict("成交记录已变化，请重新读取后再更新")
            updated = connection.execute(
                """
                SELECT * FROM user_trade_execution
                WHERE user_id=? AND execution_id=?
                """,
                (user_id, execution_id),
            ).fetchone()
            assert updated is not None
            self._insert_api_audit(
                connection,
                principal,
                action="trades.stop",
                request_id=request_id,
                outcome="UPDATED",
                details={
                    "execution_id": execution_id,
                    "revision": int(updated["revision"]),
                },
            )
        assert updated is not None
        item = dict(updated)
        item["snapshot"] = json.loads(str(item.pop("observation_snapshot_json") or "{}"))
        return item

    def complete_trade_time(
        self,
        user_id: str,
        execution_id: str,
        traded_at: str,
        *,
        expected_revision: int,
        principal: ApiTokenPrincipal,
        request_id: str,
    ) -> dict[str, Any]:
        """Fill a missing execution time without turning the Agent API into a general editor."""
        if principal.user_id != user_id:
            raise ValueError("Token 与交易用户不匹配")
        normalized_time = str(traded_at or "").strip()
        try:
            datetime.strptime(normalized_time, "%H:%M:%S")
        except ValueError as exc:
            raise ValueError("成交时间必须为 HH:MM:SS") from exc
        normalized_revision = _strict_positive_integer(expected_revision, field="expected_revision")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current_row = connection.execute(
                """
                SELECT * FROM user_trade_execution
                WHERE user_id=? AND execution_id=?
                """,
                (user_id, execution_id),
            ).fetchone()
            if current_row is None:
                raise ValueError("成交记录不存在")
            current = dict(current_row)
            if int(current.get("revision") or 1) != normalized_revision:
                raise TradeRevisionConflict("成交记录已变化，请重新读取后再更新")
            if str(current.get("traded_at") or "").strip():
                raise ValueError("成交时间已存在；set-time 只能补全空时间")
            rows = self._trade_rows(connection, user_id)
            for row in rows:
                if str(row["execution_id"]) == execution_id:
                    row["traded_at"] = normalized_time
                    break
            ordered = sorted(
                rows,
                key=lambda row: (
                    str(row["traded_on"]),
                    str(row.get("traded_at") or ""),
                    str(row.get("created_at") or ""),
                    str(row["execution_id"]),
                ),
            )
            match_trade_executions(ordered)
            cursor = connection.execute(
                """
                UPDATE user_trade_execution
                SET traded_at=?, revision=revision + 1, updated_at=CURRENT_TIMESTAMP
                WHERE user_id=? AND execution_id=? AND revision=? AND traded_at=''
                """,
                (normalized_time, user_id, execution_id, normalized_revision),
            )
            if cursor.rowcount != 1:
                raise TradeRevisionConflict("成交记录已变化，请重新读取后再更新")
            updated = connection.execute(
                """
                SELECT * FROM user_trade_execution
                WHERE user_id=? AND execution_id=?
                """,
                (user_id, execution_id),
            ).fetchone()
            assert updated is not None
            self._insert_api_audit(
                connection,
                principal,
                action="trades.time",
                request_id=request_id,
                outcome="UPDATED",
                details={
                    "execution_id": execution_id,
                    "traded_at": normalized_time,
                    "revision": int(updated["revision"]),
                },
            )
        item = dict(updated)
        item["snapshot"] = json.loads(str(item.pop("observation_snapshot_json") or "{}"))
        return item

    def _prepare_trade_batch(
        self,
        connection: sqlite3.Connection,
        user_id: str,
        trades: Sequence[Mapping[str, Any]],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if not trades or len(trades) > 200:
            raise ValueError("每个交易批次必须包含 1 至 200 笔记录")
        existing = self._trade_rows(connection, user_id)
        working = list(existing)
        known: dict[tuple[Any, ...], Mapping[str, Any]] = {
            _trade_identity(row): row for row in existing
        }
        results: list[dict[str, Any] | None] = [None] * len(trades)
        normalized: list[dict[str, Any]] = []
        for index, payload in enumerate(trades):
            try:
                item = _normalize_api_trade(payload, index)
            except (TypeError, ValueError) as exc:
                results[index] = {
                    "index": index,
                    "client_id": str(payload.get("client_id") or ""),
                    "status": "REJECTED",
                    "reason": str(exc),
                }
            else:
                normalized.append(item)
        normalized.sort(key=lambda item: (item["traded_on"], item["traded_at"], int(item["index"])))
        candidates: list[dict[str, Any]] = []
        for item in normalized:
            index = int(item["index"])
            identity = _trade_identity(item)
            duplicate = known.get(identity)
            base_result = {
                "index": index,
                "client_id": item["client_id"],
                "symbol": item["symbol"],
                "traded_on": item["traded_on"],
                "traded_at": item["traded_at"],
                "side": item["side"],
                "quantity": item["quantity"],
                "price": item["price"],
            }
            if duplicate is not None:
                if item["source_ref"] and _trade_fingerprint(duplicate) != _trade_fingerprint(item):
                    results[index] = {
                        **base_result,
                        "status": "REJECTED",
                        "reason": "source_ref 已用于另一笔不同的成交",
                    }
                    continue
                results[index] = {
                    **base_result,
                    "status": "DUPLICATE",
                    "execution_id": str(duplicate["execution_id"]),
                    "revision": int(duplicate.get("revision") or 1),
                }
                continue
            if item["side"] == "BUY":
                requested = item.get("plan_version_id")
                active = self.trading_plans.current(user_id)
                if requested:
                    plan = self.trading_plans.show(user_id, str(requested))
                    if plan is None:
                        results[index] = {
                            **base_result,
                            "status": "REJECTED",
                            "reason": "计划版本不存在或不属于当前用户",
                        }
                        continue
                else:
                    plan = active
                if plan is not None:
                    item["plan_version_id"] = plan["version_id"]
                assignment = _setup_assignment(
                    plan,
                    plan_setup_id=str(item.get("plan_setup_id") or OTHER_SETUP_ID),
                    setup_code=item.get("setup_code"),
                    setup_method=str(item.get("explicit_setup_method") or ""),
                    other_setup_note=str(item.get("other_setup_note") or ""),
                )
                item.update(assignment)
                if assignment["setup_kind"] == "PLAN":
                    observation_row = connection.execute(
                        """SELECT setup_code_snapshot, setup_name_snapshot, status,
                                  evidence_json, updated_at
                        FROM user_setup_observation
                        WHERE user_id=? AND symbol=? AND setup_id=?""",
                        (user_id, item["symbol"], assignment["plan_setup_id"]),
                    ).fetchone()
                    if observation_row is not None:
                        snapshot = dict(item.get("observation_snapshot") or {})
                        try:
                            evidence = json.loads(str(observation_row["evidence_json"] or "{}"))
                        except (TypeError, ValueError, json.JSONDecodeError):
                            evidence = {}
                        snapshot["setup_observation"] = {
                            "setup_id": assignment["plan_setup_id"],
                            "setup_code": str(observation_row["setup_code_snapshot"]),
                            "setup_name": str(observation_row["setup_name_snapshot"]),
                            "status": str(observation_row["status"]),
                            "evidence": evidence if isinstance(evidence, dict) else {},
                            "updated_at": str(observation_row["updated_at"]),
                        }
                        item["observation_snapshot"] = snapshot
            else:
                item["plan_version_id"] = None
                item["evaluation_cycle_id"] = None
                item.update(_setup_assignment(None))
            execution_id = uuid4().hex
            candidate = {
                **item,
                "execution_id": execution_id,
                "revision": 1,
                "created_at": f"batch-{index:06d}",
            }
            try:
                ordered = sorted(
                    [*working, candidate],
                    key=lambda row: (
                        str(row["traded_on"]),
                        str(row.get("traded_at") or ""),
                        str(row.get("created_at") or ""),
                        str(row["execution_id"]),
                    ),
                )
                match_trade_executions(ordered)
            except ValueError:
                available = open_quantity(
                    [row for row in working if str(row["symbol"]) == item["symbol"]]
                )
                results[index] = {
                    **base_result,
                    "status": "REJECTED",
                    "reason": f"卖出数量超过可复盘持仓：可用 {available} 股",
                }
                continue
            working.append(candidate)
            known[identity] = candidate
            candidates.append(candidate)
            results[index] = {
                **base_result,
                "status": "READY",
                "execution_id": execution_id,
                "revision": 1,
            }
        final_results = [item for item in results if item is not None]
        return {
            "status": "VALID"
            if all(item["status"] != "REJECTED" for item in final_results)
            else "INVALID",
            "ready": sum(item["status"] == "READY" for item in final_results),
            "duplicates": sum(item["status"] == "DUPLICATE" for item in final_results),
            "rejected": sum(item["status"] == "REJECTED" for item in final_results),
            "results": final_results,
        }, candidates

    def chart_trade_overlay(
        self, user_id: str, symbol: str, end_date: str
    ) -> dict[str, list[dict[str, Any]]]:
        with self.connect() as connection:
            rows = self._trade_rows(connection, user_id, symbol.upper(), end_date)
        _, open_lots, _ = match_trade_executions(rows, permit_unmatched_sells=True)
        return {
            "executions": [
                {
                    key: row[key]
                    for key in (
                        "execution_id",
                        "traded_on",
                        "side",
                        "quantity",
                        "price",
                        "stop_price",
                    )
                }
                for row in rows
            ],
            "open_stops": [
                {
                    "buy_execution_id": lot["buy_execution_id"],
                    "buy_date": lot["buy_date"],
                    "stop_price": lot["stop_price"],
                }
                for lot in open_lots
                if lot.get("stop_price") is not None
            ],
        }

    def chart_drawings(
        self, user_id: str, symbol: str, price_scale_id: str, interval: str = "day"
    ) -> list[dict[str, Any]]:
        if interval not in {"day", "week"}:
            raise ValueError("unsupported chart interval")
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT drawing_id, tool, anchors_json, content_text, interval_scope,
                       created_at, updated_at
                FROM user_chart_drawing
                WHERE user_id=? AND symbol=? AND price_scale_id=?
                  AND tool IN ('trendline', 'horizontal', 'text')
                  AND interval_scope IN ('both', ?)
                ORDER BY created_at, drawing_id
                """,
                (user_id, symbol.upper(), price_scale_id, interval),
            ).fetchall()
        return [{**dict(row), "anchors": json.loads(str(row["anchors_json"]))} for row in rows]

    def save_chart_drawing(
        self,
        user_id: str,
        drawing_id: str,
        symbol: str,
        price_scale_id: str,
        tool: str,
        anchors: Sequence[Mapping[str, Any]],
        content_text: str = "",
        interval_scope: str = "both",
    ) -> dict[str, Any]:
        normalized, normalized_content, normalized_interval = _normalize_drawing(
            tool, price_scale_id, anchors, content_text, interval_scope
        )
        with self.connect() as connection:
            existing = connection.execute(
                """SELECT user_id, symbol, price_scale_id, tool
                FROM user_chart_drawing WHERE drawing_id=?""",
                (drawing_id,),
            ).fetchone()
            if existing is not None and str(existing["user_id"]) != user_id:
                raise ValueError("画线记录不属于当前用户")
            if existing is not None and (
                str(existing["symbol"]) != symbol.upper()
                or str(existing["price_scale_id"]) != price_scale_id
                or str(existing["tool"]) != tool
            ):
                raise ValueError("画线记录与当前图表不匹配")
            connection.execute(
                """
                INSERT INTO user_chart_drawing(
                    drawing_id, user_id, symbol, price_scale_id, tool, anchors_json,
                    content_text, interval_scope
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(drawing_id) DO UPDATE SET
                    anchors_json=excluded.anchors_json,
                    content_text=excluded.content_text,
                    interval_scope=excluded.interval_scope,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    drawing_id,
                    user_id,
                    symbol.upper(),
                    price_scale_id,
                    tool,
                    json.dumps(normalized, ensure_ascii=False, separators=(",", ":")),
                    normalized_content,
                    normalized_interval,
                ),
            )
        return {
            "drawing_id": drawing_id,
            "tool": tool,
            "anchors": normalized,
            "content_text": normalized_content,
            "interval_scope": normalized_interval,
        }

    def delete_chart_drawing(
        self, user_id: str, drawing_id: str, symbol: str, price_scale_id: str
    ) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM user_chart_drawing
                WHERE user_id=? AND drawing_id=? AND symbol=? AND price_scale_id=?
                """,
                (user_id, drawing_id, symbol.upper(), price_scale_id),
            )
        return cursor.rowcount == 1

    @staticmethod
    def _trade_rows(
        connection: sqlite3.Connection,
        user_id: str,
        symbol: str | None = None,
        end_date: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["user_id=?"]
        parameters: list[Any] = [user_id]
        if symbol is not None:
            clauses.append("symbol=?")
            parameters.append(symbol)
        if end_date is not None:
            clauses.append("traded_on<=?")
            parameters.append(end_date)
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM user_trade_execution WHERE "
                + " AND ".join(clauses)
                + " ORDER BY traded_on, traded_at, created_at, execution_id",
                tuple(parameters),
            ).fetchall()
        ]
