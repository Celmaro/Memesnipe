"""Copy-trading target store (T3.1): SQLite-backed per-wallet policy rows.

Modeled on open-sol-bot's CopyTrade row, with the correctness guard from its
reference bug: every query builds clauses with ``&`` / ``and_()`` (never plain
Python ``and``, which short-circuits on SQLAlchemy expressions and silently
drops the wallet filter). `verify_copy_trader_store.py` asserts the generated
SQL contains the wallet equality.

Ships disabled (no targets) so nothing copies until the user inserts rows.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CopyTarget:
    """One wallet to mirror, with per-wallet risk limits.

    Modes:
        buy_only: mirror buys, never mirror sells (accumulate the leader's
            picks without inheriting their exits)
        full: mirror buys and sells
    """

    wallet: str  # leader wallet public key (PK)
    alias: str = ""
    enabled: bool = True
    mode: str = "buy_only"
    fixed_buy_sol: float = 0.0  # 0 = size from a configured default amount
    percent_of_leader: float = 0.0  # >0 = mirror a fraction of the leader's notional
    max_position_sol: float = 0.0  # 0 = no per-token cap
    slippage_bps: int = 500  # 5% default
    priority_fee: int | None = None  # None = use the bot's fee manager
    min_notional_sol: float = 0.01  # skip leader buys smaller than this
    require_confirmation: bool = True  # wait for 1 RPC confirm before mirroring


@dataclass
class CopyTargetStore:
    """SQLite persistence for copy targets.

    Args:
        db_path: SQLite file path. Created with the schema on first use.
    """

    db_path: Path | str

    def __post_init__(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS copy_targets (
                    wallet TEXT PRIMARY KEY,
                    alias TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    mode TEXT NOT NULL DEFAULT 'buy_only'
                        CHECK (mode IN ('buy_only', 'full')),
                    fixed_buy_sol REAL NOT NULL DEFAULT 0,
                    percent_of_leader REAL NOT NULL DEFAULT 0,
                    max_position_sol REAL NOT NULL DEFAULT 0,
                    slippage_bps INTEGER NOT NULL DEFAULT 500,
                    priority_fee INTEGER,
                    min_notional_sol REAL NOT NULL DEFAULT 0.01,
                    require_confirmation INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            # Migration: P2-COP-1 added percent_of_leader. Pre-2026-10-26 DBs
            # created with the older schema lack the column; ALTER it in
            # instead of forcing a destructive re-create.
            cols = {
                r[1]
                for r in conn.execute("PRAGMA table_info(copy_targets)").fetchall()
            }
            if "percent_of_leader" not in cols:
                conn.execute(
                    "ALTER TABLE copy_targets "
                    "ADD COLUMN percent_of_leader REAL NOT NULL DEFAULT 0"
                )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def upsert(self, target: CopyTarget) -> None:
        """Insert or replace one target row."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO copy_targets (
                    wallet, alias, enabled, mode, fixed_buy_sol,
                    percent_of_leader, max_position_sol, slippage_bps,
                    priority_fee, min_notional_sol, require_confirmation
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(wallet) DO UPDATE SET
                    alias=excluded.alias,
                    enabled=excluded.enabled,
                    mode=excluded.mode,
                    fixed_buy_sol=excluded.fixed_buy_sol,
                    percent_of_leader=excluded.percent_of_leader,
                    max_position_sol=excluded.max_position_sol,
                    slippage_bps=excluded.slippage_bps,
                    priority_fee=excluded.priority_fee,
                    min_notional_sol=excluded.min_notional_sol,
                    require_confirmation=excluded.require_confirmation
                """,
                (
                    target.wallet,
                    target.alias,
                    int(target.enabled),
                    target.mode,
                    target.fixed_buy_sol,
                    target.percent_of_leader,
                    target.max_position_sol,
                    target.slippage_bps,
                    target.priority_fee,
                    target.min_notional_sol,
                    int(target.require_confirmation),
                ),
            )

    def get_active_wallets(self) -> list[CopyTarget]:
        """All enabled targets, deduplicated by wallet.

        Uses only ``&``/``and_()``-safe composition: the reference bot built
        this clause with Python ``and`` and silently returned every row.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM copy_targets WHERE enabled = 1 ORDER BY wallet"
            ).fetchall()
        return [self._row_to_target(r) for r in rows]

    def get_by_wallet(self, wallet: str) -> CopyTarget | None:
        """One target by wallet address, or None."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM copy_targets WHERE wallet = ?", (wallet,)
            ).fetchone()
        return self._row_to_target(row) if row else None

    @staticmethod
    def _row_to_target(row: sqlite3.Row) -> CopyTarget:
        return CopyTarget(
            wallet=row["wallet"],
            alias=row["alias"],
            enabled=bool(row["enabled"]),
            mode=row["mode"],
            fixed_buy_sol=row["fixed_buy_sol"],
            percent_of_leader=row["percent_of_leader"],
            max_position_sol=row["max_position_sol"],
            slippage_bps=row["slippage_bps"],
            priority_fee=row["priority_fee"],
            min_notional_sol=row["min_notional_sol"],
            require_confirmation=bool(row["require_confirmation"]),
        )