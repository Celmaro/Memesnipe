"""Offline verifier for the copy-trader qualification engine (Phase 7).

Covers the audit Part 5 starting rules: trade-count floor, net-PnL windows,
24h freshness, entry drift, and independent position sizing.
"""
import sys

sys.path.insert(0, "src")

from copy_trader.qualification import (
    CopySignal,
    LeaderProfile,
    LeaderVerdict,
    check_entry_drift,
    qualify_leader,
    size_follow_position,
)


def _profiled_profile() -> LeaderProfile:
    p = LeaderProfile(address="wallet1", last_seen_days_ago=0.2)
    p.window(30).closed_trades = 150
    p.window(30).net_pnl_sol = 1.5
    p.window(7).closed_trades = 40
    p.window(7).net_pnl_sol = 0.4
    return p


def check() -> bool:
    ok = True

    # 1. Qualified leader
    verdict = qualify_leader(_profiled_profile())
    print(f"1. baseline qualified wallet -> {verdict.value}")
    ok &= verdict is LeaderVerdict.QUALIFIED

    # 2. Too few closed trades in 30d
    p = _profiled_profile()
    p.window(30).closed_trades = 10
    verdict = qualify_leader(p)
    print(f"2. 10 trades in 30d -> {verdict.value}")
    ok &= verdict is LeaderVerdict.INSUFFICIENT_TRADES

    # 3. Negative 7d net despite positive 30d
    p = _profiled_profile()
    p.window(7).net_pnl_sol = -0.05
    verdict = qualify_leader(p)
    print(f"3. negative 7d -> {verdict.value}")
    ok &= verdict is LeaderVerdict.NEGATIVE_NET

    # 4. Stale: no activity in 24h
    p = _profiled_profile()
    p.last_seen_days_ago = 2.0
    verdict = qualify_leader(p)
    print(f"4. stale 2d -> {verdict.value}")
    ok &= verdict is LeaderVerdict.STALE_24H

    # 5. Entry drift within 8% -> qualified
    sig = CopySignal(
        leader="wallet1",
        mint="abc",
        leader_entry_price_sol=0.0001,
        current_price_sol=0.000107,
    )
    verdict = check_entry_drift(sig)
    print(f"5. drift +7% -> {verdict.value}")
    ok &= verdict is LeaderVerdict.QUALIFIED

    # 6. Entry drift beyond 8% -> rejected
    sig = CopySignal(
        leader="wallet1",
        mint="abc",
        leader_entry_price_sol=0.0001,
        current_price_sol=0.00012,
    )
    verdict = check_entry_drift(sig)
    print(f"6. drift +20% -> {verdict.value}")
    ok &= verdict is LeaderVerdict.DRIFT_TOO_FAR

    # 7. Independent sizing caps exposure and ignores leader size
    size = size_follow_position(
        budget_sol=0.01, leader_bought_sol=5.0, existing_exposure_sol=0.0
    )
    print(f"7. fresh copy sized -> {size:.4f} SOL (budget 0.01)")
    ok &= abs(size - 0.01) < 1e-9

    # 8. Exposure cap: existing exposure already at cap -> no entry
    size = size_follow_position(
        budget_sol=0.01, leader_bought_sol=5.0, existing_exposure_sol=0.1
    )
    print(f"8. exposure at cap -> {size:.4f} SOL (want 0)")
    ok &= size == 0.0

    # 9. Sizing respects remaining room under the cap
    size = size_follow_position(
        budget_sol=0.05, leader_bought_sol=9.0, existing_exposure_sol=0.06
    )
    print(f"9. partial room -> {size:.4f} SOL (cap 0.1, room 0.04)")
    ok &= abs(size - 0.04) < 1e-9

    return ok


def main() -> int:
    print("=" * 72)
    print("Copy-trader qualification engine (Phase 7)")
    print("=" * 72)
    ok = check()
    print(f"\n{'ALL CHECKS PASSED' if ok else 'SOME CHECKS FAILED'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())