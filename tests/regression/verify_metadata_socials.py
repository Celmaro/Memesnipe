"""R3-P2-8 regression: metadata socials gate.

Covers:
- parse_metadata_json: extensions extraction, non-empty counting, malformed
  JSON -> empty snapshot.
- MetadataSnapshot.non_empty_extensions: counts only non-empty entries.
- _skip_reason: with min_metadata_extensions > 0, a snapshot with zero
  non-empty extensions rejects; no snapshot (fail-open) passes; gate off
  (0) passes.
- Config wiring: YAML default 0, validation rule, bot_runner read.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from screening.metadata_socials import (  # noqa: E402
    MetadataSnapshot,
    MetadataStore,
    parse_metadata_json,
)
from trading.universal_trader import UniversalTrader  # noqa: E402
import config_loader  # noqa: E402

MINT = "Mint111111111111111111111111111111111111111"


def _trader(min_ext: int) -> UniversalTrader:
    from screening.metadata_socials import MetadataStore

    t = object.__new__(UniversalTrader)
    t.max_gini_coefficient = 0.0  # R4-P1-10 mock default
    t.rug_score_threshold = 0.0  # R4-P1-1 mock default
    t.factory_deploy_rate = 0.0  # R4-P1-2 mock default
    t.max_coordinated_supply_share = 0.0  # R4-P1-3 mock default
    t.exit_mode = "tp_sl"  # R4-P1-7 mock default
    t.strategies_ledger = None  # R4-P1-7 mock default
    t.min_metadata_extensions = min_ext
    t._metadata_store = MetadataStore()  # noqa: SLF001
    # _skip_reason deps
    t.blocked_creators = set()
    t.creator_pnl_store = None
    t.allow_mayhem_mode = True
    t.holder_reward_mode = "trade"
    t.min_unique_holders = 0
    t.max_top10_concentration = 0.0
    t.max_transfer_fee_bps = 0
    from core.pubkeys import WSOL_MINT

    t.allowed_quote_mints = {WSOL_MINT}
    t.quote_amounts = {WSOL_MINT: 0.0001}
    return t


def _token() -> SimpleNamespace:
    from core.pubkeys import DEFAULT_PUBKEY

    return SimpleNamespace(
        platform="pump_fun",
        creator="Creator111111111111111111111111111111111",
        user=None,
        quote_mint=DEFAULT_PUBKEY,
        transfer_fee_bps=0,
        mint=MINT,
    )


def main() -> int:
    failures = []

    def check(label: str, passed: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            failures.append(label)

    print("R3-P2-8 metadata socials gate")

    # Parse + counting.
    snap = parse_metadata_json(
        '{"name":"X","symbol":"X","extensions":{"twitter":"@x","website":"https://x.com"}}',
        MINT,
    )
    check("extensions parsed", snap.non_empty_extensions == 2, f"-> {snap.non_empty_extensions}")
    snap2 = parse_metadata_json(
        '{"name":"X","symbol":"X","extensions":{"twitter":"","website":null,"description":" "}}',
        MINT,
    )
    check("empty entries not counted", snap2.non_empty_extensions == 0, f"-> {snap2.non_empty_extensions}")
    snap3 = parse_metadata_json("not json", MINT)
    check("malformed JSON -> empty snapshot", snap3.non_empty_extensions == 0)

    # Gate semantics.
    t_off = _trader(0)
    r = t_off._skip_reason(_token())  # noqa: SLF001
    check("gate off -> no skip", r is None, f"-> {r!r}")

    t_on = _trader(1)
    r = t_on._skip_reason(_token())  # noqa: SLF001
    check("no snapshot -> fail open", r is None, f"-> {r!r}")

    t_on._metadata_store.put(MetadataSnapshot(mint=MINT, extensions={}))
    r = t_on._skip_reason(_token())  # noqa: SLF001
    check(
        "zero extensions rejects when enabled",
        r is not None and "metadata" in r,
        f"-> {r!r}",
    )

    t_on._metadata_store.put(
        MetadataSnapshot(mint=MINT, extensions={"twitter": "@x"})
    )
    r = t_on._skip_reason(_token())  # noqa: SLF001
    check("one non-empty extension passes", r is None, f"-> {r!r}")

    import yaml

    cfg = yaml.safe_load(Path("bots/memesnipe.yaml").read_text())
    check(
        "YAML default min_metadata_extensions 0",
        cfg["filters"].get("min_metadata_extensions") == 0,
        f"-> {cfg['filters'].get('min_metadata_extensions')!r}",
    )
    rule_paths = [r[0] for r in config_loader.CONFIG_VALIDATION_RULES]
    check(
        "validation rule for filters.min_metadata_extensions",
        "filters.min_metadata_extensions" in rule_paths,
    )

    if failures:
        print(f"\nR3-P2-8 FAILED ({len(failures)}): {failures}")
        return 1
    print("\nR3-P2-8 metadata socials gate OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())