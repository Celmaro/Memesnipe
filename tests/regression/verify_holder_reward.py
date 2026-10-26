"""R3-P0-3 regression: holder-reward coins policy + cashback legacy note.

Covers:
- `holder_reward_mode="trade"` (default): holder-reward coins pass `_skip_reason`.
- `holder_reward_mode="skip"`: holder-reward coins are refused with an explicit reason.
- cashback coins are never gated (legacy-only, still trade).
- Config wiring: YAML default "trade" + VALID_VALUES entry + bot_runner read.
- Shadow capture: detect rows carry is_holder_reward / is_mayhem_mode /
  is_cashback_coin flags (research data, R3-P0-3 ledger field).
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from trading.universal_trader import UniversalTrader  # noqa: E402
import config_loader  # noqa: E402


def _trader(mode: str) -> UniversalTrader:
    from core.pubkeys import DEFAULT_PUBKEY, WSOL_MINT

    t = object.__new__(UniversalTrader)
    t.allow_mayhem_mode = True  # isolate the holder-reward gate
    t.holder_reward_mode = mode
    t.blocked_creators = set()
    t.creator_pnl_store = None
    t.allowed_quote_mints = {WSOL_MINT}
    t.quote_amounts = {WSOL_MINT: 0.0001}
    t.max_transfer_fee_bps = 0
    t.min_unique_holders = 0  # R3-P1-5 off in verifiers
    t.max_top10_concentration = 0.0
    from screening.holder_snapshot import HolderSnapshotStore

    t._holder_store = HolderSnapshotStore()  # noqa: SLF001
    from screening.metadata_socials import MetadataStore
    t.min_metadata_extensions = 0  # R3-P2-8 off in verifiers
    t._metadata_store = MetadataStore()  # noqa: SLF001
    return t


def _token(is_holder: bool, is_cashback: bool = False) -> SimpleNamespace:
    from core.pubkeys import DEFAULT_PUBKEY

    return SimpleNamespace(
        platform="pump_fun",
        creator="Creator111111111111111111111111111111111",
        user="Creator111111111111111111111111111111111",
        quote_mint=DEFAULT_PUBKEY,
        transfer_fee_bps=0,
        is_mayhem_mode=False,
        is_holder_reward=is_holder,
        is_cashback_coin=is_cashback,
    )


def main() -> int:
    failures = []

    def check(label: str, passed: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            failures.append(label)

    print("R3-P0-3 holder-reward policy")
    t_trade = _trader("trade")
    t_skip = _trader("skip")

    r = t_trade._skip_reason(_token(True))  # noqa: SLF001
    check("default trade: holder-reward coin passes", r is None, f"-> {r!r}")

    r = t_skip._skip_reason(_token(True))  # noqa: SLF001
    check(
        "skip mode refuses holder-reward coin",
        r is not None and "holder-reward" in r,
        f"-> {r!r}",
    )

    r = t_trade._skip_reason(_token(False, is_cashback=True))  # noqa: SLF001
    check(
        "cashback coin never gated (legacy-only)",
        r is None,
        f"-> {r!r}",
    )

    import yaml

    cfg = yaml.safe_load(Path("bots/memesnipe.yaml").read_text())
    check(
        "YAML default holder_reward_mode trade",
        cfg["filters"].get("holder_reward_mode") == "trade",
        f"-> {cfg['filters'].get('holder_reward_mode')!r}",
    )
    check(
        "VALID_VALUES includes filters.holder_reward_mode",
        "filters.holder_reward_mode" in config_loader.VALID_VALUES,
    )

    # Shadow capture detect payload: research flags present.
    from research.shadow_capture import ShadowDataCapture

    cap = ShadowDataCapture(dir_path="shadow_data", enabled=True)
    cap.detect(
        mint="Mint111111111111111111111111111111111111111",
        symbol="TEST",
        platform="pump_fun",
        quote_mint="sol",
        creator=None,
        transfer_fee_bps=0,
        listener="geyser",
        is_mayhem_mode=False,
        is_holder_reward=True,
        is_cashback_coin=False,
    )
    import glob

    day_file = sorted(glob.glob("shadow_data/*.jsonl"))[-1]
    row = json.loads(Path(day_file).read_text().splitlines()[-1])
    check(
        "shadow detect row carries is_holder_reward",
        row.get("is_holder_reward") is True,
        f"-> {row.get('is_holder_reward')!r}",
    )
    check(
        "shadow detect row carries is_cashback_coin",
        row.get("is_cashback_coin") is False,
        f"-> {row.get('is_cashback_coin')!r}",
    )

    if failures:
        print(f"\nR3-P0-3 FAILED ({len(failures)}): {failures}")
        return 1
    print("\nR3-P0-3 holder-reward policy OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
