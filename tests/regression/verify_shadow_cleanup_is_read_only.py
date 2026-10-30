"""Verify shadow exits never submit real ATA-cleanup transactions."""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import trading.exit_engine as exit_engine  # noqa: E402
from trading.exit_engine import ExitEngineMixin  # noqa: E402


async def main() -> None:
    calls: list[tuple] = []

    async def fake_cleanup(*args) -> None:
        calls.append(args)

    original = exit_engine.handle_cleanup_after_sell
    exit_engine.handle_cleanup_after_sell = fake_cleanup
    try:
        trader = object.__new__(ExitEngineMixin)
        trader.shadow_mode = True
        trader.solana_client = object()
        trader.wallet = object()
        trader.priority_fee_manager = object()
        trader.cleanup_mode = "after_sell"
        trader.cleanup_with_priority_fee = True
        trader.cleanup_force_close_with_burn = False
        cleared: list[str] = []
        trader._clear_mint_tracking_state = cleared.append
        token = SimpleNamespace(mint="mint", token_program_id="token-program")

        await ExitEngineMixin._cleanup_after_exit(trader, token)
        assert not calls, calls
        assert cleared == ["mint"], cleared
        print("[PASS] shadow exit performs no ATA cleanup transaction")

        trader.shadow_mode = False
        await ExitEngineMixin._cleanup_after_exit(trader, token)
        assert len(calls) == 1, calls
        print("[PASS] live exit retains configured ATA cleanup")
    finally:
        exit_engine.handle_cleanup_after_sell = original

    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
