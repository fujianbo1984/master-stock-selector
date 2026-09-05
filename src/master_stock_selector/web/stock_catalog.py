"""Read-only inventory of stored A shares, independent of screening eligibility."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from threading import Lock
from typing import Any

from ..watchlist.brooks import BROOKS_POLICY_VERSION


class StockCatalog:
    def __init__(self, market_path: Path, facts_path: Path):
        self.paths = (Path(market_path), Path(facts_path))
        self._cache: dict[tuple[Any, ...], dict[str, dict[str, Any]]] = {}
        self._lock = Lock()

    def inventory(self, cutoff: str) -> dict[str, dict[str, Any]]:
        fingerprint: list[tuple[int, int] | None] = []
        for path in self.paths:
            for candidate in (path, Path(str(path) + "-wal")):
                try:
                    stat = candidate.stat()
                    fingerprint.append((stat.st_mtime_ns, stat.st_size))
                except FileNotFoundError:
                    fingerprint.append(None)
        key = (cutoff, *fingerprint)
        with self._lock:
            if key not in self._cache:
                rows = self._read(cutoff)
                if len(self._cache) >= 8:
                    self._cache.clear()
                self._cache[key] = rows
            return self._cache[key]

    def _read(self, cutoff: str) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}

        def add(symbol: str, **fields: Any) -> None:
            symbol = symbol.upper()
            if not re.fullmatch(r"\d{6}\.(SH|SZ)", symbol):
                return
            item = result.setdefault(symbol, {"symbol": symbol, "name": "", "industry": ""})
            item.update({key: value for key, value in fields.items() if value is not None})

        for path in reversed(self.paths):
            if not path.is_file():
                continue
            with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as db:
                db.row_factory = sqlite3.Row
                tables = {
                    row[0]
                    for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                if "stock_method_daily_fact" in tables:
                    rows = db.execute(
                        """
                        WITH latest AS (
                            SELECT symbol, MAX(as_of_date) AS as_of_date
                            FROM stock_method_daily_fact WHERE as_of_date <= ? GROUP BY symbol
                        )
                        SELECT f.symbol, f.as_of_date, f.evidence_json
                        FROM latest JOIN stock_method_daily_fact f
                        ON f.symbol=latest.symbol AND f.as_of_date=latest.as_of_date
                    """,
                        (cutoff,),
                    )
                    for row in rows:
                        identity = json.loads(row["evidence_json"] or "{}").get("eligibility", {})
                        add(
                            row["symbol"],
                            **{
                                k: identity[k]
                                for k in ("name", "industry", "list_date", "is_st", "is_suspended")
                                if k in identity
                            },
                            fact_date=row["as_of_date"],
                        )
                if "brooks_second_leg_daily_fact" in tables:
                    for row in db.execute(
                        "SELECT symbol, MAX(as_of_date) AS fact_date FROM brooks_second_leg_daily_fact "
                        "WHERE as_of_date<=? AND policy_version=? GROUP BY symbol",
                        (cutoff, BROOKS_POLICY_VERSION),
                    ):
                        previous_date = result.get(row["symbol"], {}).get("fact_date", "")
                        add(row["symbol"], fact_date=max(previous_date, row["fact_date"]))
                for table in ("security_identity_history", "security_master_history"):
                    if table not in tables:
                        continue
                    for row in db.execute(
                        f"""
                        SELECT h.* FROM {table} h WHERE h.valid_from <= ?
                        AND h.valid_from=(SELECT MAX(x.valid_from) FROM {table} x
                            WHERE x.symbol=h.symbol AND x.valid_from <= ?)
                    """,
                        (cutoff, cutoff),
                    ):
                        fields = dict(row)
                        add(
                            fields["symbol"],
                            **{
                                k: fields[k]
                                for k in ("name", "industry", "list_date", "is_st", "is_suspended")
                                if k in fields
                            },
                        )
                if (
                    "security_master_snapshots" in tables
                    and "security_master_history" not in tables
                ):
                    snapshot = db.execute(
                        "SELECT members_json FROM security_master_snapshots WHERE market='ashare' AND as_of_date<=? ORDER BY as_of_date DESC LIMIT 1",
                        (cutoff,),
                    ).fetchone()
                    for member in json.loads(snapshot[0] or "[]") if snapshot else []:
                        symbol = member.get("ts_code") or member.get("symbol") or ""
                        add(
                            symbol,
                            **{
                                k: member[k]
                                for k in ("name", "industry", "list_date", "is_st", "is_suspended")
                                if k in member
                            },
                        )
                if "daily_bars" in tables:
                    # Use the covering primary-key index for the inventory; resolve
                    # PIT freshness for matching symbols instead of scanning every OHLC row.
                    for row in db.execute(
                        "SELECT DISTINCT symbol FROM daily_bars WHERE market='ashare' AND trade_date<=?",
                        (cutoff,),
                    ):
                        add(row["symbol"])
        return result

    def lookup(self, symbol: str, cutoff: str) -> dict[str, Any] | None:
        identity = self.inventory(cutoff).get(symbol)
        if identity is None:
            return None
        item = dict(identity)
        path = self.paths[0]
        if path.is_file():
            with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as db:
                columns = {row[1] for row in db.execute("PRAGMA table_info(daily_bars)")}
                if columns:
                    pit = " AND data_as_of_date <= ?" if "data_as_of_date" in columns else ""
                    params = (symbol, cutoff, cutoff) if pit else (symbol, cutoff)
                    row = db.execute(
                        "SELECT MAX(trade_date) FROM daily_bars WHERE market='ashare' AND symbol=? AND trade_date<=?"
                        + pit,
                        params,
                    ).fetchone()
                    item["market_date"] = row[0] or ""
        return item

    def search(self, query: str, cutoff: str, limit: int = 20) -> list[dict[str, Any]]:
        query = query.strip().casefold()
        if not query:
            return []
        rows = [
            dict(item)
            for item in self.inventory(cutoff).values()
            if query in item["symbol"].casefold() or query in item["name"].casefold()
        ]
        rows.sort(
            key=lambda item: (
                item["symbol"].split(".")[0].casefold() != query
                and item["symbol"].casefold() != query,
                item["name"].casefold() != query,
                item["symbol"],
            )
        )
        return [self.lookup(item["symbol"], cutoff) or item for item in rows[:limit]]
