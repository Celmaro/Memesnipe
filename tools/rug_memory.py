"""Rug memory — collect bad deployers and rugged mints into screening stores.

Two feeds, one SQLite each (reusing the repo's own stores):

1. Creator PnL ledger (src/screening/creator_pnl_store.py):
   joins shadow-ledger sim_close rows with detect rows (mint -> creator)
   and records each closed trade's PnL against the deployer wallet.
   The bot's `creator_pnl_blacklist` filter then skips creators with a
   net-negative track record (>=3 trades, net <= 0 SOL).

2. Rugged-mint memory (state/rug_memory.db):
   mints whose post-graduation 30-min path died (<0.5x, from
   tools/graduation_monitor.py) or whose curve collapsed hard, keyed by
   BOTH mint and creator (the feed carries poolCreatedBy on create/migrate
   events; graduation_monitor logs pool + symbol; creator comes from the
   sidecar ingester's create events). Deployers with >= N rugged mints
   land in the same creator skip-list, permanently.

Run continuously (cron/loop) or on demand:
    uv run tools/rug_memory.py --ingest-shadow   # close PnL -> creator ledger
    uv run tools/rug_memory.py --ingest-grads    # grad nukes -> rug memory
    uv run tools/rug_memory.py --stats           # report
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from screening.creator_pnl_store import CreatorPnlStore  # noqa: E402

SHADOW_DIR = PROJECT_ROOT / "shadow_data"
GRADS_DIR = PROJECT_ROOT / "state" / "pumpapi_grads"
RUG_DB = PROJECT_ROOT / "state" / "rug_memory.db"

RUG_TRADES_THRESHOLD = 3  # deployers with >= this many rugged mints -> creator blacklist
RUG_FINAL_MULTIPLE = 0.5  # grad result below this = rug
RUGGED_CREATOR_MARK = "RUGGED_DEPLOYER"


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


def ingest_shadow(ledger_glob: str = "2026-*.jsonl") -> int:
    """Feed every sim_close PnL into the creator PnL ledger (idempotent)."""
    store = CreatorPnlStore(PROJECT_ROOT / "state" / "creator_pnl.db")
    seen = _seen_marks("shadow")
    n = 0
    for path in sorted(SHADOW_DIR.glob(ledger_glob)):
        m2c: dict[str, str] = {}
        closes: list[dict] = []
        for line in path.open():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue  # torn concurrent write
            if r.get("type") == "detect" and r.get("creator"):
                m2c[r["mint"]] = r["creator"]
            elif r.get("type") == "sim_close":
                closes.append(r)
        for c in closes:
            creator = m2c.get(c["mint"])
            if not creator:
                continue
            mark = f"shadow:{c['ts']}:{c['mint']}"
            if mark in seen:
                continue
            # bot's realized PnL on 0.01 SOL sims, scaled to SOL
            # R5-P0-3: net of modeled fees — gross shadow pnl overstates the
            # creator's true result; blacklist evidence is cost-adjusted with
            # the same model the strategy ledger uses for ranking.
            pnl_sol = (c.get("pnl_quote") or 0.0)
            try:
                from research.cost_model import net_round_trip

                pnl_sol = net_round_trip(pnl_sol / 0.01, 0.01) * 0.01
            except Exception:
                pass  # cost model unavailable: keep gross (research tooling)
            store.record_trade(creator, pnl_sol)
            _mark_seen("shadow", mark)
            n += 1
    print(f"[rug] creator ledger: {n} new closed trades recorded")
    return n


def ingest_grads() -> int:
    """Mints that died post-graduation -> rug memory (mint + deployer)."""
    conn = sqlite3.connect(RUG_DB, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rugs (
            mint TEXT PRIMARY KEY,
            creator TEXT,
            symbol TEXT,
            pool TEXT,
            mc_at_grad REAL,
            final_multiple REAL,
            detected_at REAL
        )
        """
    )
    seen = _seen_marks("rug")
    n = 0
    for path in sorted(GRADS_DIR.glob("*.jsonl")):
        for line in path.open():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("type") != "grad_result":
                continue
            final = r.get("last_multiple") or r.get("final_multiple")
            if final is None or final >= RUG_FINAL_MULTIPLE:
                continue
            mark = f"grad:{r.get('mint')}"
            if mark in seen:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO rugs VALUES (?,?,?,?,?,?,?)",
                (r.get("mint"), r.get("creator"), r.get("symbol"), r.get("pool"),
                 r.get("mc_at_grad"), final, _now()),
            )
            conn.commit()
            _mark_seen("rug", mark)
            n += 1
    # deployers with enough rugs -> permanent creator skip via PnL ledger mark
    rows = conn.execute(
        "SELECT creator, COUNT(*) c FROM rugs WHERE creator IS NOT NULL GROUP BY creator HAVING c >= ?",
        (RUG_TRADES_THRESHOLD,),
    ).fetchall()
    store = CreatorPnlStore(PROJECT_ROOT / "state" / "creator_pnl.db")
    for creator, cnt in rows:
        stats = store.get_stats(creator)
        # push ledger deep negative so is_blacklisted() fires immediately
        store.record_trade(creator, -1.0 * (RUG_TRADES_THRESHOLD + 1))
        print(f"[rug] deployer {creator[:8]}… flagged: {cnt} rugged mints")
    conn.close()
    print(f"[rug] rug memory: {n} new rugs recorded")
    return n


def _seen_db() -> sqlite3.Connection:
    conn = sqlite3.connect(RUG_DB, timeout=30)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS seen_marks (kind TEXT, mark TEXT, PRIMARY KEY(kind, mark))"
    )
    return conn


def _seen_marks(kind: str) -> set[str]:
    conn = _seen_db()
    rows = conn.execute("SELECT mark FROM seen_marks WHERE kind = ?", (kind,)).fetchall()
    conn.close()
    return {r[0] for r in rows}


def _mark_seen(kind: str, mark: str):
    conn = _seen_db()
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("INSERT OR IGNORE INTO seen_marks VALUES (?,?)", (kind, mark))
    conn.commit()
    conn.close()


def stats() -> int:
    conn = sqlite3.connect(RUG_DB)
    rugs = conn.execute("SELECT COUNT(*), COUNT(DISTINCT creator) FROM rugs").fetchone()
    print(f"rug memory: {rugs[0]} rugged mints from {rugs[1]} deployers")
    for row in conn.execute(
        "SELECT creator, symbol, final_multiple FROM rugs ORDER BY final_multiple LIMIT 5"
    ):
        print(f"  worst: {row[1] or '?'} {row[0] and row[0][:8]}… {row[2]}x")
    conn.close()
    store = CreatorPnlStore(PROJECT_ROOT / "state" / "creator_pnl.db")
    with store._connect() as c:  # noqa: SLF001 - report-only
        rows = c.execute("SELECT COUNT(*), SUM(trades) FROM creator_pnl").fetchone()
        blacklisted = c.execute(
            "SELECT COUNT(*) FROM creator_pnl WHERE trades >= 3 AND net_pnl_sol <= 0"
        ).fetchone()[0]
    print(f"creator ledger: {rows[0]} creators, {rows[1]} trades, {blacklisted} blacklisted")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ingest-shadow", action="store_true")
    ap.add_argument("--ingest-grads", action="store_true")
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args()
    if args.ingest_shadow:
        ingest_shadow()
    if args.ingest_grads:
        ingest_grads()
    if args.stats or not (args.ingest_shadow or args.ingest_grads):
        stats()
    return 0


if __name__ == "__main__":
    sys.exit(main())
