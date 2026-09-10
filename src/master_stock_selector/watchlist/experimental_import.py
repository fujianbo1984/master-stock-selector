"""Build an independent website snapshot from the audited research feature panel.

Only the import command needs NumPy. The website uses the standard library.
Existing databases are never overwritten; build a new snapshot to update it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

from .experimental import KEYS, MODELS, POLICY, SELECTED, select_rows

SCHEMA = """
CREATE TABLE metadata (payload TEXT NOT NULL);
CREATE TABLE observation_days (date TEXT PRIMARY KEY, pool_count INTEGER NOT NULL);
CREATE TABLE observations (
 date TEXT NOT NULL, model TEXT NOT NULL, symbol TEXT NOT NULL, name TEXT NOT NULL,
 rank INTEGER NOT NULL, score REAL NOT NULL,
 turnover REAL NOT NULL, volume_dry REAL NOT NULL, up_down_amount REAL NOT NULL,
 rs20 REAL NOT NULL, limit60 REAL NOT NULL, range20 REAL NOT NULL,
 first_seen TEXT NOT NULL, episode_start TEXT NOT NULL, new_episode INTEGER NOT NULL,
 PRIMARY KEY(date, model, symbol), UNIQUE(date, model, rank)
);
"""


def import_panel(panel: Path, lock_path: Path, audit_path: Path, output: Path) -> dict:
    import numpy as np

    if output.exists():
        raise FileExistsError("Refusing to overwrite an existing database; choose a new output path")
    lock = json.loads(lock_path.read_text())
    audit = json.loads(audit_path.read_text())
    if lock.get("selected_grid_id") != 190 or lock.get("parameters") != dict(zip(KEYS, SELECTED)):
        raise ValueError("The parameter lock does not match frozen group 190")
    with panel.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    if digest != lock.get("source_panel_sha256") or digest != audit.get("panel_sha256"):
        raise ValueError("Feature panel fingerprint does not match the lock and source audit")
    # Deliberately do not load label50, new5board, score, or eval_year.
    with np.load(panel, allow_pickle=False) as source:
        arrays = {key: source[key] for key in ("F", "si", "ti", "dates", "symbols", "names")}
    F, si, ti = (arrays[key] for key in ("F", "si", "ti"))
    dates = [str(value) for value in arrays["dates"]]
    symbols = [str(value) for value in arrays["symbols"]]
    names = [str(value) for value in arrays["names"]]
    if (not dates or dates != sorted(set(dates)) or len(symbols) != len(set(symbols))
            or len(names) != len(symbols) or F.shape != (len(si), 6) or ti.shape != si.shape):
        raise ValueError("Invalid feature panel dimensions or calendar")
    for value in dates:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError("Invalid calendar date")
    if any(not re.fullmatch(r"\d{6}\.(SH|SZ)", symbol) for symbol in symbols):
        raise ValueError("Expected Shanghai/Shenzhen stock symbols")
    if (not np.issubdtype(si.dtype, np.integer) or not np.issubdtype(ti.dtype, np.integer)
            or np.any(si < 0) or np.any(si >= len(symbols))
            or np.any(ti < 0) or np.any(ti >= len(dates))):
        raise ValueError("Invalid stock or date indices")
    if len(si) != audit.get("rows") or dates[-1] != audit.get("calendar_end"):
        raise ValueError("Panel coverage differs from source audit")
    metadata = dict(policy=POLICY, source_end=dates[-1], source_start=dates[0],
                    origin="RECONSTRUCTED", source_panel_sha256=digest,
                    imported_at=datetime.now(timezone.utc).isoformat(),
                    feature_rows=len(si), parameters=dict(zip(KEYS, SELECTED)),
                    name_basis="研究面板名称，仅作识别，不保证历史时点名称",
                    purpose="OBSERVATION_ONLY", source_quality=audit.get("quality", {}))
    output.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".experimental-", suffix=".sqlite3", dir=output.parent)
    os.close(handle)
    total = 0
    try:
        with sqlite3.connect(temporary) as con:
            con.executescript(SCHEMA)
            order = np.argsort(ti, kind="stable")
            sorted_ti = ti[order]
            starts = np.searchsorted(sorted_ti, np.arange(len(dates) + 1))
            first_seen: dict[tuple[str, str], str] = {}
            episodes: dict[tuple[str, str], int] = {}
            for t, day in enumerate(dates):
                # Retain calendar sessions with zero eligible stocks; no future-label censoring.
                ids = order[starts[t]:starts[t + 1]]
                con.execute("INSERT INTO observation_days VALUES (?, ?)", (day, len(ids)))
                rows = [dict(symbol=symbols[int(si[i])], name=names[int(si[i])], eligible=True,
                             **dict(zip(KEYS, map(float, F[i])))) for i in ids]
                for model in MODELS:
                    for row in select_rows(rows, model):
                        key = (model, row["symbol"])
                        first_seen.setdefault(key, day)
                        new_episode = key not in episodes or t > episodes[key] + 20
                        if new_episode:
                            episodes[key] = t
                        con.execute("INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                    (day, model, row["symbol"], row["name"], row["rank"], row["score"],
                                     *(row[k] for k in KEYS), first_seen[key], dates[episodes[key]],
                                     int(new_episode)))
                        total += 1
            metadata["observation_rows"] = total
            con.execute("INSERT INTO metadata VALUES (?)", (json.dumps(metadata, ensure_ascii=False),))
            con.commit()
            if con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("Snapshot integrity check failed")
        # Hard link publishes the completed snapshot atomically and fails if output appeared meanwhile.
        os.link(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description="计算实验观察名单并写入独立网站数据文件，不覆盖已有数据库")
    parser.add_argument("--panel", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = import_panel(args.panel, args.lock, args.audit, args.output)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
