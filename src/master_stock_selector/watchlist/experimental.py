"""Frozen, observation-only rules from watchlist_tuning; no future labels or orders."""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

POLICY = "early-active-watchlist-190-v1"
KEYS = ("turnover", "volume_dry", "up_down_amount", "rs20", "limit60", "range20")
DIVISORS = (6.0, 1.2, 1.5, 0.10, 2.0, 0.30)
SELECTED = (4.0, 1.3, 1.0, 0.05, 1.0, 0.25)
MODELS = {"selected": "冻结窄池 · 六项条件", "reference": "简单参考 · 两项粗筛"}


def select_rows(rows: list[dict[str, Any]], model: str = "selected") -> list[dict[str, Any]]:
    """Input eligibility must certify the report's full PIT base pool, not just six factors."""
    if model not in MODELS:
        raise ValueError("Unknown experimental model")
    result = []
    seen: set[str] = set()
    for row in rows:
        symbol = str(row["symbol"])
        if symbol in seen:
            raise ValueError("Duplicate security in a single observation day")
        seen.add(symbol)
        if row.get("eligible") is not True:
            continue
        try:
            values = tuple(float(row[k]) for k in KEYS)
        except (KeyError, TypeError, ValueError):
            continue
        if not all(math.isfinite(v) for v in values):
            continue
        if not (4 <= values[0] <= 20 and 0.25 <= values[5] <= 0.80):
            continue
        if model == "selected" and any(v < low for v, low in zip(values, SELECTED)):
            continue
        score = sum(min(2.0, max(0.0, v / d)) for v, d in zip(values, DIVISORS))
        result.append({**row, "score": score})
    result.sort(key=lambda row: (-row["score"], row["symbol"]))
    return [{**row, "rank": i} for i, row in enumerate(result[:20], 1)]


def read_snapshot(path: Path, query_date: str | None, model: str) -> dict[str, Any]:
    """Never substitute another day's candidates for a requested date."""
    if model not in MODELS:
        raise ValueError("Unknown experimental model")
    if query_date:
        date.fromisoformat(query_date)
    result: dict[str, Any] = dict(rows=[], query_date=query_date or "", latest_date="",
                                  earliest_date="", available=False, metadata={}, pool_count=0,
                                  previous_date=None, next_date=None, error="")
    if not path.is_file():
        result["error"] = "实验数据尚未准备，当前没有可展示的名单。"
        return result
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as con:
            con.row_factory = sqlite3.Row
            metadata = json.loads(con.execute("SELECT payload FROM metadata").fetchone()[0])
            if metadata.get("policy") != POLICY:
                raise ValueError("Unsupported policy")
            first, latest = con.execute("SELECT min(date), max(date) FROM observation_days").fetchone()
            selected_date = query_date or latest
            result.update(query_date=selected_date, latest_date=latest, earliest_date=first,
                          metadata=metadata)
            day = con.execute("SELECT pool_count FROM observation_days WHERE date=?", (selected_date,)).fetchone()
            if day is not None:
                result.update(available=True, pool_count=day[0])
                result["rows"] = [dict(row) for row in con.execute(
                    "SELECT * FROM observations WHERE date=? AND model=? ORDER BY rank",
                    (selected_date, model))]
            else:
                result["error"] = "所选日期没有实验数据；未使用其他日期的名单替代。"
            result["previous_date"] = con.execute(
                "SELECT max(date) FROM observation_days WHERE date<?", (selected_date,)).fetchone()[0]
            result["next_date"] = con.execute(
                "SELECT min(date) FROM observation_days WHERE date>?", (selected_date,)).fetchone()[0]
    except (sqlite3.Error, ValueError, TypeError, KeyError, IndexError):
        result.update(available=False, rows=[], error="实验数据不可用，请检查数据文件与规则版本。")
    return result
