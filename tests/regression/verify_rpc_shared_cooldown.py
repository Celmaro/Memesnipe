"""Regression checks for shared RPC cooldown and confirmation metering.

Offline only: no chain connection, transaction, wallet key, or provider required.
Run with: uv run tests/regression/verify_rpc_shared_cooldown.py
"""

import asyncio
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from core.rpc_rate_limiter import TokenBucketRateLimiter


async def check_cooldown_pauses_all_callers() -> bool:
    limiter = TokenBucketRateLimiter(max_rps=100.0, burst_size=1)
    await limiter.acquire()  # consume the only initial burst token
    await limiter.defer(0.10)

    started = time.monotonic()
    await asyncio.gather(limiter.acquire(), limiter.acquire())
    elapsed = time.monotonic() - started

    passed = elapsed >= 0.095
    print(
        f"[{'PASS' if passed else 'FAIL'}] shared cooldown pauses concurrent callers "
        f"(elapsed={elapsed:.3f}s, required>=0.095s)"
    )
    return passed


async def check_cooldown_only_extends() -> bool:
    limiter = TokenBucketRateLimiter(max_rps=100.0, burst_size=1)
    await limiter.acquire()
    await limiter.defer(0.05)
    await asyncio.sleep(0.01)
    await limiter.defer(0.10)

    started = time.monotonic()
    await limiter.acquire()
    elapsed = time.monotonic() - started
    passed = elapsed >= 0.095
    print(
        f"[{'PASS' if passed else 'FAIL'}] later 429 cooldown extends shared pause "
        f"(elapsed={elapsed:.3f}s, required>=0.095s)"
    )
    return passed


async def check_cooldown_does_not_release_a_full_burst() -> bool:
    limiter = TokenBucketRateLimiter(max_rps=10.0, burst_size=10)
    await limiter.acquire()
    await limiter.defer(0.05)

    started = time.monotonic()
    await asyncio.gather(*(limiter.acquire() for _ in range(10)))
    elapsed = time.monotonic() - started
    passed = elapsed >= 0.85
    print(
        f"[{'PASS' if passed else 'FAIL'}] cooldown does not release a full bucket "
        f"(elapsed={elapsed:.3f}s, required>=0.850s)"
    )
    return passed


async def main() -> None:
    results = [
        await check_cooldown_pauses_all_callers(),
        await check_cooldown_only_extends(),
        await check_cooldown_does_not_release_a_full_burst(),
    ]
    print(
        f"\n{'ALL CHECKS PASSED' if all(results) else 'SOME CHECKS FAILED'} "
        f"({len(results) - results.count(True)} failed)"
    )
    if not all(results):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
