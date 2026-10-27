"""P2-EXIT-1 regression: absolute depth-floor forces a liquidity exit.

The reference (ssp-protocol deploy_backstop) fires when pool depth drops below
a floor purely because the pool cannot absorb sell pressure. Guard: with
min_pool_depth_sol set, a pool whose depth is below the floor must trigger
ExitReason.LIQUIDITY; with the feature off (default), nothing changes.
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from trading.position import ExitReason  # noqa: E402
from trading.universal_trader import UniversalTrader  # noqa: E402

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


class StubCurve:
    def __init__(self, depth_lamports: int):
        self.depth_lamports = depth_lamports

    async def get_pool_state(self, _pool):
        return {"real_quote_reserves": self.depth_lamports}


class StubSeller:
    def __init__(self):
        self.sells = 0

    async def execute(self, *a, **k):
        self.sells += 1
        from trading.base import TradeResult

        return TradeResult(success=True, platform=a[0].platform)


def _trader(curve: StubCurve, min_depth: float) -> UniversalTrader:
    trader = object.__new__(UniversalTrader)
    trader._momentum_peak_rate = {}  # R4-P1-8 mock state
    trader._momentum_peak_price = {}  # R4-P1-8 mock state
    trader.fragility_scale_min = 1.0  # R4-P3-1 mock default (off)
    trader.fragility_whale_threshold = 0.6  # R4-P3-1 mock default
    trader.fragility_rate_threshold = 0.6  # R4-P3-1 mock default
    trader.stagnation_exit_min = 0  # R4-P1-9 mock default (off)
    trader.stagnation_band_pct = 0.02  # R4-P1-9 mock default
    trader.max_wash_share = 0.0  # R4-P1-6 mock default
    from screening.wash_lpi import WashLpiDetector as _WLD  # R4-P1-6 mock default
    trader._wash_lpi = _WLD()  # R4-P1-6 mock default
    trader.max_gini_coefficient = 0.0  # R4-P1-10 mock default
    trader.rug_score_threshold = 0.0  # R4-P1-1 mock default
    trader.factory_deploy_rate = 0.0  # R4-P1-2 mock default
    trader.max_coordinated_supply_share = 0.0  # R4-P1-3 mock default
    trader.exit_mode = "tp_sl"  # R4-P1-7 mock default
    trader.strategies_ledger = None  # R4-P1-7 mock default
    trader.allow_mayhem_mode = False  # R3-P0-2 off in verifiers
    trader.min_pool_depth_sol = min_depth
    trader.platform_implementations = SimpleNamespace(
        curve_manager=curve,
        address_provider=SimpleNamespace(
            derive_pool_address=lambda _mint: _mint
        ),
    )
    trader.seller = StubSeller()
    trader.solana_client = None
    trader.wallet = None
    trader.priority_fee_manager = None
    trader.max_exit_sell_attempts = 3
    trader.cleanup_mode = "disabled"
    trader._log_trade = lambda *a, **k: None  # noqa: SLF001
    trader._log_attempt = lambda *a, **k: None  # noqa: SLF001
    trader._unsold_positions = {}
    trader._unsold_watcher_task = None
    trader.persistent_exit_ttl = 0
    trader.persistent_exit_interval = 0
    return trader


def main() -> int:
    from solders.pubkey import Pubkey

    from interfaces.core import Platform, TokenInfo

    token = TokenInfo(
        name="Depth", symbol="DPT", uri="", mint=Pubkey.default(),
        platform=Platform.PUMP_FUN,
    )

    # Shallow pool (0.005 SOL = 5_000_000 lamports) with floor 0.01 SOL.
    curve = StubCurve(5_000_000)
    trader = _trader(curve, min_depth=0.01)
    reason = asyncio.run(trader._check_depth_floor(token))
    check(
        "shallow pool returns a depth reason",
        reason is not None and "pool depth" in reason,
        f"got {reason}",
    )

    # Deep pool (1 SOL) with the same floor.
    curve2 = StubCurve(1_000_000_000)
    trader2 = _trader(curve2, min_depth=0.01)
    check(
        "deep pool returns no reason",
        asyncio.run(trader2._check_depth_floor(token)) is None,
    )

    # Feature off (default): never returns a reason.
    trader3 = _trader(StubCurve(100), min_depth=0.0)
    check(
        "feature off never forces exit",
        asyncio.run(trader3._check_depth_floor(token)) is None,
    )

    # Unknown depth (graduated / unmappable) fails open.
    trader4 = _trader(StubCurve(0), min_depth=0.01)
    check(
        "zero/unknown depth fails open",
        asyncio.run(trader4._check_depth_floor(token)) is None,
    )

    # End-to-end: the LIQUIDITY reason is a real enum member the monitor
    # loop can raise.
    check(
        "LIQUIDITY exit reason exists",
        ExitReason.LIQUIDITY.value == "liquidity_depth",
    )

    print()
    if FAILURES:
        print(f"P2-EXIT-1 FAILED: {len(FAILURES)} check(s) — {FAILURES}")
        return 1
    print("P2-EXIT-1 PASSED: depth-floor forced exit verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())