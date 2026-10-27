"""R4-P3-2 regression: graduation-label verification.

2607.02823 (full text): collector-generated graduation labels fail temporal
generalization (dev AUROC 0.86 -> validation 0.46, coin flip; calibration
slope 0.013; collector missed 42/43 first-invisible events). The
MigrateWatcher therefore keeps the geyser log SIGNAL separate from the
CONFIRMED verdict: consumers acting on graduation must confirm the
migration against a direct pool-state read first. This verifier proves:
- on_logs() records the signal;
- has_migrated(require_confirmed=False) keeps legacy behavior;
- has_migrated(require_confirmed=True) fails closed until confirm();
- confirm(False) leaves the label unconfirmed.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from research.migrate_watcher import (  # noqa: E402
    COMPLETE_PUMP_AMM_MIGRATION_DISCRIMINATOR,
    MigrateWatcher,
)


def main() -> int:
    failures = []

    def check(label: str, passed: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            failures.append(label)

    print("R4-P3-2: graduation-label verification")

    from solders.keypair import Keypair

    mint = str(Keypair().pubkey())
    # Build a realistic Program data: log line: discriminator + mint.
    payload = (
        COMPLETE_PUMP_AMM_MIGRATION_DISCRIMINATOR.hex()
        + bytes(Keypair().pubkey()).hex()[:64]
    )
    # The mint must match the payload's last 32 bytes exactly.
    payload = COMPLETE_PUMP_AMM_MIGRATION_DISCRIMINATOR.hex() + bytes(
        Keypair().pubkey()
    ).hex()
    log_line = f"Program data: {payload}"
    # Reconstruct the mint the watcher will decode from that payload.
    from solders.pubkey import Pubkey as SP

    mint_from_payload = str(
        SP.from_bytes(bytes.fromhex(payload)[8:40])
    )

    w = MigrateWatcher()
    found = w.on_logs([log_line])
    check(
        "log signal recorded",
        found == mint_from_payload,
        f"-> {found} vs {mint_from_payload}",
    )
    check(
        "legacy has_migrated trusts the signal",
        w.has_migrated(mint_from_payload) is True,
    )
    check(
        "require_confirmed fails closed before confirmation",
        w.has_migrated(mint_from_payload, require_confirmed=True) is False,
    )

    # Confirm with a real pool-state read.
    w.confirm(mint_from_payload, pool_exists=True)
    check(
        "confirmed migration passes the strict check",
        w.has_migrated(mint_from_payload, require_confirmed=True) is True,
    )

    # Refute: pool read fails / pool missing -> still unconfirmed.
    w2 = MigrateWatcher()
    w2.on_logs([log_line])
    w2.confirm(mint_from_payload, pool_exists=False)
    check(
        "refuted pool keeps label unconfirmed",
        w2.has_migrated(mint_from_payload, require_confirmed=True) is False,
    )

    # Unknown mint: never migrated.
    other = str(Keypair().pubkey())
    check(
        "unseen mint not migrated",
        w.has_migrated(other, require_confirmed=True) is False
        and w.has_migrated(other) is False,
    )

    # Clear resets both dicts.
    w.clear()
    check(
        "clear resets signal + confirmation",
        w.migrated == {} and w.confirmed == {},
    )

    # R4-P3-2 wiring: the trader actually calls confirm() when the watcher
    # flags a migration (the audit found confirm() with zero callers —
    # dead wiring; _confirm_migration is the fix, spawned fire-and-forget
    # from the migration-detect site).
    src = (Path(__file__).resolve().parents[2] / "src/trading/universal_trader.py").read_text()
    check("_confirm_migration method exists", "async def _confirm_migration" in src)
    check(
        "migration-detect site spawns confirm task",
        "self._confirm_migration(migrated_mint)" in src,
    )
    check(
        "confirm feeds the watcher",
        "watcher.confirm(mint_str, pool_exists)" in src,
    )

    if failures:
        print(f"\nR4-P3-2 FAILED ({len(failures)}): {failures}")
        return 1
    print("\nGraduation-label verification OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())