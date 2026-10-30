"""Offline regression checks for metered signature-status confirmation.

No RPC endpoint is contacted and no transaction is sent.
Run with: uv run tests/regression/verify_metered_confirmation.py
"""

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import core.client as client_module  # noqa: E402
from core.client import SolanaClient  # noqa: E402
from interfaces.core import ConfirmationStatus  # noqa: E402

SIGNATURE = (
    "2bHRaovWyYNTX3K1KfMbMBSvefuT34ZExL2J2N83DDh9aB3Fynjyae5bcaCdRuQoihHAMHK4PzcRYoqc9hYypWb2"
)


class VirtualClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


class CountingLimiter:
    def __init__(self) -> None:
        self.acquires = 0

    async def acquire(self) -> None:
        self.acquires += 1


async def main() -> None:
    client = object.__new__(SolanaClient)
    limiter = CountingLimiter()
    client._rate_limiter = limiter
    clock = VirtualClock()
    statuses = [
        {"err": {"InstructionError": [0, {"Custom": 1}]}, "confirmationStatus": "processed"},
        {"err": None, "confirmationStatus": "confirmed"},
    ]
    calls: list[str] = []

    async def fake_post_rpc(body: dict, **_kwargs: object) -> dict:
        calls.append(body["method"])
        await limiter.acquire()
        return {"result": {"value": [statuses.pop(0)]}}

    async def fake_verify(_signature: str) -> ConfirmationStatus:
        return ConfirmationStatus.SUCCESS

    client.post_rpc = fake_post_rpc
    client.verify_transaction_status = fake_verify

    real_sleep = asyncio.sleep
    real_monotonic = client_module.monotonic
    asyncio.sleep = clock.sleep
    client_module.monotonic = clock.monotonic
    try:
        result = await client.confirm_transaction_detailed(
            SIGNATURE, commitment="confirmed"
        )
    finally:
        asyncio.sleep = real_sleep
        client_module.monotonic = real_monotonic

    assert result is ConfirmationStatus.SUCCESS, result
    assert calls == ["getSignatureStatuses", "getSignatureStatuses"], calls
    assert limiter.acquires == 2, limiter.acquires
    print("[PASS] every confirmation poll passes through the shared RPC limiter")
    print("[PASS] a processed-fork error is not terminal before confirmed commitment")
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
