"""PumpSwap graduation watcher (R3-P2-5).

Detects the pump.fun `CompletePumpAmmMigrationEvent` in `Program data:` log
lines — the on-chain signal that a completed curve migrated to PumpSwap —
and records the mint so downstream consumers (copy-trader feed, wallet
watchers) can treat post-graduation activity as PumpSwap trades instead of
curve trades.

Detection pattern mirrors the repo's event parser: scan `Program data:` log
lines for the 8-byte LE event discriminator. Event layout (from the IDL):

    CompletePumpAmmMigrationEvent { mint: Pubkey (32 bytes) }

Disabled-first: the watcher is constructed and wired only when enabled
(`research.pumpswap_migrate_watcher: true`); a None/absent watcher keeps
every consumer byte-identical to prior behavior.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from solders.pubkey import Pubkey

#: First 8 bytes of SHA256("event:CompletePumpAmmMigrationEvent").
COMPLETE_PUMP_AMM_MIGRATION_DISCRIMINATOR = bytes.fromhex("bde95db95c94ea94")


@dataclass
class MigrateWatcher:
    """Tracks mints that migrated to PumpSwap, from geyser log lines.

    R4-P3-2: a geyser log line is a *signal*, not a verification. The
    graduation-label audit (2607.02823) shows collector-generated labels
    carry measurement error (dev AUROC 0.86 -> validation 0.46, coin
    flip). Consumers that act on graduation (exit/leader logic) must
    confirm the migration against a direct pool-state read first; this
    watcher keeps the log-derived signal separate from the confirmed
    verdict so no label is trusted before verification.
    """

    #: mint string -> True once a migration event was seen (log signal).
    migrated: dict[str, bool] = field(default_factory=dict)
    #: mint string -> True once a DIRECT pool-state read confirmed it.
    confirmed: dict[str, bool] = field(default_factory=dict)

    def on_logs(self, logs: list[str]) -> str | None:
        """Scan one batch of `Program data:` logs for a migration event.

        Returns the migrated mint (base58) when found, else None.
        """
        for line in logs:
            if "Program data:" not in line:
                continue
            payload = line.split("Program data:", 1)[1].strip()
            try:
                data = bytes.fromhex(payload)
            except (TypeError, ValueError):
                continue
            if len(data) < 40:
                continue
            if data[:8] != COMPLETE_PUMP_AMM_MIGRATION_DISCRIMINATOR:
                continue
            # 8-byte discriminator + 32-byte mint.
            try:
                mint = str(Pubkey.from_bytes(data[8:40]))
            except (ValueError, TypeError):
                continue
            self.migrated[mint] = True
            return mint
        return None

    def confirm(self, mint: str, pool_exists: bool) -> None:
        """Mark a migration verified (or refuted) by a direct pool-state read.

        R4-P3-2: ``pool_exists=True`` confirms the mint's PumpSwap pool is
        live on-chain; ``False`` (or a failed read) leaves the log signal
        unconfirmed — callers may not treat it as a graduation.
        """
        self.confirmed[mint] = bool(pool_exists)

    def has_migrated(self, mint: str, require_confirmed: bool = False) -> bool:
        """Whether the mint migrated.

        ``require_confirmed=False`` (legacy) trusts the log line alone;
        ``True`` (R4-P3-2) requires a direct pool-state confirmation —
        unverified labels return False (the fail-closed direction for
        graduation-triggered actions).
        """
        if not self.migrated.get(mint, False):
            return False
        if require_confirmed:
            return self.confirmed.get(mint, False)
        return True

    def clear(self) -> None:
        self.migrated.clear()
        self.confirmed.clear()
