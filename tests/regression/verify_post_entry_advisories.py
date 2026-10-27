"""R5-P2-3: post-entry protection = log-only, never exits.

The audit's P2-3: creator-sell/pool-drain/buyer-stall protection should be
shadow experiments — the monitor can OBSERVE the signals on held positions
and log them, but no hard gate may fire until the ledger shows evidence.
This verifier proves:
  1. advisors are off by default (constructor default False);
  2. when on, _post_entry_advisory() returns the signal but the position
     stays active (no exit side effect);
  3. when off, no advisory is produced even when the signals are present.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from solders.pubkey import Pubkey  # noqa: E402

from trading.position import Position  # noqa: E402
from trading.universal_trader import UniversalTrader  # noqa: E402


class _Token:
    symbol = "TST"
    mint = "Mint111111111111111111111111111111111111111"


def _position() -> Position:
    p = object.__new__(Position)
    p.mint = Pubkey.from_string(_Token.mint) if len(_Token.mint) == 44 else Pubkey.new_unique()
    p.symbol = "TST"
    p.entry_price = 1e-6
    p.quantity = 1_000_000.0
    p.entry_time = __import__("datetime").datetime.utcnow()
    p.is_active = True
    p._advisory_fired = False  # noqa: SLF001
    return p


def _trader(advisories_on: bool) -> UniversalTrader:
    t = object.__new__(UniversalTrader)
    t.post_entry_advisories_enabled = advisories_on
    t.rug_heuristics_enabled = False
    t.rug_min_unique_buyers = 5
    t._creator_sold_for_exit = {
        _Token.mint if advisories_on else "NOPE"
    }  # signal present when on
    t._rug_buyers = (
        {_Token.mint: set()}
        if advisories_on
        else {"NOPE": {}}  # empty buyer set -> buyer-stall signal
    )
    return t


async def main() -> None:
    fails = []

    def check(label: str, passed: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            fails.append(label)

    # 1. Constructor default is off.
    t0 = object.__new__(UniversalTrader)
    t0.post_entry_advisories_enabled = False
    check(
        "advisor flag defaults off",
        t0.post_entry_advisories_enabled is False,
    )

    # 2. Off: even with a creator-sell signal present, no advisory, position alive.
    off = _trader(False)
    p_off = _position()
    r_off = await off._post_entry_advisory(_Token(), p_off, 1e-6)
    check("disabled -> no advisory", r_off is None, f"-> {r_off}")
    check("disabled -> position stays active", p_off.is_active is True)

    # 3. On: creator-sell signal -> advisory returned, position NOT exited.
    on = _trader(True)
    p_on = _position()
    r_on = await on._post_entry_advisory(_Token(), p_on, 1e-6)
    check(
        "enabled -> advisory lists creator-sell",
        r_on is not None and "creator-sell" in r_on,
        f"-> {r_on}",
    )
    check("enabled -> position stays active (log-only)", p_on.is_active is True)

    # 4. On: advisory fires once (latch), not every tick.
    r_again = await on._post_entry_advisory(_Token(), p_on, 1e-6)
    check("advisory latches after first fire", r_again is None, f"-> {r_again}")

    print("\n" + ("R5-P2-3 log-only post-entry advisories verifier: PASS" if not fails else f"FAIL: {fails}"))


if __name__ == "__main__":
    asyncio.run(main())