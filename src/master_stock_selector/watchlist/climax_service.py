"""Full-universe batch computation and read-only chart projection.

Run with the project interpreter: -m master_stock_selector.watchlist.climax_service
Schema changes require --migrate --apply; computation requires --apply.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import zlib
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .climax import POLICY_VERSION, build_climax, ordinary_ashare
from .repository import MarketDataReader

SCHEMA = """
CREATE TABLE IF NOT EXISTS climax_stock_document (
    policy_version TEXT NOT NULL, symbol TEXT NOT NULL, as_of_date TEXT NOT NULL,
    document BLOB NOT NULL, PRIMARY KEY (policy_version, symbol)
);
CREATE TABLE IF NOT EXISTS climax_run (
    policy_version TEXT NOT NULL, as_of_date TEXT NOT NULL, receipt_json TEXT NOT NULL,
    PRIMARY KEY (policy_version, as_of_date)
);
"""


def ready(path: Path) -> bool:
    if not path.is_file():
        return False
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as c:
        return (
            c.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN ('climax_stock_document','climax_run')"
            ).fetchone()[0]
            == 2
        )


def migrate(path: Path) -> dict[str, Any]:
    existed = path.is_file()
    backup = ""
    if ready(path):
        return {"status": "REUSED", "database": str(path)}
    if existed:
        backup = (
            str(path) + ".before-climax-" + datetime.now().strftime("%Y%m%d%H%M%S") + ".sqlite3"
        )
        with (
            sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as source,
            sqlite3.connect(backup) as dest,
        ):
            source.backup(dest)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as c:
        c.executescript(SCHEMA)
    return {"status": "SUCCESS", "database": str(path), "backup": backup}


def _pack(value: dict[str, Any]) -> bytes:
    return zlib.compress(
        json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(), 6
    )


def _unpack(value: bytes) -> dict[str, Any]:
    return json.loads(zlib.decompress(value))


def source_revision(path: Path) -> dict[str, list[int]]:
    return {
        str(p.resolve()): [p.stat().st_size, p.stat().st_mtime_ns]
        for p in (path, Path(str(path) + "-wal"))
        if p.exists()
    }


def _verify_prefix(old: dict[str, Any], new: dict[str, Any]) -> None:
    if old["daily"] != new["daily"][: len(old["daily"])]:
        raise ValueError(
            f"{old['symbol']}: historical inputs changed; rebuild into a separate database/version, do not overwrite saved facts"
        )
    new_events = {e["id"]: e for e in new["events"]}
    for event in old["events"]:
        candidate = new_events.get(event["id"])
        if (
            candidate is None
            or event["evidence"] != candidate["evidence"]
            or event["timeline"] != candidate["timeline"][: len(event["timeline"])]
        ):
            raise ValueError(f"{old['symbol']}: saved event prefix changed")


def run_climax(
    market_database: Path, watchlist_database: Path, as_of_date: str = ""
) -> dict[str, Any]:
    if not ready(watchlist_database):
        raise ValueError(
            "扩张图层表尚未初始化；使用项目虚拟环境运行 -m master_stock_selector.watchlist.climax_service --migrate --apply"
        )
    revision = source_revision(market_database)
    reader = MarketDataReader(market_database)
    cutoff = as_of_date or reader.latest_market_date()
    dates = reader.trading_dates(cutoff)
    if not dates or dates[-1] != cutoff:
        raise ValueError("requested cutoff is not a collected market trading date")
    with sqlite3.connect(watchlist_database) as existing:
        found = existing.execute(
            "SELECT receipt_json FROM climax_run WHERE policy_version=? AND as_of_date=?",
            (POLICY_VERSION, cutoff),
        ).fetchone()
        if found:
            receipt = json.loads(found[0])
            if receipt.get("source_revision") != revision:
                raise ValueError(
                    "source changed since saved batch; use a separate reconstruction database/version"
                )
            return {**receipt, "status": "REUSED"}
        latest = existing.execute(
            "SELECT max(as_of_date) FROM climax_run WHERE policy_version=?", (POLICY_VERSION,)
        ).fetchone()[0]
        if latest and cutoff < latest:
            raise ValueError(
                "cannot backdate the active batch; use a separate reconstruction database"
            )
    with reader.connect() as c:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"security_master_history", "daily_metrics"}
        if not required <= tables:
            raise ValueError("missing security history or market-value source")
        histories: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in c.execute(
            "SELECT * FROM security_master_history WHERE valid_from<=? ORDER BY symbol,valid_from",
            (cutoff,),
        ):
            histories[str(r["symbol"])].append(dict(r))
        st: dict[str, set[str]] = defaultdict(set)
        if "security_st_daily_fact" in tables:
            for r in c.execute(
                "SELECT trade_date,symbol FROM security_st_daily_fact WHERE trade_date<=?",
                (cutoff,),
            ):
                st[r[0]].add(r[1])
        live_dates: set[str] = set()
        if "watchlist_market_collection_receipt" in tables:
            for r in c.execute(
                "SELECT trade_date,quality_json FROM watchlist_market_collection_receipt WHERE status='SUCCESS' AND trade_date<=?",
                (cutoff,),
            ):
                live_dates.add(r[0])
                quality = json.loads(r[1] or "{}")
                # Known open sessions supplement quote dates, including whole-market collection gaps.
                dates = sorted(
                    set(dates)
                    | {d for d in quality.get("week_open_dates", []) if dates[0] <= d <= cutoff}
                )
        metrics: dict[str, dict[str, float]] = defaultdict(dict)
        cols = {r[1] for r in c.execute("PRAGMA table_info(daily_metrics)")}
        known_clause = " AND data_as_of_date<=?" if "data_as_of_date" in cols else ""
        args = (cutoff, cutoff) if known_clause else (cutoff,)
        for r in c.execute(
            "SELECT symbol,trade_date,total_mv FROM daily_metrics WHERE market='ashare' AND trade_date<=?"
            + known_clause,
            args,
        ):
            metrics[r[0]][r[1]] = r[2]
    counts: Counter[str] = Counter()
    seen: set[str] = set()
    with sqlite3.connect(watchlist_database, timeout=60) as dest:
        dest.execute("BEGIN IMMEDIATE")

        def save(symbol: str, raw: list[dict[str, Any]]) -> None:
            history = histories.get(symbol, [])
            starts = [r["valid_from"] for r in history]
            metadata: dict[str, dict[str, Any]] = {}
            for d in dates:
                pos = bisect_right(starts, d) - 1
                identity = history[pos] if pos >= 0 else {}
                active = (
                    None
                    if not identity
                    else (
                        identity.get("listing_status_as_of") == "listed"
                        and (not identity.get("list_date") or identity["list_date"] <= d)
                        and (not identity.get("delist_date") or d < identity["delist_date"])
                    )
                )
                metadata[d] = dict(
                    active=active,
                    suspended=bool(identity.get("is_suspended")),
                    st_known=d in st or d in live_dates,
                    is_st=symbol in st.get(d, set()) if d in st else bool(identity.get("is_st")),
                    total_mv=metrics.get(symbol, {}).get(d),
                )
            document = build_climax(symbol, dates, {r["trade_date"]: r for r in raw}, metadata)
            old = dest.execute(
                "SELECT document FROM climax_stock_document WHERE policy_version=? AND symbol=?",
                (POLICY_VERSION, symbol),
            ).fetchone()
            if old:
                _verify_prefix(_unpack(old[0]), document)
            dest.execute(
                "INSERT OR REPLACE INTO climax_stock_document VALUES (?,?,?,?)",
                (POLICY_VERSION, symbol, cutoff, _pack(document)),
            )
            counts["symbols"] += 1
            counts["events"] += len(document["events"])
            counts[document["daily"][-1]["reason"]] += 1

        for symbol, raw in reader.iter_brooks_bars(cutoff):
            if ordinary_ashare(symbol):
                seen.add(symbol)
                save(symbol, raw)
        for symbol in sorted(set(histories) - seen):
            if ordinary_ashare(symbol):
                save(symbol, [])
        if source_revision(market_database) != revision:
            raise ValueError("market source changed during batch; transaction rolled back")
        receipt = dict(
            status="SUCCESS",
            as_of_date=cutoff,
            version=POLICY_VERSION,
            source_revision=revision,
            counts=dict(counts),
            coverage_start=dates[0],
            origin="RECONSTRUCTED",
            note="Historical reconstruction; no complete PIT certification. ST coverage requires source rows or successful daily collection.",
            finished_at=datetime.now().astimezone().isoformat(),
        )
        dest.execute(
            "INSERT INTO climax_run VALUES (?,?,?)",
            (POLICY_VERSION, cutoff, json.dumps(receipt, ensure_ascii=False)),
        )
    return receipt


def chart_climax(
    path: Path, symbol: str, cutoff: str, bars: list[dict[str, Any]]
) -> dict[str, Any]:
    empty: dict[str, Any] = dict(
        status="NOT_COMPUTED", version=POLICY_VERSION, events=[], current=None
    )
    if not ready(path):
        return empty
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as c:
        row = c.execute(
            "SELECT as_of_date,document FROM climax_stock_document WHERE policy_version=? AND symbol=?",
            (POLICY_VERSION, symbol.upper()),
        ).fetchone()
    if row is None:
        return empty
    document = _unpack(row[1])
    daily = [f for f in document["daily"] if f["date"] <= cutoff]
    if not daily:
        return {**empty, "status": "OUTSIDE_COVERAGE"}
    visible = {b["trade_date"]: b for b in bars}
    anchor = next(
        (f for f in reversed(daily) if f["date"] in visible and f["metrics"].get("close")), None
    )
    scale = visible[anchor["date"]]["close"] / anchor["metrics"]["close"] if anchor else None
    events = []
    for event in document["events"]:
        if event["date"] > cutoff:
            continue
        timeline = [dict(p) for p in event["timeline"] if p["date"] <= cutoff]
        if not any(p["date"] in visible for p in timeline):
            continue
        evidence = dict(event["evidence"])
        for key in ("atr", "close"):
            evidence[key] = evidence[key] * scale if scale else None
        for point in timeline:
            for key in ("close", "reference"):
                if point.get(key) is not None:
                    point[key] = point[key] * scale if scale else None
        events.append(
            dict(
                id=event["id"],
                date=event["date"],
                category=event["category"],
                evidence=evidence,
                checks=event["checks"],
                timeline=timeline,
            )
        )
    current = dict(daily[-1])
    current["metrics"] = dict(current["metrics"])
    for key in ("atr", "close"):
        if key in current["metrics"]:
            current["metrics"][key] = current["metrics"][key] * scale if scale else None
    return dict(
        status="OK" if row[0] >= cutoff else "STALE",
        version=POLICY_VERSION,
        events=events,
        current=current,
        computed_through=row[0],
        coverage_start=document["daily"][0]["date"],
        origin="历史重建",
        scale_available=scale is not None,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="扩张图层全量计算／显式迁移（后台维护）")
    parser.add_argument("--market-database", type=Path, default=Path("data/market.sqlite3"))
    parser.add_argument(
        "--watchlist-database", type=Path, default=Path("data/master_watchlist.sqlite3")
    )
    parser.add_argument("--date", default="")
    parser.add_argument("--migrate", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.apply:
        parser.error("--apply is required; migration backs up an existing database first")
    result = (
        migrate(args.watchlist_database)
        if args.migrate
        else run_climax(args.market_database, args.watchlist_database, args.date)
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
