from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from .display_time import sqlite_utc_to_shanghai
from .user_store import UserStore
from .users import (
    OTHER_SETUP_ID,
    OTHER_SETUP_LABEL,
    ApiTokenPrincipal,
    _review_value,
    _validated_c1_record,
    _validated_setup_observation,
    normalize_observation_tags,
)


class UserObservationStore(UserStore):
    def reviews_for_symbols(
        self, user_id: str, symbols: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        normalized = sorted({symbol.upper() for symbol in symbols if symbol})
        if not normalized:
            return {}
        placeholders = ",".join("?" for _ in normalized)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT symbol, manual_state, note, tags_json, c1_record_json, reviewed_at
                FROM user_stock_review
                WHERE user_id=? AND symbol IN ({placeholders})
                """,
                (user_id, *normalized),
            ).fetchall()
        return {str(row["symbol"]): _review_value(row) for row in rows}

    def reviews_for_user(self, user_id: str) -> list[dict[str, Any]]:
        """Return the user's stocks that currently belong to their observation workspace."""
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT symbol, manual_state, note, tags_json, c1_record_json, reviewed_at
                FROM user_stock_review
                WHERE user_id=?
                ORDER BY reviewed_at DESC, symbol
                """,
                (user_id,),
            ).fetchall()
        return [_review_value(row) for row in rows]

    def review(self, user_id: str, symbol: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT symbol, manual_state, note, tags_json, c1_record_json, reviewed_at
                FROM user_stock_review WHERE user_id=? AND symbol=?
                """,
                (user_id, symbol.upper()),
            ).fetchone()
        if row is not None:
            return _review_value(row)
        return {
            "symbol": symbol.upper(),
            "manual_state": "UNREVIEWED",
            "note": "",
            "tags": [],
            "c1_record": {},
            "reviewed_at": "",
        }

    def save_review(
        self,
        user_id: str,
        symbol: str,
        manual_state: str,
        note: str,
        tags: str | Sequence[str] | None = None,
    ) -> None:
        state = manual_state.upper()
        if state == "UNREVIEWED":
            with self.connect() as connection:
                connection.execute(
                    "DELETE FROM user_stock_review WHERE user_id=? AND symbol=?",
                    (user_id, symbol.upper()),
                )
            return
        if state not in {"WATCH", "FOCUS", "ARCHIVED"}:
            raise ValueError(f"unsupported manual state: {manual_state}")
        normalized_tags = normalize_observation_tags(tags) if tags is not None else None
        with self.connect() as connection:
            if normalized_tags is None:
                connection.execute(
                    """
                    INSERT INTO user_stock_review(
                        user_id, symbol, manual_state, note, reviewed_at
                    ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(user_id, symbol) DO UPDATE SET
                        manual_state=excluded.manual_state,
                        note=excluded.note,
                        reviewed_at=CURRENT_TIMESTAMP
                    """,
                    (user_id, symbol.upper(), state, note.strip()),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO user_stock_review(
                        user_id, symbol, manual_state, note, tags_json, reviewed_at
                    ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(user_id, symbol) DO UPDATE SET
                        manual_state=excluded.manual_state,
                        note=excluded.note,
                        tags_json=excluded.tags_json,
                        reviewed_at=CURRENT_TIMESTAMP
                    """,
                    (
                        user_id,
                        symbol.upper(),
                        state,
                        note.strip(),
                        json.dumps(normalized_tags, ensure_ascii=False),
                    ),
                )

    def add_observations(
        self,
        principal: ApiTokenPrincipal,
        symbols: Sequence[str],
        *,
        note: str = "",
        tags: str | Sequence[str] | None = None,
        request_id: str,
    ) -> dict[str, Any]:
        """Add many observations atomically without changing existing reviews."""
        normalized = list(dict.fromkeys(symbol.upper().strip() for symbol in symbols if symbol))
        if not normalized:
            raise ValueError("股票代码不能为空")
        normalized_tags = normalize_observation_tags(tags if tags is not None else ())
        placeholders = ",".join("?" for _ in normalized)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = {
                str(row["symbol"])
                for row in connection.execute(
                    f"""SELECT symbol FROM user_stock_review
                    WHERE user_id=? AND symbol IN ({placeholders})""",
                    (principal.user_id, *normalized),
                ).fetchall()
            }
            added = [symbol for symbol in normalized if symbol not in existing]
            connection.executemany(
                """INSERT INTO user_stock_review(
                    user_id, symbol, manual_state, note, tags_json, reviewed_at
                ) VALUES (?, ?, 'WATCH', ?, ?, CURRENT_TIMESTAMP)""",
                [
                    (
                        principal.user_id,
                        symbol,
                        note.strip(),
                        json.dumps(normalized_tags, ensure_ascii=False),
                    )
                    for symbol in added
                ],
            )
            results = [
                {
                    "symbol": symbol,
                    "status": "ALREADY_PRESENT" if symbol in existing else "ADDED",
                }
                for symbol in normalized
            ]
            self._insert_api_audit(
                connection,
                principal,
                action="observations.batch",
                request_id=request_id,
                outcome="COMMITTED",
                details={
                    "requested": len(normalized),
                    "added": len(added),
                    "already_present": len(existing),
                },
            )
        return {
            "request_id": request_id,
            "status": "COMMITTED",
            "requested": len(normalized),
            "added": len(added),
            "already_present": len(existing),
            "results": results,
        }

    def save_c1_record(
        self, user_id: str, symbol: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        record = _validated_c1_record(payload)
        encoded = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with self.connect() as connection:
            plan = self.trading_plans.current(user_id)
            setup = next(
                (
                    item
                    for item in (plan or {}).get("setup_catalog", [])
                    if str(item.get("setup_id") or "") == "C1"
                ),
                {"code": "C1", "name": "区间失败测试"},
            )
            connection.execute(
                """
                INSERT INTO user_stock_review(
                    user_id, symbol, manual_state, note, c1_record_json, reviewed_at
                ) VALUES (?, ?, 'WATCH', '', ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, symbol) DO UPDATE SET
                    c1_record_json=excluded.c1_record_json,
                    reviewed_at=CURRENT_TIMESTAMP
                """,
                (user_id, symbol.upper(), encoded),
            )
            connection.execute(
                """INSERT INTO user_setup_observation(
                    user_id, symbol, setup_id, plan_version_id,
                    setup_code_snapshot, setup_name_snapshot, status, evidence_json,
                    updated_at
                ) VALUES (?, ?, 'C1', ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, symbol, setup_id) DO UPDATE SET
                    plan_version_id=excluded.plan_version_id,
                    setup_code_snapshot=excluded.setup_code_snapshot,
                    setup_name_snapshot=excluded.setup_name_snapshot,
                    status=excluded.status,
                    evidence_json=excluded.evidence_json,
                    updated_at=CURRENT_TIMESTAMP""",
                (
                    user_id,
                    symbol.upper(),
                    (plan or {}).get("version_id"),
                    str(setup.get("code") or "C1"),
                    str(setup.get("name") or "区间失败测试"),
                    "READY"
                    if record["structure_state"] == "READY"
                    else ("INVALID" if record["structure_state"] == "INVALID" else "WATCH"),
                    encoded,
                ),
            )
        return record

    def clear_c1_record(self, user_id: str, symbol: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE user_stock_review
                SET c1_record_json='{}', reviewed_at=CURRENT_TIMESTAMP
                WHERE user_id=? AND symbol=?""",
                (user_id, symbol.upper()),
            )
            connection.execute(
                "DELETE FROM user_setup_observation WHERE user_id=? AND symbol=? AND setup_id='C1'",
                (user_id, symbol.upper()),
            )

    def setup_observations(self, user_id: str, symbol: str) -> list[dict[str, Any]]:
        with self.connect(read_only=True) as connection:
            rows = connection.execute(
                """SELECT * FROM user_setup_observation
                WHERE user_id=? AND symbol=?
                ORDER BY updated_at DESC, setup_code_snapshot""",
                (user_id, symbol.upper()),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                evidence = json.loads(str(item.pop("evidence_json") or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                evidence = {}
            item["evidence"] = evidence if isinstance(evidence, dict) else {}
            item["updated_at"] = sqlite_utc_to_shanghai(item.get("updated_at"))
            result.append(item)
        return result

    def save_setup_observation(
        self,
        user_id: str,
        symbol: str,
        setup_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        plan = self.trading_plans.current(user_id)
        if plan is None:
            raise ValueError("请先发布交易计划")
        setup = next(
            (
                item
                for item in plan.get("setup_catalog", [])
                if str(item.get("setup_id") or "").upper() == setup_id.upper()
                and bool(item.get("enabled"))
                and str(item.get("status") or "ACTIVE") == "ACTIVE"
            ),
            None,
        )
        if setup is None:
            raise ValueError("交易形态不存在、未启用或已归档")
        evidence = _validated_setup_observation(payload)
        status = str(evidence.pop("status"))
        encoded = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO user_stock_review(
                    user_id, symbol, manual_state, note, reviewed_at
                ) VALUES (?, ?, 'WATCH', '', CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, symbol) DO UPDATE SET reviewed_at=CURRENT_TIMESTAMP""",
                (user_id, symbol.upper()),
            )
            connection.execute(
                """INSERT INTO user_setup_observation(
                    user_id, symbol, setup_id, plan_version_id,
                    setup_code_snapshot, setup_name_snapshot, status, evidence_json,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, symbol, setup_id) DO UPDATE SET
                    plan_version_id=excluded.plan_version_id,
                    setup_code_snapshot=excluded.setup_code_snapshot,
                    setup_name_snapshot=excluded.setup_name_snapshot,
                    status=excluded.status,
                    evidence_json=excluded.evidence_json,
                    updated_at=CURRENT_TIMESTAMP""",
                (
                    user_id,
                    symbol.upper(),
                    str(setup["setup_id"]),
                    str(plan["version_id"]),
                    str(setup["code"]),
                    str(setup["name"]),
                    status,
                    encoded,
                ),
            )
        return {"status": status, **evidence}

    def clear_setup_observation(self, user_id: str, symbol: str, setup_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM user_setup_observation WHERE user_id=? AND symbol=? AND setup_id=?",
                (user_id, symbol.upper(), setup_id),
            )

    def trade_setup_options(
        self, user_id: str, *, plan_version_id: str | None = None
    ) -> list[dict[str, str]]:
        plan = (
            self.trading_plans.show(user_id, plan_version_id)
            if plan_version_id
            else self.trading_plans.current(user_id)
        )
        options = [{"value": OTHER_SETUP_ID, "label": OTHER_SETUP_LABEL}]
        if plan is not None:
            options.extend(
                {
                    "value": str(item["setup_id"]),
                    "label": f"{item['code']} · {item['name']}",
                }
                for item in plan.get("setup_catalog", [])
                if item.get("enabled")
            )
        return options
