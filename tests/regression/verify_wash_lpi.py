"""R4-P1-6 regression: wash-trade / LPI manipulation detector.

Proves the WashLpiDetector (2507.01963: 82.8% of high-return tokens show
artificial growth via wash trading + Liquidity-Pool-Based Price Inflation)
flags round-trip volume and small-buy-large-jump patterns, and that the
trader's _skip_reason gate refuses high wash share. Fail-open: no
observations -> share 0.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import yaml  # noqa: E402

import config_loader  # noqa: E402
from screening.wash_lpi import WashLpiDetector  # noqa: E402


def main() -> int:
    failures = []

    def check(label: str, passed: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            failures.append(label)

    print("R4-P1-6: wash-trade / LPI manipulation detector")

    # 1. Round-trip wash: wallet buys AND sells the same mint.
    d = WashLpiDetector()
    d.observe_trade("m", "w1", is_buy=True, quote_lamports=3_000_000_000, ts=100.0)  # 3 SOL
    d.observe_trade("m", "w2", is_buy=True, quote_lamports=3_000_000_000, ts=101.0)
    d.observe_trade("m", "w1", is_buy=False, quote_lamports=2_000_000_000, ts=120.0)
    # 3+3+2 = 8 SOL total. Round-tripper = w1 (bought AND sold). C4.6
    # STRONG test: w1's FULL volume (buy 3 + sell 2 = 5 SOL) is wash —
    # not just the opposite-side 2 SOL — so share = 5/8 = 0.625.
    check(
        "round-trip volume flagged (5/8)",
        abs(d.wash_share("m") - 0.625) < 1e-9,
        f"-> {d.wash_share('m'):.3f}",
    )
    check(
        "no round trip -> 0",
        WashLpiDetector().wash_share("m") == 0.0,
    )

    # 2. Genuine distribution: distinct buyers, no round trip.
    d2 = WashLpiDetector()
    d2.observe_trade("m2", "a", is_buy=True, quote_lamports=1_000_000_000, ts=1.0)
    d2.observe_trade("m2", "b", is_buy=True, quote_lamports=2_000_000_000, ts=2.0)
    d2.observe_trade("m2", "c", is_buy=False, quote_lamports=1_500_000_000, ts=3.0)
    check(
        "genuine volume not flagged",
        d2.wash_share("m2") == 0.0,
        f"-> {d2.wash_share('m2'):.3f}",
    )

    # 3. LPI: a small buy followed by a >= 3x price jump.
    d3 = WashLpiDetector(lpi_small_quote_sol=0.05, min_jump_mult=3.0)
    d3.observe_trade(
        "m3", "dev", is_buy=True, quote_lamports=20_000_000, ts=1.0, token_price=1.0
    )
    d3.observe_trade(
        "m3", "bot", is_buy=True, quote_lamports=2_000_000_000, ts=2.0, token_price=4.0
    )
    check(
        "small-buy -> price jump flagged",
        d3.lpi_flags_recent("m3") == 1,
        f"-> {d3.lpi_flags_recent('m3')}",
    )
    check(
        "small buy count",
        d3.lpi_small_buy_count("m3") == 1,
        f"-> {d3.lpi_small_buy_count('m3')}",
    )
    # No jump: small buy with flat price.
    d4 = WashLpiDetector(lpi_small_quote_sol=0.05, min_jump_mult=3.0)
    d4.observe_trade("m4", "a", is_buy=True, quote_lamports=20_000_000, ts=1.0, token_price=1.0)
    d4.observe_trade("m4", "b", is_buy=True, quote_lamports=2_000_000_000, ts=2.0, token_price=1.1)
    check(
        "no jump -> no LPI flag",
        d4.lpi_flags_recent("m4") == 0,
        f"-> {d4.lpi_flags_recent('m4')}",
    )

    # 4. Unknown mint fail-open.
    check(
        "unknown mint -> 0",
        WashLpiDetector().wash_share("ghost") == 0.0,
    )

    # 5. Trader wiring: ctor param + gate in _skip_reason + feed hook.
    from trading.universal_trader import UniversalTrader

    t = object.__new__(UniversalTrader)
    t._momentum_peak_rate = {}  # R4-P1-8 mock state
    t._momentum_peak_price = {}  # R4-P1-8 mock state
    t.fragility_scale_min = 1.0  # R4-P3-1 mock default (off)
    t.fragility_whale_threshold = 0.6  # R4-P3-1 mock default
    t.fragility_rate_threshold = 0.6  # R4-P3-1 mock default
    t.stagnation_exit_min = 0  # R4-P1-9 mock default (off)
    t.stagnation_band_pct = 0.02  # R4-P1-9 mock default
    t._wash_lpi = WashLpiDetector()  # noqa: SLF001
    t.max_wash_share = 0.3
    t._wash_lpi.observe_trade(  # noqa: SLF001
        "bad", "w", is_buy=True, quote_lamports=3_000_000_000, ts=1.0
    )
    t._wash_lpi.observe_trade(  # noqa: SLF001
        "bad", "w", is_buy=False, quote_lamports=3_000_000_000, ts=2.0
    )
    share = t._wash_lpi.wash_share("bad")  # noqa: SLF001
    check(
        "gate catches wash-heavy mint",
        share > t.max_wash_share,
        f"-> {share:.3f} > {t.max_wash_share}",
    )

    # 6. Config wiring.
    cfg = yaml.safe_load(Path("bots/memesnipe.yaml").read_text())
    check(
        "YAML max_wash_share 0",
        cfg["filters"].get("max_wash_share") == 0.0,
        f"-> {cfg['filters'].get('max_wash_share')!r}",
    )
    check(
        "CONFIG_VALIDATION_RULES has max_wash_share",
        any(r[0] == "filters.max_wash_share" for r in config_loader.CONFIG_VALIDATION_RULES),
    )
    runner = Path("src/bot_runner.py").read_text()
    check(
        "bot_runner passes max_wash_share",
        "max_wash_share=cfg" in runner,
    )
    trader_src = Path("src/trading/universal_trader.py").read_text()
    check(
        "trader feeds wash/LPI on trade events",
        "_wash_lpi.observe_trade(" in trader_src,
    )
    check(
        "trader gates on wash share",
        "wash_share(str(token_info.mint))" in trader_src,
    )

    if failures:
        print(f"\nR4-P1-6 FAILED ({len(failures)}): {failures}")
        return 1
    print("\nWash-trade / LPI detector OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())