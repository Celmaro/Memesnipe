"""Regression checks for bounded Pump.fun curve-state cache.

Offline only. No network or wallet is used.
Run with: uv run tests/regression/verify_curve_state_cache_bounds.py
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from solders.pubkey import Pubkey  # noqa: E402

from platforms.pumpfun.curve_manager import PumpFunCurveManager  # noqa: E402


class StubClient:
    def __init__(self) -> None:
        self.calls = 0

    async def get_account_info(self, _address: Pubkey, commitment=None):
        self.calls += 1
        return SimpleNamespace(data=b"curve")


async def main() -> None:
    client = StubClient()
    manager = PumpFunCurveManager(client, idl_parser=None)
    manager._POOL_STATE_CACHE_MAX_ENTRIES = 3
    manager._decode_curve_state_with_idl = lambda _data: {
        "is_sol_paired": False,
        "virtual_quote_reserves": 1,
    }

    keys = [Pubkey.from_bytes(bytes([i]) * 32) for i in range(1, 6)]
    first = await manager.get_pool_state(keys[0])
    second = await manager.get_pool_state(keys[0])
    assert first == second
    assert client.calls == 1, client.calls
    print("[PASS] warm curve read reuses the cached state")

    for key in keys[1:]:
        await manager.get_pool_state(key)

    assert len(manager._state_cache) <= 3, len(manager._state_cache)
    assert str(keys[0]) not in manager._state_cache, list(manager._state_cache)
    print("[PASS] cache remains within configured LRU capacity")
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
