"""Creator PnL store (P2-STRUC-2): persistent creator-outcome ledger.

Port of the reference bot's trade_logger.rs:32-88 idea — append every trade
outcome and let creators whose net history is negative accumulate into a
skip list. Memesnipe keeps the ledger in SQLite (state/creator_pnl.db by
default) so the blacklist survives restarts.

Disabled by default: the store is only touched when
``filters.creator_pnl_blacklist`` is true, so the off path is byte-identical
to prior behavior.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CreatorPnlStore:
    """SQLite persistence for per-creator trade outcomes.

    Args:
        db_path: SQLite file path. Created with the schema on first use.
    """

    db_path: Path | str

    def __post_init__(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS creator_pnl (
                    creator TEXT PRIMARY KEY,
                    trades INTEGER NOT NULL DEFAULT 0,
                    wins INTEGER NOT NULL DEFAULT 0,
                    net_pnl_sol REAL NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL DEFAULT 0
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        # A2-P1-1: WAL + NORMAL so the snipe hot path never blocks on a
        # concurrent writer (migrate_watcher / shadow_capture / this store).
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def record_trade(self, creator: str, pnl_sol: float) -> None:
        """Append one realized trade outcome for a creator.

        Args:
            creator: Creator wallet pubkey string
            pnl_sol: Realized PnL in SOL for this trade (negative = loss)
        """
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO creator_pnl (creator, trades, wins, net_pnl_sol, updated_at)
                VALUES (?, 1, ?, ?, ?)
                ON CONFLICT(creator) DO UPDATE SET
                    trades = trades + 1,
                    wins = wins + excluded.wins,
                    net_pnl_sol = net_pnl_sol + excluded.net_pnl_sol,
                    updated_at = excluded.updated_at
                """,
                (
                    creator,
                    1 if pnl_sol > 0 else 0,
                    pnl_sol,
                    sqlite3_time(),
                ),
            )

    def get_stats(self, creator: str) -> tuple[int, float]:
        """(trades, net_pnl_sol) for a creator; (0, 0.0) when unseen."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT trades, net_pnl_sol FROM creator_pnl WHERE creator = ?",
                (creator,),
            ).fetchone()
        if row is None:
            return (0, 0.0)
        return (row["trades"], row["net_pnl_sol"])

    def is_blacklisted(
        self,
        creator: str,
        min_trades: int = 10,
        max_net_sol: float = 0.0,
    ) -> bool:
        """True when a creator's history is net-negative enough to skip.

        A creator needs both a track record (>= min_trades) AND a net PnL at
        or below max_net_sol (default 0.0 = net non-positive) before being
        blacklisted, so a single bad trade never bans a creator.
        """
        trades, net = self.get_stats(creator)
        return trades >= min_trades and net <= max_net_sol


def sqlite3_time() -> float:
    """Monotonic-ish wall time for the ledger. Import-time safe."""
    import time

    return time.time()