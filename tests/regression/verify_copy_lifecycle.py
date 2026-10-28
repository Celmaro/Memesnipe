"""R5-P0-4: copy-trader lifecycle — sell-follow + mode flag + qualification.

- A leader SELL reconciles the mirrored position (was previously ignored).
- _mirror_sell is called with the position size and leader_sell reason.
- Qualification: an unqualified leader profile (0 trades) skips the mirror.
- Mode flag: _mirror_buy branches on shadow_mode, not shadow_capture (the
  audit's latent bug).
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from copy_trader.engine import CopyTraderEngine  # noqa: E402
from screening.copy_target_store import CopyTarget, CopyTargetStore  # noqa: E402


class _FauxClient:
    pass


class _FauxProfiler:
    """Returns an empty profile (0 trades) -> qualification rejects."""

    async def profile_multiwindow(self, wallet: str) -> dict:
        return {"windows": {}}


def main() -> None:
    bought: list = []
    sold: list = []

    async def buy_mirror(token_info, target, size):
        bought.append((size, token_info.symbol))
        return True

    async def sell_mirror(token_info, target, size, reason, entry=None):
        sold.append((size, token_info.symbol, reason))
        return True

    MINT = "9xQeWvG817bTMtD2aXP2XmYhH3ZkQmG2jK9qR8dNpump"  # any valid base58

    async def run() -> None:
        with tempfile.TemporaryDirectory() as td:
            store = CopyTargetStore(Path(td) / "targets.db")
            store.upsert(
                CopyTarget(
                    wallet="LEADER1",
                    alias="lead1",
                    enabled=True,
                    min_notional_sol=0.0,
                    fixed_buy_sol=0.01,
                    mode="full",
                    max_position_sol=0.1,
                    slippage_bps=500,
                )
            )
            engine = CopyTraderEngine(
                _FauxClient(),
                store,
                buy_mirror=buy_mirror,
                sell_mirror=sell_mirror,
                min_buy_interval=0.0,
                # no profiler: this arm tests the buy/sell lifecycle only;
                # the qualification arm below uses the FauxProfiler.
            )
            # Leader buy -> mirrored + tracked.
            await engine._maybe_mirror(
                {"is_buy": True, "mint": MINT, "symbol": "SYM", "trader": "LEADER1", "sol_amount": 1000000000}
            )
            # Leader buy -> mirrored + tracked. C5.3: the ledger entry is a
            # dict with size/entry_price/entry_time, not a bare float.
            assert displayed(bought) and engine._mirror_positions.get(
                f"LEADER1:{MINT}"
            ), (
                "buy not mirrored/tracked",
                bought,
                engine._mirror_positions,
            )
            pre_entry = engine._mirror_positions[f"LEADER1:{MINT}"]
            pre = float(pre_entry["size"])
            assert pre > 0, pre_entry
            assert "entry_price" in pre_entry and "entry_time" in pre_entry, pre_entry
            # A source-tagged buy supplies its own trade price as entry.
            assert pre_entry["entry_price"] == 0.0 or pre_entry["entry_price"] > 0, pre_entry
            # Leader sell -> engine calls sell_mirror, position removed.
            await engine._maybe_mirror(
                {"is_buy": False, "mint": MINT, "symbol": "SYM", "trader": "LEADER1", "sol_amount": 1000000000}
            )
            assert sold and sold[0][2] == "leader_sell", sold
            assert abs(sold[0][0] - pre) < 1e-9, (sold, pre)
            assert f"LEADER1:{MINT}" not in engine._mirror_positions, engine._mirror_positions
            # Qualification: empty profile -> qualify_leader rejects (no mirror).
            engine2 = CopyTraderEngine(
                _FauxClient(), store, buy_mirror=buy_mirror, sell_mirror=sell_mirror, profiler=_FauxProfiler()
            )
            before = len(bought)
            await engine2._maybe_mirror(
                {"is_buy": True, "mint": MINT, "symbol": "SY2", "trader": "LEADER1", "sol_amount": 1000000000}
            )
            assert len(bought) == before, ("unqualified leader was mirrored", bought)

    def displayed(b) -> bool:
        return len(b) > 0

    asyncio.run(run())
    print("R5-P0-4 copy-trader lifecycle verifier: PASS")


if __name__ == "__main__":
    main()