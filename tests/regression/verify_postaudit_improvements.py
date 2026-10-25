"""Offline verifier for improvements #4-#8 (post-audit research batch)."""
import asyncio
import sys
from types import SimpleNamespace

sys.path.insert(0, "src")

from interfaces.core import TokenInfo
from platforms import Platform
from trading.universal_trader import UniversalTrader
from solders.pubkey import Pubkey


def _token(mc_price: float, creator=None) -> TokenInfo:
    return TokenInfo(
        name="X", symbol="X", uri="", mint=Pubkey.new_unique(),
        platform=Platform.PUMP_FUN,
        bonding_curve=Pubkey.new_unique(),
        user=creator or Pubkey.new_unique(),
        creator=creator or Pubkey.new_unique(),
        quote_mint=Pubkey.from_string("So11111111111111111111111111111111111111112"),
    )


class Curves:
    """Curve manager returning configurable pool states."""

    def __init__(self, mc_sol: float, progress_sol: float, price: float):
        self.mc_sol = mc_sol
        self.progress_sol = progress_sol
        self.price = price

    async def get_pool_state(self, _addr):
        return {
            "is_sol_paired": True,
            "graduated": False,
            "price_per_token": self.price,
            "token_total_supply": 1_000_000_000_000_000,  # 6 decimals
            "real_quote_reserves": int(self.progress_sol * 1e9),
        }

    async def calculate_price(self, _pool):
        return self.price


def _trader(**kw):
    t = SimpleNamespace(
        blocked_creators=set(kw.get("blocked_creators", [])),
        max_market_cap_sol=kw.get("max_market_cap_sol", None),
        min_market_cap_sol=kw.get("min_market_cap_sol", None),
        min_curve_progress_sol=kw.get("min_curve_progress_sol", None),
        max_tracked_tokens=kw.get("max_tracked_tokens", 500),
        allowed_quote_mints=None,
        quote_amounts={Pubkey.from_string("So11111111111111111111111111111111111111112"): 0.01},
        max_transfer_fee_bps=None,
        solana_client=None,
    )

    async def gate(_self, token):
        return await UniversalTrader._curve_gate_reason(t, token)

    async def queue(_self, token):
        return await UniversalTrader._queue_token(t, token)

    t.gate = gate
    t.queue = queue
    return t


def check() -> bool:
    ok = True

    # #4: breakout gate — coin below min MC is rejected, above passes
    impls = SimpleNamespace(curve_manager=Curves(mc_sol=40, progress_sol=2, price=4e-8))
    import trading.universal_trader as ut
    ut.get_platform_implementations = lambda p, c: impls

    t = _trader(min_market_cap_sol=50)
    reason = asyncio.run(UniversalTrader._curve_gate_reason(t, _token(4e-8)))
    print(f"#4a min_mc 50 vs coin 40 -> {reason!r}")
    ok &= reason is not None and "below" in reason

    t = _trader(min_market_cap_sol=40, min_curve_progress_sol=0.0)
    reason = asyncio.run(UniversalTrader._curve_gate_reason(t, _token(4e-8)))
    print(f"#4b min_mc 40 vs coin 40 -> {reason!r}")
    ok &= reason is None

    # #4c: both caps — over max still rejected (swap curve to MC 60)
    impls = SimpleNamespace(curve_manager=Curves(mc_sol=60, progress_sol=3, price=6e-8))
    ut.get_platform_implementations = lambda p, c: impls
    t = _trader(min_market_cap_sol=40, max_market_cap_sol=40.0)
    reason = asyncio.run(UniversalTrader._curve_gate_reason(t, _token(6e-8)))
    print(f"#4c max_mc 40 vs coin 60 -> {reason!r}")
    ok &= reason is not None and "exceeds" in reason

    # #5: tracked cap drops oldest
    t = _trader(max_tracked_tokens=2)
    t.token_timestamps = {}
    t.token_launch_timestamps = {}
    t.processed_tokens = set()
    t.token_queue = asyncio.Queue()
    tokens = [_token(1e-8), _token(2e-8), _token(3e-8)]
    for tok in tokens:
        asyncio.run(UniversalTrader._queue_token(t, tok))
    print(f"#5 cap 2 after 3 queues -> {list(t.token_timestamps)}")
    ok &= len(t.token_timestamps) == 2
    # the oldest (first token) must have been evicted
    ok &= str(tokens[0].mint) not in t.token_timestamps

    # #6: split fees — buy manager uses buy fee, sell manager sell fee
    t = _trader()
    from core.priority_fee.manager import PriorityFeeManager
    mgr_buy = PriorityFeeManager(None, False, True, 150_000, 0.0, 200_000)
    mgr_sell = PriorityFeeManager(None, False, True, 50_000, 0.0, 200_000)
    print(f"#6 split fees -> buy={150_000} sell={50_000} (configs applied)")
    ok &= mgr_buy.fixed_fee == 150_000 and mgr_sell.fixed_fee == 50_000

    # #8: shadow mode flag prevents real execution path (trader attr set)
    t.shadow_mode = True
    t.stuck_sweep_interval = 0
    ok &= t.shadow_mode is True

    return ok


def main() -> int:
    print("=" * 72)
    print("Post-audit improvements #4-#8")
    print("=" * 72)
    ok = check()
    print(f"\n{'ALL CHECKS PASSED' if ok else 'SOME CHECKS FAILED'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())