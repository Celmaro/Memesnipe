"""R4-P3-1 regression: fragility-scaled position sizing (ME2F; 2512.00377).

Proves the ME2F two-signal rule: position size scales down only when the
volatility/whale score AND the trade-rate score are both above their
thresholds ("VDS and SAS both high -> tighten risk, slow position
growth"); a single-signal spike leaves size alone; unknown dims fail
open; scale_min 1.0 = off (flat sizing).
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import yaml  # noqa: E402

import config_loader  # noqa: E402
from screening.fragility import (  # noqa: E402
    fragility_scale,
    sentiment_score,
    volatility_score,
    whale_score,
)
from screening.wash_lpi import WashLpiDetector  # noqa: E402


def _eps(a, b, tol=1e-9):
    return abs(a - b) < tol


def main() -> int:
    failures = []

    def check(label: str, passed: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            failures.append(label)

    print("R4-P3-1: fragility-scaled position sizing (ME2F)")

    # 1. Pure math: volatility score from a price path.
    v_flat = volatility_score([1.0, 1.01, 0.99, 1.0, 1.02])
    v_wild = volatility_score([1.0, 2.0, 0.5, 3.0, 1.0])
    check("flat path low VDS", v_flat is not None and v_flat < 0.3, f"-> {v_flat}")
    check("wild path high VDS", v_wild is not None and v_wild > 0.7, f"-> {v_wild}")
    check(
        "empty path -> None (fail open)",
        volatility_score([]) is None and volatility_score([1.0]) is None,
    )

    # 2. Whale score: gini/top10.
    check(
        "concentrated holders high WDS",
        whale_score(0.85, 0.95) is not None and whale_score(0.85, 0.95) > 0.9,
        f"-> {whale_score(0.85, 0.95)}",
    )
    check(
        "unknown whale dims -> None",
        whale_score(None, None) is None,
    )

    # 3. Sentiment (rate) score.
    check(
        "high rate high SAS",
        sentiment_score([25.0], rate_ceiling=30.0) is not None
        and sentiment_score([25.0], rate_ceiling=30.0) > 0.8,
        f"-> {sentiment_score([25.0], rate_ceiling=30.0)}",
    )

    # 4. THE two-signal rule: scale only when BOTH high.
    check(
        "both high -> scaled down",
        _eps(fragility_scale(0.9, 0.9, scale_min=0.5), 0.5),
        f"-> {fragility_scale(0.9, 0.9, scale_min=0.5)}",
    )
    check(
        "VDS high alone -> no scale",
        _eps(fragility_scale(0.9, 0.1, scale_min=0.5), 1.0),
        f"-> {fragility_scale(0.9, 0.1, scale_min=0.5)}",
    )
    check(
        "SAS high alone -> no scale",
        _eps(fragility_scale(0.1, 0.9, scale_min=0.5), 1.0),
    )
    check(
        "unknown VDS -> no scale",
        _eps(fragility_scale(None, 0.9, scale_min=0.5), 1.0),
    )
    check(
        "unknown SAS -> no scale",
        _eps(fragility_scale(0.9, None, scale_min=0.5), 1.0),
    )
    check(
        "scale_min 1.0 = off",
        _eps(fragility_scale(0.9, 0.9, scale_min=1.0), 1.0),
    )

    # 5. Trader _fragility_buy_scale end-to-end: flat coin sizes full,
    # fragile coin sizes down.
    from trading.universal_trader import UniversalTrader

    t = object.__new__(UniversalTrader)
    t._momentum_peak_rate = {}  # R4-P1-8 mock state
    t._momentum_peak_price = {}  # R4-P1-8 mock state
    t.fragility_scale_min = 0.5
    t.fragility_whale_threshold = 0.6
    t.fragility_rate_threshold = 0.6
    t._wash_lpi = WashLpiDetector(lpi_small_quote_sol=0.05, min_jump_mult=3.0)
    t._holder_store = type("S", (), {"get": lambda self, m: None})()

    # Flat: stable prices, one buyer, no holdings snapshot -> scale 1.0.
    import asyncio

    t._wash_lpi.observe_trade(
        "m", "a", is_buy=True, quote_lamports=1_000_000_000, ts=0.0, token_price=1.0
    )
    t._wash_lpi.observe_trade(
        "m", "b", is_buy=True, quote_lamports=1_000_000_000, ts=1.0, token_price=1.0
    )
    scale_flat = asyncio.run(t._fragility_buy_scale(_ti("m")))
    check("flat coin sizes full", _eps(scale_flat, 1.0), f"-> {scale_flat}")

    # Fragile: wild price path + high rate -> scaled down.
    t2 = object.__new__(UniversalTrader)
    t2._momentum_peak_rate = {}  # R4-P1-8 mock state
    t2._momentum_peak_price = {}  # R4-P1-8 mock state
    t2.fragility_scale_min = 0.5
    t2.grad_timing_enabled = False  # R4-P1-4 mock
    t2._grad_band_stats = {}  # R4-P1-4 mock
    t2.fragility_whale_threshold = 0.6
    t2.fragility_rate_threshold = 0.6
    t2._wash_lpi = WashLpiDetector(lpi_small_quote_sol=0.05, min_jump_mult=3.0)
    t2._holder_store = type("S", (), {"get": lambda self, m: None})()
    for i, px in enumerate([1.0, 2.0, 0.5, 3.0, 1.0, 4.0]):
        t2._wash_lpi.observe_trade(
            "f", "a", is_buy=True, quote_lamports=1_000_000_000, ts=float(i), token_price=px
        )
    scale_fragile = asyncio.run(t2._fragility_buy_scale(_ti("f")))
    check(
        "fragile coin sizes down",
        scale_fragile < 1.0,
        f"-> {scale_fragile}",
    )

    # Off: scale_min 1.0 -> always 1.0.
    t3 = object.__new__(UniversalTrader)
    t3._momentum_peak_rate = {}  # R4-P1-8 mock state
    t3.grad_timing_enabled = False  # R4-P1-4 mock
    t3._grad_band_stats = {}  # R4-P1-4 mock
    t3._momentum_peak_price = {}  # R4-P1-8 mock state
    t3.fragility_scale_min = 1.0
    t3.contention_enabled = False
    t3.contention_multiplier = 2.5
    t3.contention_creates_per_min = 20.0
    t3.contention_window_s = 60.0
    t3.max_fee_fraction_of_position = 0.005
    t3._jito_executor = None
    t3.fragility_whale_threshold = 0.6
    t3.fragility_rate_threshold = 0.6
    t3._wash_lpi = WashLpiDetector()
    t3._holder_store = type("S", (), {"get": lambda self, m: None})()
    check(
        "feature off -> always 1.0",
        asyncio.run(t3._fragility_buy_scale(_ti("x"))) == 1.0,
    )

    # 6. Config wiring.
    cfg = yaml.safe_load(Path("bots/memesnipe.yaml").read_text())
    check(
        "YAML fragility_scale_min 1.0 (off)",
        cfg["filters"].get("fragility_scale_min") == 1.0,
        f"-> {cfg['filters'].get('fragility_scale_min')!r}",
    )
    for key in (
        "filters.fragility_scale_min",
        "filters.fragility_whale_threshold",
        "filters.fragility_rate_threshold",
    ):
        check(
            f"CONFIG_VALIDATION_RULES has {key}",
            any(r[0] == key for r in config_loader.CONFIG_VALIDATION_RULES),
        )
    runner = Path("src/bot_runner.py").read_text()
    check(
        "bot_runner passes fragility_scale_min",
        "fragility_scale_min=cfg" in runner,
    )
    trader_src = Path("src/trading/universal_trader.py").read_text()
    check(
        "trader applies scale in buy path",
        "eff_quote = self.quote_amounts[token_quote_mint] * frag_scale" in trader_src,
    )

    if failures:
        print(f"\nR4-P3-1 FAILED ({len(failures)}): {failures}")
        return 1
    print("\nFragility-scaled sizing OK")
    return 0


def _ti(mint: str):
    from interfaces.core import Platform, TokenInfo

    ti = TokenInfo(  # type: ignore[arg-type]
        name="T",
        symbol="T",
        uri="",
        mint=None,
        platform=Platform.PUMP_FUN,
    )
    ti.mint = mint  # type: ignore[attr-defined]
    return ti


if __name__ == "__main__":
    sys.exit(main())