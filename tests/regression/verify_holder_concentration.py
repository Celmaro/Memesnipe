"""R3-P1-5 regression: holder-concentration gate.

Covers:
- compute_snapshot: unique-holder floor (observed top accounts), top10
  fraction math, supply-zero guard, clamp to [0,1].
- HolderSnapshot.passes: min_unique_holders / max_top10_concentration gates
  with 0 = off.
- _skip_reason: refuses a mint whose snapshot fails the gates; fails OPEN
  (allows) when the mint has no snapshot yet (refresher not run / RPC down);
  allows when gates are off.
- Config wiring: YAML defaults 0 / 0.0, validation rules, refresher guard.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from screening.holder_snapshot import (  # noqa: E402
    HolderSnapshot,
    HolderSnapshotStore,
    compute_snapshot,
)
from trading.universal_trader import UniversalTrader  # noqa: E402
import config_loader  # noqa: E402

MINT = "Mint111111111111111111111111111111111111111"


def _trader(min_holders: int, max_top10: float) -> UniversalTrader:
    from screening.holder_snapshot import HolderSnapshotStore

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
    t.min_unique_holders = min_holders
    t.max_top10_concentration = max_top10
    t._holder_store = HolderSnapshotStore()  # noqa: SLF001
    from screening.metadata_socials import MetadataStore
    t.min_metadata_extensions = 0  # R3-P2-8 off in verifiers
    t._metadata_store = MetadataStore()  # noqa: SLF001
    # _skip_reason deps
    t.blocked_creators = set()
    t.creator_pnl_store = None
    t.allow_mayhem_mode = True
    t.holder_reward_mode = "trade"
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

    print("R3-P1-5 holder-concentration gate")

    # Snapshot math.
    rows = [
        (f"owner{i}", 1000)
        for i in range(10)
    ]  # 10 accounts, 1000 each = 10000
    snap = compute_snapshot(MINT, rows, total_supply_raw=100_000, slot=42)
    check("unique holders = len(rows)", snap.unique_holders == 10, f"-> {snap.unique_holders}")
    check("top10 fraction 0.1", abs(snap.top10_fraction - 0.1) < 1e-9, f"-> {snap.top10_fraction}")
    check("slot carried", snap.slot == 42)

    snap2 = compute_snapshot(MINT, rows[:5], total_supply_raw=0)  # supply zero
    check("zero supply -> fraction 0 (no div0)", snap2.top10_fraction == 0.0)

    snap3 = compute_snapshot(MINT, rows, total_supply_raw=1)  # top10 > supply
    check("fraction clamped to 1.0", snap3.top10_fraction == 1.0)

    # passes() semantics.
    check("gate off by default", snap.passes(min_unique_holders=0, max_top10_concentration=0.0))
    check(
        "min_unique_holders floor refuses",
        not snap.passes(min_unique_holders=11, max_top10_concentration=0.0),
    )
    check(
        "max_top10_concentration refuses concentrated",
        not snap.passes(min_unique_holders=0, max_top10_concentration=0.05),
    )
    check(
        "both satisfied",
        snap.passes(min_unique_holders=10, max_top10_concentration=0.2),
    )

    # _skip_reason semantics.
    t_off = _trader(0, 0.0)
    r = t_off._skip_reason(_token())  # noqa: SLF001
    check("gates off -> no skip", r is None, f"-> {r!r}")

    t_on = _trader(20, 0.5)
    r = t_on._skip_reason(_token())  # noqa: SLF001
    check("no snapshot -> fail open (allow)", r is None, f"-> {r!r}")

    bad = compute_snapshot(MINT, [("a", 1)] * 5, total_supply_raw=10_000)
    t_on._holder_store.put(bad)
    r = t_on._skip_reason(_token())  # noqa: SLF001
    check(
        "snapshot failing floor -> skip",
        r is not None and "holder distribution" in r,
        f"-> {r!r}",
    )

    t_conc = _trader(0, 0.5)
    conc = compute_snapshot(
        MINT, [(f"w{i}", 1000) for i in range(8)], total_supply_raw=10_000
    )
    t_conc._holder_store.put(conc)  # top8 = 8000/10000 = 0.8 > 0.5
    r = t_conc._skip_reason(_token())  # noqa: SLF001
    check(
        "snapshot failing concentration -> skip",
        r is not None and "top-10" in r,
        f"-> {r!r}",
    )

    # Config wiring.
    import yaml

    cfg = yaml.safe_load(Path("bots/memesnipe.yaml").read_text())
    check(
        "YAML default min_unique_holders 0",
        cfg["filters"].get("min_unique_holders") == 0,
        f"-> {cfg['filters'].get('min_unique_holders')!r}",
    )
    check(
        "YAML default max_top10_concentration 0.0",
        cfg["filters"].get("max_top10_concentration") == 0.0,
        f"-> {cfg['filters'].get('max_top10_concentration')!r}",
    )
    rule_paths = [r[0] for r in config_loader.CONFIG_VALIDATION_RULES]
    check(
        "validation rule for filters.min_unique_holders",
        "filters.min_unique_holders" in rule_paths,
    )

    if failures:
        print(f"\nR3-P1-5 FAILED ({len(failures)}): {failures}")
        return 1
    print("\nR3-P1-5 holder-concentration gate OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())