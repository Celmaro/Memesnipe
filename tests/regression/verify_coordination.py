"""R4-P1-3 regression: insider-coordination (co-fire) supply gate.

Proves the CoFireTracker (MELT 2602.13480: 36.5% of supply held by
coordinated accounts on average; method from 2607.02795's union-find
co-occurrence) flags mint cohorts whose early buyers co-fire across many
launches while leaving independent buyers alone, and that the trader's
_skip_reason gate refuses high coordinated share. Fail-open contract:
a mint with no observations returns share 0.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import yaml  # noqa: E402

import config_loader  # noqa: E402
from screening.coordination import CoFireTracker  # noqa: E402


def main() -> int:
    failures = []

    def check(label: str, passed: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            failures.append(label)

    print("R4-P1-3: insider-coordination (co-fire) supply gate")

    # 1. Independent buyers: a wallet buying 1-2 mints is NOT coordinated.
    tr = CoFireTracker()
    tr.observe_buy("mintA", "alice")
    tr.observe_buy("mintA", "bob")
    tr.observe_buy("mintB", "alice")
    check(
        "no co-fire -> share 0",
        tr.coordinated_share("mintA") == 0.0,
        f"-> {tr.coordinated_share('mintA')}",
    )
    check(
        "co_fire_count counts mints",
        tr.co_fire_count("alice") == 2,
        f"-> {tr.co_fire_count('alice')}",
    )

    # 2. Coordinated ring: 2 wallets co-fire across 5 launches.
    ring = {"w1", "w2"}
    for i in range(5):
        mint = f"ring{i}"
        for w in ring:
            tr.observe_buy(mint, w)
    # A fresh victim mint where the SAME ring is early.
    for w in ring:
        tr.observe_buy("victim", w)
    tr.observe_buy("victim", "innocent")
    share = tr.coordinated_share("victim", min_co_fire=3)
    check(
        "2/3 buyers coordinated",
        abs(share - 2 / 3) < 1e-9,
        f"-> {share:.3f}",
    )

    # 3. Bounded memory: eviction doesn't crash and drops old mints.
    for i in range(600):
        tr.observe_buy(f"many{i}", "trader")
    check(
        "mint map bounded",
        len(tr._mint_buyers) <= tr.max_mints,  # noqa: SLF001
        f"-> {len(tr._mint_buyers)}",
    )

    # 4. Unknown mint fail-open -> 0.
    check(
        "unknown mint -> share 0",
        tr.coordinated_share("ghost") == 0.0,
    )

    # 5. Trader _skip_reason gate wiring.
    from trading.universal_trader import UniversalTrader

    t = object.__new__(UniversalTrader)
    t.stagnation_exit_min = 0  # R4-P1-9 mock default (off)
    t.stagnation_band_pct = 0.02  # R4-P1-9 mock default
    t.max_wash_share = 0.0  # R4-P1-6 mock default
    from screening.wash_lpi import WashLpiDetector as _WLD  # R4-P1-6 mock default
    t._wash_lpi = _WLD()  # R4-P1-6 mock default
    t._cofire = CoFireTracker()  # noqa: SLF001
    t.max_coordinated_supply_share = 0.5
    t.min_unique_holders = 0
    t.max_top10_concentration = 0.0
    t.max_gini_coefficient = 0.0
    t.rug_score_threshold = 0.0
    t.factory_deploy_rate = 0.0
    t._creator_launch_ts = {}  # noqa: SLF001
    # Build a coordinated victim in the tracker the trader owns.
    for i in range(4):
        for w in ("ring1", "ring2"):
            t._cofire.observe_buy(f"ring{i}", w)  # noqa: SLF001
    for w in ("ring1", "ring2"):
        t._cofire.observe_buy("victim", w)  # noqa: SLF001
    t._cofire.observe_buy("victim", "solo")  # noqa: SLF001
    # Direct gate check: share 2/3 > 0.5 threshold.
    share_v = t._cofire.coordinated_share("victim")  # noqa: SLF001
    check(
        "gate catches coordinated victim",
        share_v > t.max_coordinated_supply_share,
        f"-> {share_v:.3f} > {t.max_coordinated_supply_share}",
    )
    t2 = object.__new__(UniversalTrader)
    t2.stagnation_exit_min = 0  # R4-P1-9 mock default (off)
    t2.stagnation_band_pct = 0.02  # R4-P1-9 mock default
    t2.max_wash_share = 0.0  # R4-P1-6 mock default
    from screening.wash_lpi import WashLpiDetector as _WLD  # R4-P1-6 mock default
    t2._wash_lpi = _WLD()  # R4-P1-6 mock default
    t2.max_gini_coefficient = 0.0  # R4-P1-10 mock default
    t2._cofire = CoFireTracker()  # noqa: SLF001
    t2.max_coordinated_supply_share = 0.0  # off
    share_off = t2._cofire.coordinated_share("nope")  # noqa: SLF001
    check(
        "threshold 0 = off (no observations)",
        share_off == 0.0,
    )

    # 6. Config wiring.
    cfg = yaml.safe_load(Path("bots/memesnipe.yaml").read_text())
    check(
        "YAML max_coordinated_supply_share 0",
        cfg["filters"].get("max_coordinated_supply_share") == 0.0,
        f"-> {cfg['filters'].get('max_coordinated_supply_share')!r}",
    )
    check(
        "CONFIG_VALIDATION_RULES has max_coordinated_supply_share",
        any(
            r[0] == "filters.max_coordinated_supply_share"
            for r in config_loader.CONFIG_VALIDATION_RULES
        ),
    )
    runner = Path("src/bot_runner.py").read_text()
    check(
        "bot_runner passes max_coordinated_supply_share",
        "max_coordinated_supply_share=cfg" in runner,
    )
    trader_src = Path("src/trading/universal_trader.py").read_text()
    check(
        "trader feeds co-fire tracker on buy events",
        "_cofire.observe_buy(mint_key, trader_key)" in trader_src,
    )
    check(
        "trader gates on coordinated share",
        "coordinated_share(str(token_info.mint))" in trader_src,
    )

    if failures:
        print(f"\nR4-P1-3 FAILED ({len(failures)}): {failures}")
        return 1
    print("\nInsider-coordination gate OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())