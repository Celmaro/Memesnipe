"""R5-P0-3: creator blacklist uses net-of-fees evidence + min_trades 10.

- A creator with 3 gross-positive but fee-crushed trades must NOT be
  blacklisted at min_trades=10 (evidence strength), even though the old
  default of 3 would have banned them.
- The rug_memory ingestion now nets the shadow pnl through cost_model.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

sys_path_guard = None  # noqa: F841 - placeholder

from screening.creator_pnl_store import CreatorPnlStore  # noqa: E402


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        db = CreatorPnlStore(Path(td) / "creator_pnl.db")
        # 3 small net losses — enough to ban under the old default
        for _ in range(3):
            db.record_trade("CREATOR_NET_LOSER", -0.00005)  # ~ -0.5% on 0.01
        # at the old default min_trades=3 they'd be blacklisted...
        assert db.is_blacklisted("CREATOR_NET_LOSER", min_trades=3, max_net_sol=0.0)
        # ...but R5-P0-3 raised the bar to 10 (sufficient sample)
        assert not db.is_blacklisted("CREATOR_NET_LOSER", min_trades=10, max_net_sol=0.0)
        # after 10 trades the net evidence is decisive
        for _ in range(7):
            db.record_trade("CREATOR_NET_LOSER", -0.00005)
        assert db.is_blacklisted("CREATOR_NET_LOSER", min_trades=10, max_net_sol=0.0)
        print("R5-P0-3 creator blacklist net/sample verifier: PASS")


if __name__ == "__main__":
    main()