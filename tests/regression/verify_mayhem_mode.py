"""R3-P0-2 regression: Mayhem Mode coins are a distinct asset class.

Covers:
- `_skip_reason` refuses a mayhem-mode coin when `allow_mayhem_mode` is off
  (default, disabled-first) with an explicit reason.
- `_skip_reason` passes the same coin when `allow_mayhem_mode` is on.
- Config wiring: YAML default off + BOOL_FIELDS entry.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from trading.universal_trader import UniversalTrader  # noqa: E402
import config_loader  # noqa: E402


def _trader(allow_mayhem_mode: bool) -> UniversalTrader:
    t = object.__new__(UniversalTrader)
    t.exit_mode = "tp_sl"  # R4-P1-7 mock default
    t.strategies_ledger = None  # R4-P1-7 mock default
    t.allow_mayhem_mode = allow_mayhem_mode
    t.holder_reward_mode = "trade"  # R3-P0-3 default
    t.min_unique_holders = 0  # R3-P1-5 off in verifiers
    t.max_top10_concentration = 0.0
    from screening.holder_snapshot import HolderSnapshotStore
    t._holder_store = HolderSnapshotStore()  # noqa: SLF001
    from screening.metadata_socials import MetadataStore
    t.min_metadata_extensions = 0  # R3-P2-8 off in verifiers
    t._metadata_store = MetadataStore()  # noqa: SLF001
    # Keep the rest of _skip_reason from tripping: matching creator, allowed
    # quote, buy amount present, no transfer-fee gate.
    from solders.pubkey import Pubkey

    from core.pubkeys import DEFAULT_PUBKEY, WSOL_MINT

    t.blocked_creators = set()
    t.creator_pnl_store = None
    t.allowed_quote_mints = {WSOL_MINT}
    t.quote_amounts = {WSOL_MINT: 0.0001}
    t.max_transfer_fee_bps = 0
    return t


def _token(is_mayhem: bool) -> SimpleNamespace:
    from core.pubkeys import DEFAULT_PUBKEY

    return SimpleNamespace(
        platform="pump_fun",
        creator="Creator111111111111111111111111111111111",
        user="Creator111111111111111111111111111111111",
        quote_mint=DEFAULT_PUBKEY,  # SOL-paired -> normalizes to WSOL
        transfer_fee_bps=0,
        is_mayhem_mode=is_mayhem,
    )


def main() -> int:
    failures = []

    def check(label: str, passed: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            failures.append(label)

    print("R3-P0-2 mayhem-mode gate")
    t_off = _trader(allow_mayhem_mode=False)
    t_on = _trader(allow_mayhem_mode=True)

    reason = t_off._skip_reason(_token(True))  # noqa: SLF001
    check(
        "mayhem coin refused when allow_mayhem_mode off",
        reason is not None and "mayhem" in reason.lower(),
        f"-> {reason!r}",
    )

    reason = t_off._skip_reason(_token(False))  # noqa: SLF001
    check(
        "normal coin unaffected when off",
        reason is None,
        f"-> {reason!r}",
    )

    reason = t_on._skip_reason(_token(True))  # noqa: SLF001
    check(
        "mayhem coin allowed when allow_mayhem_mode on",
        reason is None,
        f"-> {reason!r}",
    )

    import yaml

    cfg = yaml.safe_load(Path("bots/memesnipe.yaml").read_text())
    check(
        "YAML default allow_mayhem_mode false",
        cfg["filters"].get("allow_mayhem_mode") is False,
        f"-> {cfg['filters'].get('allow_mayhem_mode')!r}",
    )
    check(
        "BOOL_FIELDS includes filters.allow_mayhem_mode",
        "filters.allow_mayhem_mode" in config_loader.BOOL_FIELDS,
    )

    if failures:
        print(f"\nR3-P0-2 FAILED ({len(failures)}): {failures}")
        return 1
    print("\nR3-P0-2 mayhem-mode gate OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
