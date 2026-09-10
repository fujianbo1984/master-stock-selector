"""Daily frozen experiment: read authoritative prices; persist only independent experiment data."""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import os
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from .experimental import KEYS, MODELS, POLICY, select_rows
from .experimental_import import SCHEMA
from .market_provider import TushareMarketProvider


class SupplementProvider(Protocol):
    def assert_ready(self) -> None: ...
    def trade_calendar(self, start_date: str, end_date: str) -> list[str]: ...
    def stock_limits(self, trade_date: str) -> dict[str, dict[str, Any]]: ...
    def stock_suspensions(self, trade_date: str) -> list[dict[str, Any]]: ...
    def stock_st(self, trade_date: str) -> list[dict[str, Any]]: ...


DAILY_SCHEMA = """
CREATE TABLE IF NOT EXISTS experiment_sessions (date TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS experiment_limits (
 date TEXT NOT NULL, symbol TEXT NOT NULL, up REAL NOT NULL, down REAL NOT NULL,
 PRIMARY KEY(date,symbol));
CREATE TABLE IF NOT EXISTS experiment_suspended (
 date TEXT NOT NULL, symbol TEXT NOT NULL, PRIMARY KEY(date,symbol));
CREATE TABLE IF NOT EXISTS experiment_st (
 date TEXT NOT NULL, symbol TEXT NOT NULL, PRIMARY KEY(date,symbol));
CREATE TABLE IF NOT EXISTS experiment_inputs (
 date TEXT NOT NULL, dataset TEXT NOT NULL, digest TEXT NOT NULL, fetched_at TEXT NOT NULL,
 PRIMARY KEY(date,dataset));
CREATE TABLE IF NOT EXISTS experiment_daily_runs (
 date TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT NOT NULL);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def initialize(path: Path) -> None:
    """Explicit migration, before the timer is enabled; existing snapshots require a backup."""
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    if existed:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as source:
            metadata = json.loads(source.execute("SELECT payload FROM metadata").fetchone()[0])
            if metadata.get("policy") != POLICY:
                raise ValueError("实验规则版本不兼容")
            backup = path.with_name(
                path.name + ".before-daily-" + datetime.now().strftime("%Y%m%d%H%M%S")
            )
            if backup.exists():
                raise FileExistsError(backup)
            with closing(sqlite3.connect(backup)) as target:
                source.backup(target)
    with closing(sqlite3.connect(path)) as con:
        if not existed:
            con.executescript(SCHEMA)
            con.execute(
                "INSERT INTO metadata VALUES (?)",
                (
                    json.dumps(
                        dict(
                            policy=POLICY,
                            origin="DAILY",
                            purpose="OBSERVATION_ONLY",
                            source_end="",
                            name_basis="当日证券主数据",
                            imported_at=_now(),
                        ),
                        ensure_ascii=False,
                    ),
                ),
            )
        con.executescript(DAILY_SCHEMA)
        con.commit()


def _number(value: Any) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else math.nan
    except (ValueError, TypeError):
        return math.nan


def calculate_features(
    bars: list[dict[str, Any]],
    index: list[float],
    *,
    mature: bool,
    is_st: bool,
) -> tuple[dict[str, Any] | None, str]:
    """Exactly 61 aligned market sessions; original-price × daily factor ratios, no future bars."""
    if len(bars) != 61 or len(index) != 61:
        return None, "history"
    if not mature or is_st:
        return None, "qualification"
    valid = [
        all(
            math.isfinite(_number(b.get(k))) and _number(b.get(k)) > 0
            for k in ("open", "high", "low", "close", "volume", "af")
        )
        and not b.get("suspended", False)
        for b in bars
    ]
    if not all(valid[-60:]):
        return None, "history"
    # Missing actual limits remain unknown, never inferred using a uniform price-change threshold.
    if any(
        not all(math.isfinite(_number(b.get(k))) and _number(b.get(k)) > 0 for k in ("up", "down"))
        for b in bars
    ):
        return None, "limits"
    amounts = [_number(b.get("amount")) for b in bars[-20:]]
    if any(not math.isfinite(v) or v < 0 for v in amounts):
        return None, "amount"
    if bars[-1]["close"] < 3 or amounts[-1] < 50000 or sum(amounts) / 20 < 50000:
        return None, "liquidity"
    close = [_number(b.get("close")) * _number(b.get("af")) for b in bars]
    upclose = [
        abs(_number(b.get("close")) - _number(b.get("up"))) <= 0.005 and v
        for b, v in zip(bars, valid)
    ]
    if any(upclose[-10:]) or close[-1] / close[-11] - 1 > 0.20:
        return None, "recent_acceleration"
    if not all(math.isfinite(index[i]) and index[i] > 0 for i in (-1, -21)):
        return None, "benchmark"
    up_amount = down_amount = 0.0
    for i in range(41, 61):
        if close[i] > close[i - 1]:
            up_amount += amounts[i - 41]
        elif close[i] < close[i - 1]:
            down_amount += amounts[i - 41]
    if down_amount <= 0:
        return None, "down_amount"
    volumes = [float(b["volume"]) for b in bars[-20:]]
    result = dict(
        turnover=_number(bars[-1].get("turnover")),
        volume_dry=(sum(volumes[-5:]) / 5) / (sum(volumes) / 20),
        up_down_amount=up_amount / down_amount,
        rs20=(close[-1] / close[-21]) / (index[-1] / index[-21]) - 1,
        limit60=float(sum(upclose[:-1])),
        range20=max(b["high"] * b["af"] for b in bars[-20:])
        / min(b["low"] * b["af"] for b in bars[-20:])
        - 1,
    )
    if not all(math.isfinite(value) for value in result.values()):
        return None, "features"
    if result["turnover"] > 20 or result["range20"] > 0.80:
        return None, "observation_bounds"
    return result, "eligible"


def _validate_rows(rows: list[dict[str, Any]], day: str) -> None:
    if any(str(r.get("trade_date", "")).replace("-", "") != day.replace("-", "") for r in rows):
        raise ValueError("补充数据返回了不同交易日，拒绝拼接")


def collect_inputs(
    con: sqlite3.Connection,
    days: list[str],
    target: str,
    provider: SupplementProvider,
    minimum: int,
) -> None:
    for day in days:
        if con.execute(
            "SELECT 1 FROM experiment_inputs WHERE date=? AND dataset='limits'", (day,)
        ).fetchone():
            continue
        limits = provider.stock_limits(day)
        _validate_rows(list(limits.values()), day)
        limits = {s: r for s, r in limits.items() if s.endswith((".SH", ".SZ"))}
        if len(limits) < minimum:
            raise ValueError(f"{day} 实际涨跌停价覆盖不足：{len(limits)}")
        rows = [
            (day, s, _number(r.get("up_limit")), _number(r.get("down_limit")))
            for s, r in sorted(limits.items())
        ]
        rows = [r for r in rows if all(math.isfinite(v) and v > 0 for v in r[2:])]
        if len(rows) < minimum:
            raise ValueError(f"{day} 有效涨跌停价覆盖不足")
        suspensions = provider.stock_suspensions(day)
        _validate_rows(suspensions, day)
        suspended = sorted(
            {
                str(r["ts_code"])
                for r in suspensions
                if r.get("suspend_type") == "S"
                and (
                    not r.get("suspend_timing")
                    or any(
                        part.split("-")[0].strip() <= "09:30"
                        for part in str(r["suspend_timing"]).split(",")
                    )
                )
            }
        )
        with con:
            con.executemany("INSERT INTO experiment_limits VALUES (?,?,?,?)", rows)
            con.executemany(
                "INSERT INTO experiment_suspended VALUES (?,?)", [(day, s) for s in suspended]
            )
            con.execute(
                "INSERT INTO experiment_inputs VALUES (?,?,?,?)",
                (day, "limits", _digest([rows, suspended]), _now()),
            )
    if not con.execute(
        "SELECT 1 FROM experiment_inputs WHERE date=? AND dataset='st'", (target,)
    ).fetchone():
        st = provider.stock_st(target)
        _validate_rows(st, target)
        # The research contract only considers ST coverage known on nonempty response days.
        if not st:
            raise ValueError("ST返回空集合，无法证明当日状态覆盖")
        with con:
            con.executemany(
                "INSERT OR IGNORE INTO experiment_st VALUES (?,?)",
                [(target, str(r["ts_code"])) for r in st],
            )
            con.execute(
                "INSERT INTO experiment_inputs VALUES (?,?,?,?)",
                (target, "st", _digest(st), _now()),
            )


def load_market(
    market: Path, days: list[str], target: str
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]], list[float]]:
    with closing(sqlite3.connect(market.resolve().as_uri() + "?mode=ro", uri=True)) as con:
        con.row_factory = sqlite3.Row
        prices = con.execute(
            """
            SELECT b.symbol,b.trade_date,b.open,b.high,b.low,b.close,b.volume,b.amount,
                   a.adj_factor AS af,m.turnover_rate AS turnover
            FROM daily_bars b LEFT JOIN daily_adjustment_factors a
              ON a.market=b.market AND a.symbol=b.symbol AND a.trade_date=b.trade_date
            LEFT JOIN daily_metrics m ON m.market=b.market AND m.symbol=b.symbol AND m.trade_date=b.trade_date
            WHERE b.market='ashare' AND b.adj_type='raw' AND b.trade_date BETWEEN ? AND ?
            ORDER BY b.symbol,b.trade_date
        """,
            (days[0], target),
        ).fetchall()
        identities = {
            r["symbol"]: dict(r)
            for r in con.execute(
                """
            SELECT s.* FROM security_master_history s WHERE s.valid_from=(
              SELECT max(t.valid_from) FROM security_master_history t
              WHERE t.symbol=s.symbol AND t.valid_from<=?)
        """,
                (target,),
            )
        }
        index: dict[str, float] = {}
        for r in con.execute(
            """SELECT trade_date,close FROM market_index_daily_bars
              WHERE index_symbol='000985.CSI' AND trade_date BETWEEN ? AND ? ORDER BY fetched_at,batch_id""",
            (days[0], target),
        ):
            index[r["trade_date"]] = _number(r["close"])
    grouped: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for r in prices:
        grouped[r["symbol"]][r["trade_date"]] = dict(r)
    return (
        {s: [values.get(d, {}) for d in days] for s, values in grouped.items()},
        identities,
        [index.get(d, math.nan) for d in days],
    )


def run_experiment(
    market: Path,
    output: Path,
    target: str,
    *,
    provider: SupplementProvider | None = None,
    minimum: int = 4500,
) -> dict[str, Any]:
    day = date.fromisoformat(target)
    if day > date.today():
        raise ValueError("实验筛选不计算未来交易日")
    if not output.is_file():
        raise ValueError("实验日常库尚未初始化；先运行 experimental_daily --migrate --apply")
    active = provider or TushareMarketProvider()
    active.assert_ready()
    with closing(sqlite3.connect(output, timeout=30)) as con:
        con.row_factory = sqlite3.Row
        # Strictly serialize writers, including one-off backfills and the scheduled run.
        import fcntl

        lock_path = output.with_name(output.name + ".lock")
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                return _run(con, market, target, active, minimum)
            except Exception as exc:
                con.rollback()
                # A failed day has no observation-day marker and can be retried.
                with con:
                    con.execute(
                        "INSERT OR REPLACE INTO experiment_daily_runs VALUES (?,?,?)",
                        (
                            target,
                            "FAILED",
                            json.dumps(
                                dict(error=str(exc), finished_at=_now()), ensure_ascii=False
                            ),
                        ),
                    )
                raise


def _run(
    con: sqlite3.Connection, market: Path, target: str, provider: SupplementProvider, minimum: int
) -> dict[str, Any]:
    existing = con.execute(
        "SELECT status,payload FROM experiment_daily_runs WHERE date=?", (target,)
    ).fetchone()
    if existing and existing["status"] == "SUCCESS":
        return {**json.loads(existing["payload"]), "state": "REUSED"}
    latest = con.execute("SELECT max(date) FROM observation_days").fetchone()[0]
    if latest and target <= latest:
        raise ValueError("拒绝改写既有观察历史；请使用独立新库验证")
    start = (date.fromisoformat(target) - timedelta(days=400)).isoformat()
    calendar = provider.trade_calendar(start, target)
    if target not in calendar:
        return dict(status="SKIPPED", trade_date=target, state="NON_TRADING_DAY")
    if len(calendar) < 121 or calendar != sorted(set(calendar)):
        raise ValueError("交易日历不足或无序，无法核验上市120日")
    if latest and latest < calendar[0]:
        raise ValueError("观察历史间隔超过日历覆盖；需要显式按日期补算")
    if latest:
        for pending in (d for d in calendar if latest < d < target):
            _run(con, market, pending, provider, minimum)
    days = calendar[-61:]
    bars, identities, index = load_market(market, days, target)
    coverage = [sum(bool(values[i]) for values in bars.values()) for i in range(61)]
    if min(coverage) < minimum:
        raise ValueError(f"原价历史不足61个交易日；最低当日覆盖 {min(coverage)}，要求 {minimum}")
    factor_coverage = [
        sum(
            math.isfinite(_number(values[i].get("af"))) and _number(values[i].get("af")) > 0
            for values in bars.values()
        )
        for i in range(61)
    ]
    if any(n < minimum or n < total * 0.95 for n, total in zip(factor_coverage, coverage)):
        raise ValueError("复权因子覆盖不足，拒绝将数据缺失解释为空名单")
    metrics_count = sum(
        math.isfinite(_number(values[-1].get("turnover"))) for values in bars.values()
    )
    if metrics_count < minimum or metrics_count < coverage[-1] * 0.95:
        raise ValueError("当日换手率覆盖不足")
    identity_count = sum(bool(identities.get(s, {}).get("list_date")) for s in bars)
    if identity_count < minimum:
        raise ValueError("当时上市日期覆盖不足")
    if any(not math.isfinite(value) or value <= 0 for value in index):
        raise ValueError("中证全指历史不完整，拒绝跨日期或基准替代")
    collect_inputs(con, days, target, provider, minimum)
    limits = {
        (r["symbol"], r["date"]): (r["up"], r["down"])
        for r in con.execute(
            "SELECT * FROM experiment_limits WHERE date BETWEEN ? AND ?", (days[0], target)
        )
    }
    suspended = {
        (r["symbol"], r["date"])
        for r in con.execute(
            "SELECT * FROM experiment_suspended WHERE date BETWEEN ? AND ?", (days[0], target)
        )
    }
    st = {r[0] for r in con.execute("SELECT symbol FROM experiment_st WHERE date=?", (target,))}
    rows: list[dict[str, Any]] = []
    reasons: Counter[str] = Counter()
    for symbol, values in bars.items():
        identity = identities.get(symbol, {})
        listing = str(identity.get("list_date") or "")
        mature = bool(listing and bisect.bisect_left(calendar, listing) <= len(calendar) - 121)
        for d, value in zip(days, values):
            up, down = limits.get((symbol, d), (math.nan, math.nan))
            value.update(up=up, down=down, suspended=(symbol, d) in suspended)
        feature, reason = calculate_features(values, index, mature=mature, is_st=symbol in st)
        reasons[reason] += 1
        if feature is not None:
            rows.append(
                dict(symbol=symbol, name=identity.get("name", ""), eligible=True, **feature)
            )
    counts: dict[str, int] = {}
    with con:
        con.executemany(
            "INSERT OR IGNORE INTO experiment_sessions VALUES (?)", [(d,) for d in calendar]
        )
        con.execute("INSERT INTO observation_days VALUES (?,?)", (target, len(rows)))
        for model in MODELS:
            selected = select_rows(rows, model)
            counts[model] = len(selected)
            for row in selected:
                previous = con.execute(
                    "SELECT first_seen,episode_start FROM observations WHERE model=? AND symbol=? ORDER BY date DESC LIMIT 1",
                    (model, row["symbol"]),
                ).fetchone()
                first = previous["first_seen"] if previous else target
                episode = previous["episode_start"] if previous else target
                elapsed = con.execute(
                    "SELECT count(*) FROM experiment_sessions WHERE date>? AND date<=?",
                    (episode, target),
                ).fetchone()[0]
                new = previous is None or elapsed > 20
                if new:
                    episode = target
                con.execute(
                    "INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        target,
                        model,
                        row["symbol"],
                        row["name"],
                        row["rank"],
                        row["score"],
                        *(row[k] for k in KEYS),
                        first,
                        episode,
                        int(new),
                    ),
                )
        inputs = [
            tuple(r)
            for r in con.execute(
                "SELECT date,dataset,digest FROM experiment_inputs WHERE date BETWEEN ? AND ? ORDER BY date,dataset",
                (days[0], target),
            )
        ]
        payload = dict(
            status="SUCCESS",
            state="CALCULATED",
            trade_date=target,
            counts=counts,
            pool_count=len(rows),
            excluded=dict(reasons),
            market_window=days,
            market_coverage_min=min(coverage),
            input_digest=_digest(inputs),
            feature_digest=_digest(rows),
            origin="OBSERVED" if target == date.today().isoformat() else "RECONSTRUCTED",
            finished_at=_now(),
            policy=POLICY,
        )
        con.execute(
            "INSERT OR REPLACE INTO experiment_daily_runs VALUES (?,?,?)",
            (target, "SUCCESS", json.dumps(payload, ensure_ascii=False)),
        )
    return payload


def main() -> None:
    from ..cli import load_local_env

    load_local_env()
    p = argparse.ArgumentParser(description="根据服务器权威行情每日计算独立实验观察池")
    p.add_argument(
        "--market-database",
        type=Path,
        default=Path(os.environ.get("MASTERSTOCK_MARKET_DATABASE", "data/market.sqlite3")),
    )
    p.add_argument("--database", type=Path, default=None)
    p.add_argument("--date", default=date.today().isoformat())
    p.add_argument("--migrate", action="store_true")
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    output = args.database or Path(
        os.environ.get("MASTERSTOCK_EXPERIMENT_DATABASE")
        or args.market_database.with_name("experimental_watchlist.sqlite3")
    )
    if not args.apply:
        p.error("写入独立实验库需要 --apply")
    if args.migrate:
        initialize(output)
        print(json.dumps(dict(status="INITIALIZED", database=str(output))))
    else:
        print(
            json.dumps(run_experiment(args.market_database, output, args.date), ensure_ascii=False)
        )


if __name__ == "__main__":
    main()
