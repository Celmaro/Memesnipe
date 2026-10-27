"""R3-P2-1 regression: pre-buy risk gate (synchronous snapshot, no hot-path RPC).

The gate contract:
- _skip_reason MUST be a local-only lookup for creator-PnL (SQLite), holder
  concentration (snapshot store) — no RPC.
- _curve_gate_reason's honeypot check MUST reuse a warm cached verdict
  instead of calling honeypot_reason (which RPCs a mint read) on every gate.
- A stale/missing honeypot cache falls back to the live check (fail-safe);
  a cached pass verdict short-circuits to None without RPC.
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import trading.universal_trader as ut  # noqa: E402
from trading.universal_trader import UniversalTrader  # noqa: E402

MINT = "Mint111111111111111111111111111111111111111"
CREATOR = "Creator111111111111111111111111111111111"


def _token() -> SimpleNamespace:
    from core.pubkeys import DEFAULT_PUBKEY

    return SimpleNamespace(
        platform="pump_fun",
        creator=CREATOR,
        user=None,
        quote_mint=DEFAULT_PUBKEY,
        transfer_fee_bps=0,
        mint=MINT,
        bonding_curve="pooladdr",
        pool_state=None,
        associated_bonding_curve=None,
        symbol="TST",
    )


def _trader() -> UniversalTrader:
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
    t.grad_timing_enabled = False  # R4-P1-4 mock
    t._grad_band_stats = {}  # R4-P1-4 mock
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
    t._holder_store = HolderSnapshotStore()  # noqa: SLF001
    from screening.metadata_socials import MetadataStore
    t.min_metadata_extensions = 0  # R3-P2-8 off in verifiers
    t._metadata_store = MetadataStore()  # noqa: SLF001
    t.honeypot_freeze_authority = True
    t.min_curve_hold_fraction = 0.0
    t._honeypot_cache = {}  # noqa: SLF001
    t._honeypot_cache_ttl = 30.0
    t.fail_closed_gates = True
    t.solana_client = None
    t.max_market_cap_sol = 40.0  # non-None so the gate reaches honeypot
    t.min_curve_progress_sol = None
    t.min_market_cap_sol = None
    return t


async def _probe_honeypot_caching() -> list[str]:
    failures = []

    def check(label: str, passed: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            failures.append(label)

    calls = {"n": 0}

    class _Curve:
        def __init__(self):
            self.count = 0

        async def get_pool_state(self, _addr):
            self.count += 1
            return {
                "is_sol_paired": True,
                "graduated": False,
                "price_per_token": 1e-9,
                "real_quote_reserves": 5_000_000_000,
                "virtual_quote_reserves": 30_000_000_000,
                "virtual_token_reserves": 1_073_000_000_000_000,
                "token_total_supply": 1_073_000_000_000_000,
                "fee_bps": 100,
                "creator": CREATOR,
                "is_mayhem_mode": False,
                "is_holder_reward": False,
                "is_cashback_coin": False,
                "quote_mint": None,
            }

    curve = _Curve()

    async def fake_honeypot(*_a, **_k):
        calls["n"] += 1
        return "freeze authority set"

    # Patch the module-level functions the gate calls.
    original = ut.honeypot_reason
    ut.honeypot_reason = fake_honeypot
    original_platforms = ut.get_platform_implementations
    ut.get_platform_implementations = lambda *_a, **_k: SimpleNamespace(
        curve_manager=curve
    )
    try:
        t = _trader()
        t.platform_implementations = SimpleNamespace(curve_manager=curve)

        # First gate: no cache -> calls honeypot once, caches the verdict.
        r1 = await t._curve_gate_reason(_token())  # noqa: SLF001
        check("first gate runs honeypot check", calls["n"] == 1 and r1 == "freeze authority set", f"-> {r1}")

        # Second gate: warm cache -> ZERO additional RPC (fake not called).
        r2 = await t._curve_gate_reason(_token())  # noqa: SLF001
        check(
            "warm cache gate = zero RPC",
            calls["n"] == 1 and r2 == "freeze authority set",
            f"-> calls={calls['n']} r2={r2}",
        )

        # A cached PASS (None verdict) short-circuits without RPC.
        t2 = _trader()
        calls2 = {"n": 0}

        async def _no_honeypot_call(*_a, **_k):
            calls2["n"] += 1
            return None

        ut.honeypot_reason = _no_honeypot_call
        t2.platform_implementations = SimpleNamespace(curve_manager=curve)
        # Warm pass verdict stamped NOW (monotonic seconds since boot, so a
        # literal 0.0 would look 12k+ seconds stale).
        import time as _time

        t2._honeypot_cache[MINT] = (_time.monotonic(), None)  # noqa: SLF001
        r3 = await t2._curve_gate_reason(_token())  # noqa: SLF001
        check(
            "warm cached pass -> no RPC, gate passes",
            calls2["n"] == 0 and r3 is None,
            f"-> calls={calls2['n']} r3={r3}",
        )
    finally:
        ut.honeypot_reason = original
        ut.get_platform_implementations = original_platforms
    return failures


def _probe_skip_reason_locality() -> list[str]:
    failures = []

    def check(label: str, passed: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            failures.append(label)

    # _skip_reason must not touch solana_client at all (pure local lookups).
    t = _trader()
    t.solana_client = _ExplodingClient()
    r = t._skip_reason(_token())  # noqa: SLF001
    check("_skip_reason with exploding RPC client still runs", r is None, f"-> {r!r}")
    return failures


class _ExplodingClient:
    def __getattr__(self, _name):
        raise AssertionError("gate must not touch RPC on the hot path")


def main() -> int:
    failures = _probe_skip_reason_locality()
    failures += asyncio.run(_probe_honeypot_caching())

    if failures:
        print(f"\nR3-P2-1 FAILED ({len(failures)}): {failures}")
        return 1
    print("\nR3-P2-1 pre-buy risk gate OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())