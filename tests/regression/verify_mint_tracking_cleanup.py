"""Offline checks for transient per-mint tracking retention and cleanup."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from solders.pubkey import Pubkey  # noqa: E402
from trading.universal_trader import UniversalTrader  # noqa: E402


def main() -> None:
    trader = object.__new__(UniversalTrader)
    trader.token_timestamps = {}
    trader._buy_reservations = set()
    trader._active_exit_mints = set()
    trader._unsold_positions = {}
    closed_pubkey = Pubkey.from_bytes(bytes([7]) * 32)
    closed_mint = str(closed_pubkey)
    trader._rug_buyers = {"candidate": {"buyer1"}, closed_mint: {"buyer2"}}
    trader._creator_sellers = {closed_mint: {"creator"}}
    trader._rug_entry = {closed_mint: (100, 123)}
    trader._momentum_peak_rate = {closed_mint: 10.0}
    trader._momentum_peak_price = {closed_mint: 2.0}
    trader._creator_sold_for_exit = {closed_mint}
    trader._own_trade_prompt = {closed_mint}
    trader._graduated_mints = {closed_pubkey}

    assert not trader._should_track_mint_buyers("unknown")
    trader.token_timestamps["candidate"] = 1.0
    assert trader._should_track_mint_buyers("candidate")
    del trader.token_timestamps["candidate"]
    trader._buy_reservations.add("candidate")
    assert trader._should_track_mint_buyers("candidate")
    trader._buy_reservations.clear()
    trader._active_exit_mints.add("candidate")
    assert trader._should_track_mint_buyers("candidate")
    trader._active_exit_mints.clear()
    trader._unsold_positions["candidate"] = (None, None)
    assert trader._should_track_mint_buyers("candidate")
    print("[PASS] buyer tracking is scoped to candidates and owned positions")

    trader._clear_mint_tracking_state(closed_mint)
    for name in (
        "_rug_buyers",
        "_creator_sellers",
        "_rug_entry",
        "_momentum_peak_rate",
        "_momentum_peak_price",
    ):
        assert closed_mint not in getattr(trader, name), name
    for name in ("_creator_sold_for_exit", "_own_trade_prompt"):
        assert closed_mint not in getattr(trader, name), name
    assert closed_pubkey not in trader._graduated_mints
    print("[PASS] exit cleanup releases transient per-mint state")
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
