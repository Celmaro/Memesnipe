"""Fold the strategies ledger (shadow_data/strategies/*.jsonl) into an indexed
SQLite (state/strategies.db) at analysis time and print per-strategy stats.

Read-only on the ledger; owns only the DB. Safe to run while the bot writes
(new rows picked up on the next run; idempotent by (mint, strategy, ts)).

Usage:
    uv run python tools/strategies_report.py            # ingest + report
    uv run python tools/strategies_report.py --no-report  # ingest only
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LEDGER_DIR = PROJECT_ROOT / "shadow_data" / "strategies"
DB_PATH = PROJECT_ROOT / "state" / "strategies.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS strategy_events (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    type TEXT NOT NULL,
    mint TEXT NOT NULL,
    symbol TEXT,
    strategy TEXT,             -- NULL for strategy_open rows
    pnl_pct REAL,              -- strategy_close rows only
    sell_price REAL,
    exit_reason TEXT,
    entry_price REAL,
    quote_spent REAL,
    raw JSON NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_se_type ON strategy_events(type);
CREATE INDEX IF NOT EXISTS idx_se_mint ON strategy_events(mint);
CREATE INDEX IF NOT EXISTS idx_se_strategy ON strategy_events(strategy, type);
CREATE UNIQUE INDEX IF NOT EXISTS idx_se_dedupe
    ON strategy_events(ts, mint, COALESCE(strategy, ""), type);
"""


def ingest(conn: sqlite3.Connection) -> int:
    """Append-only ingest; returns rows added. Skips lines already in the DB."""
    conn.executescript(SCHEMA)
    known = {
        (ts, mint, strat or "", typ)
        for (ts, mint, strat, typ) in conn.execute(
            "SELECT ts, mint, strategy, type FROM strategy_events"
        )
    }
    added = 0
    for path in sorted(LEDGER_DIR.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue  # corrupt tail from a mid-write crash — skip
            typ = r.get("type", "")
            ts = r.get("ts", "")
            mint = r.get("mint", "")
            strat = r.get("strategy")
            if (ts, mint, strat or "", typ) in known:
                continue
            conn.execute(
                "INSERT INTO strategy_events"
                " (ts, type, mint, symbol, strategy, pnl_pct, sell_price,"
                "  exit_reason, entry_price, quote_spent, raw)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ts, typ, mint, r.get("symbol"),
                    strat,
                    r.get("pnl_pct"), r.get("sell_price"), r.get("exit_reason"),
                    r.get("entry_price"), r.get("quote_spent"),
                    line,
                ),
            )
            known.add((ts, mint, strat or "", typ))
            added += 1
    conn.commit()
    return added


def report(conn: sqlite3.Connection) -> None:
    opens = conn.execute("SELECT COUNT(DISTINCT mint) FROM strategy_events WHERE type='strategy_open'").fetchone()[0]
    print(f"positions opened: {opens}")
    rows = conn.execute(
        "SELECT strategy, pnl_pct FROM strategy_events"
        " WHERE type='strategy_close' AND pnl_pct IS NOT NULL"
    ).fetchall()
    if not rows:
        print("no strategy_close rows yet — grid still accumulating")
        return
    by: dict[str, list[float]] = defaultdict(list)
    for strat, pnl in rows:
        if pnl is not None and -99 < float(pnl) < 2000:
            by[strat].append(float(pnl))
    print(f"{'strategy':<14}{'n':>5}{'wr':>6}{'med':>9}{'avg':>9}{'total':>10}")
    for strat in sorted(by, key=lambda s: -statistics.median(by[s])):
        v = by[strat]
        print(
            f"{strat:<14}{len(v):>5}"
            f"{sum(1 for x in v if x > 0) / len(v) * 100:>5.0f}%"
            f"{statistics.median(v):>+8.1f}%"
            f"{sum(v) / len(v):>+8.1f}%"
            f"{sum(v):>+9.0f}%"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-report", action="store_true", help="ingest only")
    args = ap.parse_args()
    if not LEDGER_DIR.exists():
        print(f"no ledger at {LEDGER_DIR}")
        return 1
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        added = ingest(conn)
        print(f"ingested {added} new rows -> {DB_PATH}")
        if not args.no_report:
            report(conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
