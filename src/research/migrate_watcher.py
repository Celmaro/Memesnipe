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
    """Tracks mints that migrated to PumpSwap, from geyser log lines."""

    #: mint string -> True once a migration event was seen.
    migrated: dict[str, bool] = field(default_factory=dict)

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

    def has_migrated(self, mint: str) -> bool:
        return self.migrated.get(mint, False)

    def clear(self) -> None:
        self.migrated.clear()
