"""Verify shadow exits never submit real ATA-cleanup transactions."""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import trading.exit_engine as exit_engine  # noqa: E402
import trading.universal_trader as universal_trader_module  # noqa: E402
from trading.exit_engine import ExitEngineMixin  # noqa: E402
from trading.universal_trader import UniversalTrader  # noqa: E402


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

    # The post-session path must obey the same read-only contract.
    post_session_calls: list[tuple] = []

    async def fake_post_session_cleanup(*args) -> None:
        post_session_calls.append(args)

    original_post_session = universal_trader_module.handle_cleanup_post_session
    universal_trader_module.handle_cleanup_post_session = fake_post_session_cleanup
    try:
        trader = object.__new__(UniversalTrader)
        trader.shadow_mode = True
        trader.traded_mints = {"mint"}
        trader.token_timestamps = {}
        trader.processed_tokens = set()
        trader.solana_client = SimpleNamespace(close=lambda: asyncio.sleep(0))
        await UniversalTrader._cleanup_resources(trader)
        assert not post_session_calls, post_session_calls
        print("[PASS] shadow shutdown performs no post-session cleanup transaction")
    finally:
        universal_trader_module.handle_cleanup_post_session = original_post_session

    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
