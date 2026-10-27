"""R5-P1-3: cost model subtracts modeled fees from gross PnL.

pump.fun protocol fee 1.25%/side; network/priority modeled from config.
A +20% gross round trip must come out ~+16.7%, and the model is monotone
with position size (no free lunch at larger size in the fee term).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from research.cost_model import CostModel, net_round_trip  # noqa: E402


def main() -> None:
    m = CostModel()
    # +20% gross on 0.01 SOL -> ~+16.74% net (verified in dev)
    net = net_round_trip(0.20, 0.01)
    assert abs(net - 0.1674) < 1e-3, net
    # 0% gross -> negative (fees eat you)
    net0 = net_round_trip(0.0, 0.01)
    assert net0 < 0.0, net0
    # larger position pays the same fee fraction but a smaller NETWORK
    # fraction (absolute txn+priority SOL divided by bigger quote)
    net_big = net_round_trip(0.20, 1.0)
    diff = net_big - net  # ≈ network/0.01 - network/1.0 ≈ 0.0028 - 0.000028
    assert abs(diff - (0.000028 / 0.01 - 0.000028)) < 1e-4, (net, net_big, diff)
    # gross loss grows net loss (fees asymmetric both sides)
    net_loss = net_round_trip(-0.20, 0.01)
    assert net_loss < -0.20, net_loss
    print("R5-P1-3 cost-model verifier: PASS")


if __name__ == "__main__":
    main()