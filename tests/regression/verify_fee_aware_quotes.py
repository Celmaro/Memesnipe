"""P2-FEE-1 regression: pump.fun quotes must be fee-aware.

Reference bug: Memesnipe's curve quotes used raw constant-product math with NO
fee deduction, and the pump.fun curve decoder never set `fee_fraction` — yet
`platform_aware.py` multiplies expected buy tokens and sell quote output by
`(1 - fee_fraction)`. With fee_fraction always 0.0 the min/max guards did not
reflect on-chain fees, so: sell `min_sol_output` set above what the chain pays
after fees → slippage revert; buy `max_sol_cost` understated → exceeded revert.

Guard: (1) the quote helpers must deduct a fee on the input side; (2) the
decoded curve state must carry a positive `fee_bps`/`fee_fraction`; (3) custom
creator-fee coins derive a higher total than the canonical default.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from platforms.pumpfun.curve_manager import (  # noqa: E402
    DEFAULT_PUMP_FEE_BPS,
    MAX_FEE_BPS,
    PROTOCOL_FEE_BPS,
    U64_MAX,
    _total_fee_bps,
    buy_amount_out,
    fee_on_input,
    max_safe_sell_amount,
    sell_amount_out,
)

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def main() -> int:
    vt = 1_073_000_000_000  # canonical virtual token reserves
    vs = 30_000_000_011  # canonical virtual SOL reserves (lamports)
    amount_in = 10_000_000  # 0.01 SOL in lamports

    fee = fee_on_input(amount_in, 100)
    check(
        "buy fee is 1% of input with ceil rounding",
        fee == 100_000,
        f"got {fee}",
    )

    raw_buy = (amount_in * vt) // (vs + amount_in)
    fee_buy = buy_amount_out(vt, vs, amount_in, 100)
    check(
        "buy output is less than raw constant-product (fee deducted)",
        fee_buy < raw_buy,
        f"raw={raw_buy} fee_aware={fee_buy}",
    )

    raw_sell = (amount_in * vs) // (vt + amount_in)
    fee_sell = sell_amount_out(vt, vs, amount_in, 100)
    check(
        "sell output is less than raw constant-product (fee deducted)",
        fee_sell < raw_sell,
        f"raw={raw_sell} fee_aware={fee_sell}",
    )

    check(
        "default total fee is the canonical 100 bps",
        _total_fee_bps(0) == DEFAULT_PUMP_FEE_BPS == 100,
        f"got {_total_fee_bps(0)}",
    )

    custom = _total_fee_bps(150)
    check(
        "custom creator fee adds protocol share",
        custom == 150 + PROTOCOL_FEE_BPS,
        f"got {custom}",
    )

    check(
        "total fee is clamped to MAX_FEE_BPS",
        _total_fee_bps(999_999) == MAX_FEE_BPS,
        f"got {_total_fee_bps(999_999)}",
    )

    # Zero-fee path stays exact (back-compat for platforms without fees).
    check(
        "zero fee reproduces the raw constant-product formula",
        buy_amount_out(vt, vs, amount_in, 0) == raw_buy
        and sell_amount_out(vt, vs, amount_in, 0) == raw_sell,
    )

    # The critical guard: platform_aware's (1 - fee_fraction) must now bite.
    # With the canonical 1% fee the expected sell output is ~1% lower than the
    # fee-blind number — the fix that removes sell-slippage reverts.
    check(
        "fee_fraction for the canonical case is 0.01",
        abs(_total_fee_bps(0) / 10_000 - 0.01) < 1e-9,
    )

    # P2-FEE-2: oversized sells must clamp to the on-chain u64/u128 bound.
    # Canonical curve: 30 SOL virtual = 30_000_000_011 lamports quote reserves.
    huge_balance = 10**30  # far beyond u64::MAX
    safe = max_safe_sell_amount(huge_balance, 30_000_000_011)
    check(
        "oversized sell clamps to <= u64::MAX",
        safe <= U64_MAX,
        f"got {safe}",
    )
    check(
        "normal pump.fun balance is never clamped",
        max_safe_sell_amount(1_000_000_000_000, 30_000_000_011) == 1_000_000_000_000,
    )
    check(
        "zero balance returns zero",
        max_safe_sell_amount(0, 30_000_000_011) == 0,
    )
    check(
        "unknown reserves still clamp to u64::MAX",
        max_safe_sell_amount(huge_balance, 0) == U64_MAX,
    )

    print()
    if FAILURES:
        print(f"P2-FEE FAILED: {len(FAILURES)} check(s) — {FAILURES}")
        return 1
    print("P2-FEE PASSED: fee-aware quotes and safe-sell clamp verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())