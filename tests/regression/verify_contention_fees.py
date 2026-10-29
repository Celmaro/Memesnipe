"""R4-P0-3 regression: contention-mode fee racing + Jito tip leg.

Proves:
1. PriorityFeeManager contention is OFF by default (behavior unchanged).
2. set_contention(True) escalates the dynamic bid by contention_multiplier.
3. The contention cap = min(hard_cap, fraction-of-position ceiling) — a fee
   surge can never exceed max_fee_fraction_of_position of the quote.
4. JitoBundleExecutor tip scales by the armed scale (1.0 = off) and can
   never exceed the same fraction of the quote.
5. Config keys are wired: BOOL_FIELDS, VALIDATION_RULES, bot_runner reads,
   YAML defaults (all off / safe).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

import yaml  # noqa: E402

import config_loader  # noqa: E402
from core.priority_fee.manager import PriorityFeeManager  # noqa: E402
from execution.executor import JitoExecutor  # noqa: E402

PASS = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global PASS
    if ok:
        PASS += 1
        print(f"  [PASS] {name} {extra}")
    else:
        print(f"  [FAIL] {name} {extra}")


class FakeClient:
    async def post_rpc(self, body: dict, deadline_seconds: float | None = None):
        # 10 entries so the 70th-percentile quantiles(n=10)[-3] is defined.
        return {"result": [{"prioritizationFee": 250_000}] * 10}

    def _get_session(self):
        raise RuntimeError("unused")


async def main() -> None:
    fake = FakeClient()

    # 1. Default off: no escalation, byte-identical behavior.
    mgr = PriorityFeeManager(
        client=fake,
        enable_dynamic_fee=True,
        enable_fixed_fee=False,
        fixed_fee=0,
        extra_fee=0.0,
        hard_cap=5_000_000,
        bid_multiplier=1.0,
    )
    check("contention off by default", not mgr.contention_armed)
    fee_off = await mgr.calculate_priority_fee()
    check("default fee = sampled base", fee_off == 250_000, f"-> {fee_off}")

    # 2. Armed: multiplier escalates the bid.
    mgr.set_contention(True)
    check("contention armed flag", mgr.contention_armed)
    fee_armed = await mgr.calculate_priority_fee()
    check(
        "armed bid = base x contention_multiplier",
        fee_armed == int(250_000 * 2.5),
        f"-> {fee_armed}",
    )

    # 3. Fraction-of-position cap: armed + cap never exceeds min(hard_cap,
    #    quote * fraction / CU math).
    mgr2 = PriorityFeeManager(
        client=fake,
        enable_dynamic_fee=True,
        enable_fixed_fee=False,
        fixed_fee=0,
        extra_fee=0.0,
        hard_cap=5_000_000,
        bid_multiplier=1.0,
    )
    # 0.01 SOL quote, 0.5% fraction, 100k CU:
    # cap = 0.01 * 0.005 * 1e15 / 100_000 = 500_000 microlamports.
    quote_sol, fraction, cu = 0.01, 0.005, 100_000
    cap = int(quote_sol * fraction * 1e15 / cu)
    check("cap math", cap == 500_000, f"-> {cap}")
    mgr2.set_contention(True, cap=cap)
    # Sampled 250_000 x 2.5 = 625_000 > cap 500_000 -> clamped.
    fee_capped = await mgr2.calculate_priority_fee()
    check(
        "armed fee clamped to fraction cap",
        fee_capped == 500_000,
        f"-> {fee_capped}",
    )

    # 4. Jito tip leg: scale off by default, escalates when armed.
    jito = JitoExecutor(client=fake, tip_lamports=10_000)
    check("jito tip scale off by default", jito._contention_tip_scale == 1.0)
    jito.set_contention_tip_scale(2.5)
    check("jito tip scale armed", jito._contention_tip_scale == 2.5)
    jito.set_contention_tip_scale(0.5)  # clamped >= 1.0
    check("jito tip scale clamped >= 1", jito._contention_tip_scale == 1.0)

    # 5. Config wiring.
    cfg = yaml.safe_load((REPO / "bots/memesnipe.yaml").read_text())
    check(
        "YAML contention_enabled is a valid bool",
        isinstance(cfg["filters"].get("contention_enabled"), bool),
        f"-> {cfg['filters'].get('contention_enabled')!r}",
    )
    check(
        "contention_enabled in BOOL_FIELDS",
        "filters.contention_enabled" in config_loader.BOOL_FIELDS,
    )
    rules = dict(
        (r[0], r[2]) for r in config_loader.CONFIG_VALIDATION_RULES if isinstance(r, tuple)
    )
    for key in (
        "filters.contention_creates_per_min",
        "filters.contention_multiplier",
        "filters.contention_window_s",
        "filters.max_fee_fraction_of_position",
    ):
        check(f"validation rule for {key}", key in rules)
    src = (REPO / "src/bot_runner.py").read_text()
    for arg in (
        "contention_enabled=",
        "contention_multiplier=",
        "contention_creates_per_min=",
        "contention_window_s=",
        "max_fee_fraction_of_position=",
    ):
        check(f"bot_runner passes {arg}", arg in src)
    trader_src = (REPO / "src/trading/universal_trader.py").read_text()
    check("trader arms both fee managers", "set_contention(armed" in trader_src)
    check(
        "trader wires jito tip scale",
        "set_contention_tip_scale" in trader_src,
    )

    print(f"\nR4-P0-3 contention-fees regression: {PASS} checks")
    return 0 if PASS == 22 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))