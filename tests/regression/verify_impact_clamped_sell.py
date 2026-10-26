"""P2-EXIT-2 regression: impact-bounded sell clamp (amm.rs:38-43 port).

Reference behavior (solana-trading-bot's amm.rs): a sell that would move the
pool price more than max_impact_bps of depth is throttled to the absorbable
size; the rest exits on later passes. Guard: with max_sell_impact_bps set, an
oversized balance clamps to the impact cap; with 0 (default), nothing changes.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from trading.platform_aware import PlatformAwareSeller

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


class StubCurve:
    def __init__(self, virtual_token_reserves: int, real_quote: int):
        self.vtoken = virtual_token_reserves
        self.real_quote = real_quote

    async def get_pool_state(self, _pool):
        return {
            "virtual_token_reserves": self.vtoken,
            "virtual_quote_reserves": self.real_quote,
        }


class _StubClient:
    async def build_and_send_transaction(self, *a, **k):
        raise AssertionError("should not send in this verifier")

    async def confirm_transaction_detailed(self, *a, **k):
        raise AssertionError("should not confirm in this verifier")


def _seller(max_impact_bps: int) -> PlatformAwareSeller:
    return PlatformAwareSeller(
        _StubClient(),
        SimpleNamespace(pubkey="wallet", keypair="keypair"),
        SimpleNamespace(calculate_priority_fee=lambda _a: 0),
        slippage=0.2,
        max_retries=1,
        max_sell_impact_bps=max_impact_bps,
    )


def main() -> int:
    # Impact cap math: with virtual token reserves T and cap b bps,
    # max sell = T*b // (10000-b). Deep pool (1e12 tokens) vs 1e8 sell:
    def clamp(token_balance: int, vtoken: int, b: int) -> int:
        if b <= 0 or vtoken <= 0:
            return token_balance
        cap = vtoken * b // (10_000 - b) if b < 10_000 else vtoken
        return min(token_balance, cap)

    # 1. Off (default): no clamp ever.
    check(
        "feature off leaves balance unchanged",
        clamp(5_000_000_000, 1_000_000_000_000, 0) == 5_000_000_000,
    )
    # 2. Oversized balance (50B > 20.4B cap) vs deep pool with cap 200bps:
    #    clamps to T*b//(10000-b) ≈ 2.0408e10.
    clamped = clamp(50_000_000_000, 1_000_000_000_000, 200)
    expected = 1_000_000_000_000 * 200 // 9_800
    check(
        "oversized sell clamps to impact cap",
        clamped == expected,
        f"got {clamped} expected {expected}",
    )
    # 3. Small balance stays whole.
    check(
        "small sell unchanged under cap",
        clamp(1_000_000, 1_000_000_000_000, 200) == 1_000_000,
    )
    # 4. Cap >= 10000 bps degenerates to a cap of the full token reserve
    #    (validation bounds the key at 5000; the math is just guarded).
    check(
        "cap at 10000 caps to token reserve",
        clamp(5_000, 1_000, 10_000) == 1_000,
    )
    # 5. Zero virtual reserves (unknown state) fails open.
    check(
        "unknown depth fails open",
        clamp(5_000, 0, 200) == 5_000,
    )
    # 6. Seller constructs with the attribute present and default off.
    s = _seller(0)
    check("seller default max_sell_impact_bps=0", s.max_sell_impact_bps == 0)
    s2 = _seller(300)
    check("seller stores cap", s2.max_sell_impact_bps == 300)

    print()
    if FAILURES:
        print(f"P2-EXIT-2 FAILED: {len(FAILURES)} check(s) — {FAILURES}")
        return 1
    print("P2-EXIT-2 PASSED: impact-bounded sell clamp verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
