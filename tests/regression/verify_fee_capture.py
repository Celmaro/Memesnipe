"""R3-P2-4 regression: SWQoS tip/fee ground-truth capture.

Covers:
- identify_tip_provider: known jito tip-account prefixes map to the provider;
  unknown addresses -> None (ordinary value, not a tip).
- estimate_send_fee: priority fee (microlamports * CU / 1e15) + jito tip
  (lamports / 1e9) math; zero fee -> fee_source unknown; positive -> estimate.
- capture fields land on the sim_open/sim_close events via the trader's
  _fee_estimate_fields (verified through a shadow capture row).
"""

import asyncio
import glob
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from research.fee_capture import (  # noqa: E402
    FeeEstimate,
    estimate_send_fee,
    identify_tip_provider,
)


def main() -> int:
    failures = []

    def check(label: str, passed: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            failures.append(label)

    print("R3-P2-4 SWQoS fee capture")

    # Provider registry.
    check(
        "jito prefix recognized",
        identify_tip_provider("96gYZcK9q3q8L7qT6jZqW1q3q4q5q6q7q8q9q0q1q2") == "jito",
    )
    check(
        "unknown tip account -> None",
        identify_tip_provider("4xDrQZn9tQGfM6xWhf7fK9p3mFvY2nQs") is None,
    )

    # Fee math: priority fee 1000 microlamports/CU * 100k CU = 1e8
    # microlamports = 100 lamports = 1e-7 SOL.
    est = estimate_send_fee(1000, compute_units=100_000)
    check(
        "priority fee math (1000 microlamports/CU x 100k CU)",
        abs(est.priority_fee_sol - 1e-7) < 1e-15,
        f"-> {est.priority_fee_sol}",
    )
    check("no tip", est.tip_sol == 0.0 and est.tip_provider is None)

    # Tip: 10_000 lamports = 1e-5 SOL, provider identified.
    est2 = estimate_send_fee(
        None,
        tip_lamports=10_000,
        tip_account_b58="96gYZcK9q3q8L7qT6jZqW1q3q4q5q6q7q8q9q0q1q2",
    )
    check("jito tip math", abs(est2.tip_sol - 1e-5) < 1e-15, f"-> {est2.tip_sol}")
    check("tip provider carried", est2.tip_provider == "jito")
    check(
        "total = sum",
        abs(est2.total_sol - est2.priority_fee_sol - est2.tip_sol) < 1e-15,
    )

    # Zero fee -> fee_source unknown; positive -> estimate.
    check(
        "zero fee -> fee_source unknown",
        estimate_send_fee(None).to_capture_fields()["fee_source"] == "unknown",
    )
    check(
        "positive fee -> fee_source estimate",
        est2.to_capture_fields()["fee_source"] == "estimate",
    )

    # Capture fields land on the ledger row through the trader helper.
    from trading.universal_trader import UniversalTrader

    t = object.__new__(UniversalTrader)
    t._momentum_peak_rate = {}  # R4-P1-8 mock state
    t._momentum_peak_price = {}  # R4-P1-8 mock state
    t.fragility_scale_min = 1.0  # R4-P3-1 mock default (off)
    t.fragility_whale_threshold = 0.6  # R4-P3-1 mock default
    t.fragility_rate_threshold = 0.6  # R4-P3-1 mock default
    t.stagnation_exit_min = 0  # R4-P1-9 mock default (off)
    t.stagnation_band_pct = 0.02  # R4-P1-9 mock default
    t.max_wash_share = 0.0  # R4-P1-6 mock default
    from screening.wash_lpi import WashLpiDetector as _WLD  # R4-P1-6 mock default
    t._wash_lpi = _WLD()  # R4-P1-6 mock default
    t.max_gini_coefficient = 0.0  # R4-P1-10 mock default
    t.rug_score_threshold = 0.0  # R4-P1-1 mock default
    t.factory_deploy_rate = 0.0  # R4-P1-2 mock default
    t.max_coordinated_supply_share = 0.0  # R4-P1-3 mock default
    t.exit_mode = "tp_sl"  # R4-P1-7 mock default
    t.strategies_ledger = None  # R4-P1-7 mock default
    t.priority_fee_manager = SimpleNamespace2(
        calculate_priority_fee=_coro(5000)  # 5000 microlamports/CU
    )
    t.executor = SimpleNamespace2(tip_lamports=10_000, _tip_account="96gYZcK...")
    fields = asyncio.run(t._fee_estimate_fields())  # noqa: SLF001
    check(
        "trader fee fields include paid_fee_sol",
        fields.get("paid_fee_sol", 0.0) > 0,
        f"-> {fields}",
    )
    check("trader fee fields include fee_source", fields.get("fee_source") == "estimate")

    if failures:
        print(f"\nR3-P2-4 FAILED ({len(failures)}): {failures}")
        return 1
    print("\nR3-P2-4 SWQoS fee capture OK")
    return 0


class SimpleNamespace2:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _coro(value):
    async def _inner(*_a, **_k):
        return value

    return _inner


if __name__ == "__main__":
    sys.exit(main())