"""R3-P2-16 regression: real_token_reserves buy-output cap.

Covers (pumpdotfun-sdk semantics, bondingCurveAccount.ts:46-53):
- A near-completion curve (real token holdings far below the virtual quote)
  must never quote output beyond the real holdings: buy_amount_out caps at
  real_token_reserves when a positive reading is supplied.
- Normal curve: cap above the raw quote is a no-op (quote unchanged).
- 0/absent real reading (decode hiccup / completed curve) keeps the legacy
  uncapped behavior — the graduated gate handles completed curves.
- calculate_buy_amount_out wires the snapshot's positive real_token_reserves
  into the capped quote.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from platforms.pumpfun.curve_manager import (  # noqa: E402
    buy_amount_out,
    DEFAULT_PUMP_FEE_BPS,
)


def main() -> int:
    failures = []

    def check(label: str, passed: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            failures.append(label)

    print("R3-P2-16 real_token_reserves buy cap")

    # Near-completion curve: virtual quote says thousands of tokens, but the
    # curve physically holds only a few hundred — the cap must bind.
    vt = 1_000_000_000_000  # virtual tokens
    vs = 30_000_000_000  # virtual SOL lamports
    amt = 100_000_000  # 0.1 SOL
    uncapped = buy_amount_out(vt, vs, amt, DEFAULT_PUMP_FEE_BPS)
    capped = buy_amount_out(
        vt, vs, amt, DEFAULT_PUMP_FEE_BPS, real_token_reserves=500_000_000
    )
    check(
        "near-completion quote capped at real holdings",
        capped == 500_000_000 and capped < uncapped,
        f"-> capped={capped} uncapped={uncapped}",
    )

    # Normal curve: real holdings well above the quote — cap is a no-op.
    roomy = buy_amount_out(
        vt, vs, amt, DEFAULT_PUMP_FEE_BPS, real_token_reserves=10**18
    )
    check(
        "normal curve unchanged when cap is above quote",
        roomy == uncapped,
        f"-> {roomy}",
    )

    # 0/absent real reading -> legacy uncapped behavior.
    zero_cap = buy_amount_out(vt, vs, amt, DEFAULT_PUMP_FEE_BPS, real_token_reserves=0)
    none_cap = buy_amount_out(vt, vs, amt, DEFAULT_PUMP_FEE_BPS, real_token_reserves=None)
    check(
        "0 real reading keeps legacy behavior",
        zero_cap == uncapped and none_cap == uncapped,
    )

    # calculate_buy_amount_out passes the positive snapshot reading through.
    from unittest.mock import AsyncMock
    import platforms.pumpfun.curve_manager as cm
    import asyncio

    mgr = object.__new__(cm.PumpFunCurveManager)
    mgr.get_pool_state = AsyncMock(
        return_value={
            "virtual_token_reserves": vt,
            "virtual_sol_reserves": vs,
            "real_token_reserves": 500_000_000,
            "fee_bps": DEFAULT_PUMP_FEE_BPS,
        }
    )
    out = asyncio.run(mgr.calculate_buy_amount_out("pool", amt))
    check(
        "quote path caps at snapshot real_token_reserves",
        out == 500_000_000,
        f"-> {out}",
    )

    if failures:
        print(f"\nR3-P2-16 FAILED ({len(failures)}): {failures}")
        return 1
    print("\nR3-P2-16 real_token_reserves buy cap OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())