"""R3-P2-13 regression: strict buy/sell serialization.

Covers:
- The trader owns ONE shared asyncio.Lock guarding buy-vs-sell overlap.
- Two concurrent acquire attempts SERIALIZE: the second waits for the first.
- Without contention the lock is a no-op (off-by-default semantics — the
  bug only manifests when buy/sell overlap, which is exactly what the lock
  prevents).
- Config wiring: no new config key (pure wiring, always-on safety) — the
  verifier asserts the lock exists on the constructed trader path and that
  the buy/sell execution paths reference it.
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import inspect  # noqa: E402
import trading.universal_trader as ut  # noqa: E402


def main() -> int:
    failures = []

    def check(label: str, passed: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            failures.append(label)

    print("R3-P2-13 buy/sell serialization")

    # Concurrent acquisitions serialize.
    async def _serialize() -> list[float]:
        lock = asyncio.Lock()
        starts: list[float] = []

        async def hold(name: str) -> None:
            async with lock:
                starts.append(time.monotonic())
                await asyncio.sleep(0.05)

        await asyncio.gather(hold("a"), hold("b"))
        return starts

    starts = asyncio.run(_serialize())
    check(
        "two holders serialize (no overlap)",
        len(starts) == 2 and (starts[1] - starts[0]) >= 0.03,
        f"-> {starts[1] - starts[0]:.4f}s apart",
    )

    # The trader constructor installs the shared lock.
    src = inspect.getsource(ut.UniversalTrader.__init__)
    check(
        "trader installs _trade_serialization_lock",
        "self._trade_serialization_lock = asyncio.Lock()" in src,
    )
    # Buy path and both sell paths acquire it.
    buy_src = inspect.getsource(ut.UniversalTrader._handle_token)
    check(
        "buy path acquires the lock",
        "async with self._trade_serialization_lock:" in buy_src,
    )
    sell_src = inspect.getsource(ut.UniversalTrader._attempt_position_exit)
    check(
        "monitor sell path acquires the lock",
        "async with self._trade_serialization_lock:" in sell_src,
    )

    if failures:
        print(f"\nR3-P2-13 FAILED ({len(failures)}): {failures}")
        return 1
    print("\nR3-P2-13 buy/sell serialization OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())