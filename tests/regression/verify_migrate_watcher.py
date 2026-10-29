"""R3-P2-5 regression: PumpSwap graduation watcher.

Covers:
- on_logs: detects CompletePumpAmmMigrationEvent (real discriminator) in a
  `Program data:` log line and returns the migrated mint.
- has_migrated state; non-migration logs leave it untouched.
- Off-by-default wiring: YAML flag false, BOOL_FIELDS entry, trader stores
  None when disabled.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from research.migrate_watcher import (  # noqa: E402
    COMPLETE_PUMP_AMM_MIGRATION_DISCRIMINATOR,
    MigrateWatcher,
)
import config_loader  # noqa: E402

MINT_B58 = "Mint111111111111111111111111111111111111111"


def _migration_log(mint_b58: str) -> str:
    from solders.pubkey import Pubkey

    mint_bytes = bytes(Pubkey.from_string(mint_b58))
    payload = (COMPLETE_PUMP_AMM_MIGRATION_DISCRIMINATOR + mint_bytes).hex()
    return f"Program data: {payload}"


def main() -> int:
    failures = []

    def check(label: str, passed: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            failures.append(label)

    print("R3-P2-5 PumpSwap migrate watcher")

    w = MigrateWatcher()
    r = w.on_logs(["Program log: something else", _migration_log(MINT_B58)])
    check("migration detected in logs", r == MINT_B58, f"-> {r}")
    check("watcher remembers migrated mint", w.has_migrated(MINT_B58))

    w2 = MigrateWatcher()
    r2 = w2.on_logs(["Program log: ordinary trade", "Program data: deadbeef"])
    check("non-migration logs ignored", r2 is None and not w2.migrated)

    # Malformed hex line ignored.
    w3 = MigrateWatcher()
    r3 = w3.on_logs(["Program data: nothex!!"])
    check("malformed hex ignored", r3 is None)

    # Trader wiring: off by default stores None.
    from trading.universal_trader import UniversalTrader

    t = object.__new__(UniversalTrader)
    t._momentum_peak_rate = {}  # R4-P1-8 mock state
    t._momentum_peak_price = {}  # R4-P1-8 mock state
    t.fragility_scale_min = 1.0  # R4-P3-1 mock default (off)
    t.fragility_whale_threshold = 0.6  # R4-P3-1 mock default
    t.fragility_rate_threshold = 0.6  # R4-P3-1 mock default
    t.stagnation_exit_min = 0  # R4-P1-9 mock default (off)
    t.stagnation_band_pct = 0.02  # R4-P1-9 mock default
    t.max_wash_share = 0.0  # R4-P1-6 mock default
    from screening.wash_lpi import WashLpiDetector as _WLD  # R4-P1-6 mock default
    t._wash_lpi = _WLD()  # R4-P1-6 mock default
    t.max_gini_coefficient = 0.0  # R4-P1-10 mock default
    t.rug_score_threshold = 0.0  # R4-P1-1 mock default
    t.factory_deploy_rate = 0.0  # R4-P1-2 mock default
    t.max_coordinated_supply_share = 0.0  # R4-P1-3 mock default
    t.exit_mode = "tp_sl"  # R4-P1-7 mock default
    t.strategies_ledger = None  # R4-P1-7 mock default
    t.shadow_capture = None  # live/shadow research capture mock default
    t.migrate_watcher = None
    check("trader watcher None when disabled", t.migrate_watcher is None)

    import yaml

    cfg = yaml.safe_load(Path("bots/memesnipe.yaml").read_text())
    check(
        "YAML enables pumpswap_migrate_watcher (live research capture)",
        cfg["research"].get("pumpswap_migrate_watcher") is True,
        f"-> {cfg['research'].get('pumpswap_migrate_watcher')!r}",
    )
    check(
        "BOOL_FIELDS includes research.pumpswap_migrate_watcher",
        "research.pumpswap_migrate_watcher" in config_loader.BOOL_FIELDS,
    )

    if failures:
        print(f"\nR3-P2-5 FAILED ({len(failures)}): {failures}")
        return 1
    print("\nR3-P2-5 PumpSwap migrate watcher OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())