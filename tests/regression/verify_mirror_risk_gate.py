"""Wiring regression: copy-trader mirror path respects R3 guards.

Covers the fixes found in the wiring audit:
- P2-10: a mirror buy is gated by ``risk_tracker.buy_allowed`` (loss limit
  and budget block copies too), and its spend is recorded via
  ``record_spend`` — counts toward ``budget_sol`` WITHOUT inflating
  ``open_positions`` (mirrors are never closed by the exit machinery, so
  counting them would wedge ``max_concurrent_positions`` forever).
- P2-13: the real-mode mirror buy executes inside the serialization lock,
  so a copy buy never overlaps an in-flight sell.
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from trading.risk_guard import RiskTracker  # noqa: E402


class _Token:
    def __init__(self):
        from interfaces.core import Platform

        self.symbol = "TST"
        self.mint = "Mint111111111111111111111111111111111111111"
        self.platform = Platform.PUMP_FUN


def _trader(budget: float = 0.0, loss_limit: float = 0.0) -> SimpleNamespace:
    """Mini trader with the exact attrs _mirror_buy touches."""
    t = SimpleNamespace(
        risk_tracker=RiskTracker(
            max_concurrent_positions=0,
            daily_loss_limit_sol=loss_limit,
            budget_sol=budget,
        ),
        shadow_capture=None,
        solana_client=object(),
        wallet=SimpleNamespace(public_key="wallet"),
        priority_fee_manager=object(),
        max_retries=3,
        executor=object(),
        _trade_serialization_lock=asyncio.Lock(),
        _read_price=None,
        _pool_snapshot=None,
    )
    return t


def _target():
    return SimpleNamespace(alias="lead", wallet="lead", slippage_bps=500)


def main() -> int:
    failures = []

    def check(label: str, passed: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} {detail}")
        if not passed:
            failures.append(label)

    print("Wiring: copy-trader mirror path respects R3 guards")

    # RiskTracker.record_spend counts toward budget, not open_positions.
    rt = RiskTracker(max_concurrent_positions=2, budget_sol=1.0)
    rt.record_spend(0.6)
    check(
        "record_spend tracks budget",
        abs(rt.session_spend_sol - 0.6) < 1e-9,
        f"-> {rt.session_spend_sol}",
    )
    check(
        "record_spend leaves open_positions alone",
        rt.open_positions == 0,
        f"-> {rt.open_positions}",
    )

    import trading.universal_trader as ut

    # Mirror buy: gated by risk, serialized, spend recorded.
    from unittest.mock import patch as mpatch

    t = _trader(budget=1.0)
    execute_results = {"called": 0}

    class _Buyer:
        async def execute(self, token_info):
            execute_results["called"] += 1
            return SimpleNamespace(success=True, price=1.0, amount=1.0)

    async def _run() -> bool:
        # _mirror_buy imports PlatformAwareBuyer from trading.platform_aware
        # inside its body — patch THAT module, not `ut`.
        import trading.platform_aware as pa_mod

        with mpatch.object(pa_mod, "PlatformAwareBuyer", lambda *a, **k: _Buyer()):
            return await ut.UniversalTrader._mirror_buy(
                t, _Token(), _target(), 0.5
            )  # type: ignore[arg-type]

    ok = asyncio.run(_run())
    check(
        "mirror executes through real buyer",
        ok and execute_results["called"] == 1,
        f"-> ok={ok} calls={execute_results['called']}",
    )
    check(
        "mirror spend recorded toward budget",
        abs(t.risk_tracker.session_spend_sol - 0.5) < 1e-9,
        f"-> {t.risk_tracker.session_spend_sol}",
    )

    # Loss limit blocks the mirror.
    t2 = _trader(loss_limit=0.1)
    t2.risk_tracker.record_close(-0.2)  # realized loss past the limit
    calls2 = {"n": 0}

    class _BuyerBlocked:
        async def execute(self, token_info):
            calls2["n"] += 1
            return SimpleNamespace(success=True)

    async def _run2() -> bool:
        import trading.platform_aware as pa_mod

        with mpatch.object(pa_mod, "PlatformAwareBuyer", lambda *a, **k: _BuyerBlocked()):
            return await ut.UniversalTrader._mirror_buy(
                t2, _Token(), _target(), 0.5
            )  # type: ignore[arg-type]

    ok2 = asyncio.run(_run2())
    check(
        "loss limit blocks mirror (no buy executed)",
        not ok2 and calls2["n"] == 0,
        f"-> ok={ok2} calls={calls2['n']}",
    )

    # Serialization: source contains the lock around the mirror execute.
    src = Path("src/trading/universal_trader.py").read_text()
    mirror_src = src[src.find("async def _mirror_buy"):]
    check(
        "mirror buy wrapped in serialization lock",
        "async with self._trade_serialization_lock:" in mirror_src,
    )
    check(
        "mirror gated by risk_tracker.buy_allowed",
        "if not self.risk_tracker.buy_allowed:" in mirror_src,
    )

    if failures:
        print(f"\nWRITE-REGRESSION FAILED ({len(failures)}): {failures}")
        return 1
    print("\nCopy-trader mirror wiring OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())